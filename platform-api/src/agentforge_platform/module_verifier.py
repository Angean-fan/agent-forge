"""A08：分层验证器抽象（部门小循环单模块快检 + 总验收强制 Docker Runner）。

实现“部门小循环支持受限本地子进程 / Runner 单模块快检 + 后端/全栈总验收强制 Docker Runner”的分层隔离策略：
1. ``DockerRunnerModuleVerifier``：默认实现，调用独立 Docker Runner 的 ``POST /verify-module``；
2. ``LocalProcessModuleVerifier``：仅在本地开发显式开启 ``AGENTFORGE_LOCAL_DEPT_TEST_RUNNER=1`` 时可用，
   严格限制工作目录、命令白名单（``mvn`` / ``npm`` / ``npx``）、严禁 ``shell=True``、最小环境变量白名单、
   硬超时强杀进程树与日志脱敏截断；
3. ``require_docker_runner_for_final_acceptance()`` / ``run_final_acceptance_verify()`` /
   ``run_final_acceptance_accept()``：通过类型与运行时双重拦截，严禁将 ``LocalProcessModuleVerifier``
   用于后端总装验收或全栈最终验收。
"""

from __future__ import annotations

import inspect
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NoReturn, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.runner_client import HttpRunnerClient, RunnerClient

_STRICT = ConfigDict(extra="forbid")

LOCAL_DEPT_TEST_RUNNER_ENV = "AGENTFORGE_LOCAL_DEPT_TEST_RUNNER"
DEFAULT_MODULE_TIMEOUT_SECONDS = 120
MAX_MODULE_TIMEOUT_SECONDS = 600
LOG_LIMIT = 4000
TIMEOUT_EXIT_CODE = 124

ALLOWED_LOCAL_COMMANDS: frozenset[str] = frozenset({"mvn", "npm", "npx"})

# 最小非敏感环境变量白名单：绝不透传任何模型密钥、数据库 URL、平台 Token 或云凭据
ENV_WHITELIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "JAVA_HOME",
        "M2_HOME",
        "MAVEN_HOME",
        "NODE_ENV",
        "CI",
    }
)

_SAFE_MODULE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SAFE_JAVA_SELECTOR = re.compile(r"^[A-Za-z0-9_.*#,]+$")
_SAFE_FRONTEND_SELECTOR = re.compile(r"^[A-Za-z0-9_./-]+$")
_FORBIDDEN_SELECTOR_SUBSTRINGS = ("..", ";", "|", "&", "$", "`", ">", "<", "\n", "\r", "\x00", " ")

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key)\s*[:=]\s*\S+"
        ),
        r"\1=***",
    ),
    (re.compile(r"sk-[A-Za-z0-9_-]{12,}"), "sk-***"),
    (re.compile(r"(?:ghp|gho|ghu|ghs)_[A-Za-z0-9]{16,}"), "ghp_***"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{16,}"), "github_pat_***"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA***"),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "<jwt>"),
    (
        re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^:\s/]+:)[^@\s]+(@)"),
        r"\1***\2",
    ),
]

_INFRA_MARKERS = (
    "Could not resolve dependencies",
    "Could not transfer artifact",
    "Could not find artifact",
    "Connection refused",
    "UnknownHostException",
    "Network is unreachable",
    "ENOTFOUND",
    "ETIMEDOUT",
    "ECONNREFUSED",
    "command not found",
    "executable file not found",
)


def local_dept_test_runner_enabled() -> bool:
    """本地受限子进程快检开关（默认关闭；仅显式设置 ``AGENTFORGE_LOCAL_DEPT_TEST_RUNNER=1`` 启用）。"""
    return os.environ.get(LOCAL_DEPT_TEST_RUNNER_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def validate_module_id(module_id: str) -> str:
    cleaned = module_id.strip()
    if not _SAFE_MODULE_ID.fullmatch(cleaned):
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"非法 module_id：{module_id!r}",
            status_code=422,
        )
    return cleaned


def validate_test_selector(stack: str, test_selector: str) -> str:
    cleaned = test_selector.strip()
    if not cleaned or len(cleaned) > 200:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "test_selector 不能为空且长度不得超过 200",
            status_code=422,
        )
    if any(bad in cleaned for bad in _FORBIDDEN_SELECTOR_SUBSTRINGS):
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"test_selector 包含禁止字符或路径穿越：{test_selector!r}",
            status_code=422,
        )
    if cleaned.startswith(("-", "/")):
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"test_selector 不得以 '-' 或 '/' 开头：{test_selector!r}",
            status_code=422,
        )
    if stack == "java":
        if not _SAFE_JAVA_SELECTOR.fullmatch(cleaned):
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"非法 Java 测试选择器：{test_selector!r}",
                status_code=422,
            )
        for part in cleaned.split(","):
            if not part or part.startswith(("-", ".")) or part.endswith("."):
                raise AppException(
                    ErrorCode.VALIDATION_ERROR,
                    f"非法 Java 测试选择器片段：{part!r}",
                    status_code=422,
                )
        return cleaned
    if stack == "frontend":
        if not _SAFE_FRONTEND_SELECTOR.fullmatch(cleaned):
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"非法前端测试选择器：{test_selector!r}",
                status_code=422,
            )
        return cleaned
    raise AppException(
        ErrorCode.VALIDATION_ERROR,
        f"不支持的技术栈：{stack!r}",
        status_code=422,
    )


def build_sanitized_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """构造最小安全环境变量字典，彻底清除所有密钥、数据库串与平台内部变量。"""
    source = os.environ if base_env is None else base_env
    sanitized = {key: source[key] for key in ENV_WHITELIST if key in source}
    sanitized.setdefault("CI", "true")
    return sanitized


_NOISE_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"libjansi\.so|jansi\.tmpdir|jansi-[0-9]", re.IGNORECASE),
    re.compile(r"Sharing is only supported for boot loader classes"),
    re.compile(r"Failed to load native library:jansi", re.IGNORECASE),
    re.compile(r"^\[ERROR\]\s+To see the full debug logging", re.IGNORECASE),
    re.compile(r"^\[ERROR\]\s+Re-run Maven using the -X", re.IGNORECASE),
    re.compile(r"^\[ERROR\]\s+For more information about the errors and possible solutions", re.IGNORECASE),
    re.compile(r"^\[ERROR\]\s+(?:->\s*)?\[Help \d+\]", re.IGNORECASE),
    re.compile(r"^\[ERROR\]\s+After correcting the problems, you can resume", re.IGNORECASE),
)
_ERROR_LINE_PATTERN = re.compile(
    r"(\[ERROR\]|<<<\s*(?:FAILURE|ERROR)!|COMPILATION ERROR|Failed tests:|Tests in error:|"
    r"There are test failures|AssertionError|AssertionFailedError|Exception:|Error:|"
    r"expected:|but was:|Expecting\s+|Wanted but not invoked|Actually, there were zero interactions|"
    r"\bFAIL\s+\S|error TS\d+:|Caused by:|cannot find symbol|incompatible types)"
)
_SUREFIRE_FAILURE_MARKER_RE = re.compile(r"<<<\s*(?:FAILURE|ERROR)!")
_STACK_FRAME_RE = re.compile(r"^\s+at\s+")
_BUSINESS_FRAME_RE = re.compile(r"^\s+at\s+com\.agentforge\.")


def _strip_noise_lines(text: str) -> str:
    lines = text.splitlines()
    filtered = [line for line in lines if not any(p.search(line) for p in _NOISE_LINE_PATTERNS)]
    return "\n".join(filtered)


def extract_surefire_failure_summary(artifact_dir: str | Path, *, max_chars: int = 2800) -> str:
    """直接解析 ``backend/target/surefire-reports/*.txt`` 提取失败断言、期望/实际值与业务堆栈。"""
    reports_dir = Path(artifact_dir) / "backend" / "target" / "surefire-reports"
    if not reports_dir.is_dir():
        return ""

    blocks: list[str] = []
    for report_file in sorted(reports_dir.glob("*.txt")):
        if not report_file.is_file():
            continue
        try:
            content = report_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _SUREFIRE_FAILURE_MARKER_RE.search(content):
            continue

        lines = content.splitlines()
        file_lines: list[str] = []
        for header in lines[:6]:
            stripped = header.strip()
            if stripped.startswith(("Test set:", "Tests run:")):
                file_lines.append(stripped)

        idx = 0
        while idx < len(lines):
            line = lines[idx]
            if not _SUREFIRE_FAILURE_MARKER_RE.search(line):
                idx += 1
                continue
            file_lines.append(line.strip())
            idx += 1
            msg_lines = 0
            business_frames = 0
            fallback_frames: list[str] = []
            in_stack = False
            while idx < len(lines):
                cur = lines[idx]
                if _SUREFIRE_FAILURE_MARKER_RE.search(cur):
                    break
                if cur.startswith("Caused by:"):
                    in_stack = False
                    file_lines.append(cur.rstrip())
                    msg_lines += 1
                    idx += 1
                    continue
                if _STACK_FRAME_RE.match(cur):
                    in_stack = True
                    if _BUSINESS_FRAME_RE.match(cur):
                        if business_frames < 4:
                            file_lines.append(cur.rstrip())
                            business_frames += 1
                    elif len(fallback_frames) < 2:
                        fallback_frames.append(cur.rstrip())
                    idx += 1
                    continue
                if not in_stack:
                    if cur.strip() and msg_lines < 12:
                        file_lines.append(cur.rstrip())
                        msg_lines += 1
                    idx += 1
                    continue
                if not cur.strip():
                    idx += 1
                    break
                idx += 1
            if business_frames == 0 and fallback_frames:
                file_lines.extend(fallback_frames)
        if file_lines:
            blocks.append("\n".join(file_lines))

    if not blocks:
        return ""
    header = "[surefire-failures]\n"
    joined = header + "\n---\n".join(blocks)
    if len(joined) <= max_chars:
        return joined
    marker = "\n...[more surefire failures truncated]"
    budget = max(0, max_chars - len(marker))
    clipped = joined[:budget].rsplit("\n", 1)[0]
    return f"{clipped}{marker}"


def _extract_error_focused_excerpt(text: str, limit: int) -> str | None:
    lines = text.splitlines()
    hit_indices = [idx for idx, line in enumerate(lines) if _ERROR_LINE_PATTERN.search(line)]
    if not hit_indices:
        return None
    keep: set[int] = set()
    for idx in hit_indices:
        for pos in range(max(0, idx - 2), min(len(lines), idx + 5)):
            keep.add(pos)
    extracted_lines: list[str] = []
    prev = -2
    for idx in sorted(keep):
        if prev >= 0 and idx > prev + 1:
            extracted_lines.append("...")
        extracted_lines.append(lines[idx])
        prev = idx
    extracted = "\n".join(extracted_lines)
    if len(extracted) <= limit:
        return (
            f"[error-focused excerpt]\n...[truncated {max(0, len(text) - len(extracted))} chars]...\n"
            f"{extracted}"
        )
    budget = max(0, limit - 64)
    head = budget * 2 // 3
    tail = budget - head
    return (
        f"[error-focused excerpt]\n{extracted[:head]}"
        f"\n...[truncated {max(0, len(text) - limit)} chars]...\n"
        f"{extracted[-tail:] if tail > 0 else ''}"
    )


def redact_and_truncate_log(text: str, limit: int = LOG_LIMIT) -> str:
    """对验证日志先脱敏、过滤环境噪音、再优先保留 Surefire 精确断言或关键报错段截断。"""
    redacted = text
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    redacted = _strip_noise_lines(redacted)
    if len(redacted) <= limit:
        return redacted
    if redacted.lstrip().startswith("[surefire-failures]"):
        marker = "\n...[truncated]"
        budget = max(0, limit - len(marker))
        clipped = redacted[:budget].rsplit("\n", 1)[0]
        return f"{clipped}{marker}"
    focused = _extract_error_focused_excerpt(redacted, limit)
    if focused is not None:
        return focused
    head = limit * 2 // 3
    tail = limit - head
    return f"{redacted[:head]}\n...[truncated {len(redacted) - limit} chars]...\n{redacted[-tail:]}"


def _classify_local_failure(exit_code: int, log: str) -> str:
    if exit_code == TIMEOUT_EXIT_CODE:
        return "TIMEOUT"
    if any(marker in log for marker in _INFRA_MARKERS):
        return "INFRASTRUCTURE"
    return "BUILD_FAILED"


class ModuleVerificationResult(BaseModel):
    model_config = _STRICT

    artifact_dir: str
    module_id: str
    stack: Literal["java", "frontend"]
    test_selector: str
    status: Literal["PASS", "FAIL", "NOT_RUN"]
    verifier_kind: Literal["docker_runner", "local_process"]
    error_code: str | None = None
    exit_code: int | None = None
    duration_ms: int = 0
    command: list[str] = []
    detail: str | None = None
    log_excerpt: str | None = None


@runtime_checkable
class ModuleVerifier(Protocol):
    """单模块快检协议（仅用于部门小循环，严禁替代总验收）。"""

    verifier_kind: Literal["docker_runner", "local_process"]

    def verify_module(
        self,
        *,
        artifact_dir: str | Path,
        module_id: str,
        stack: Literal["java", "frontend"],
        test_selector: str,
        timeout_seconds: int = DEFAULT_MODULE_TIMEOUT_SECONDS,
    ) -> ModuleVerificationResult: ...


def to_runner_artifact_dir(
    artifact_dir: str | Path,
    work_root: str | Path | None = None,
) -> str:
    """将工件目录规范化为 Runner 接受的相对路径（位于 ``work_root`` 之内，不以 ``/`` 开头）。

    Runner ``security.resolve_artifact_dir`` 严禁绝对路径与路径穿越；当调用方传入 ``work_root`` 时，
    若 ``artifact_dir`` 为绝对路径则自动转为相对 POSIX 路径，越界直接拒绝（403 ``FORBIDDEN``）。
    """
    raw = str(artifact_dir).strip()
    if not raw or "\x00" in raw:
        raise AppException(ErrorCode.BAD_REQUEST, "artifact_dir 不能为空", status_code=422)
    candidate = Path(raw)
    if not candidate.is_absolute():
        if "\\" in raw or any(part in ("", ".", "..") for part in PurePosixPath(raw).parts):
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"artifact_dir 含非法字符或路径穿越：{artifact_dir!r}",
                status_code=403,
            )
        return PurePosixPath(raw).as_posix()

    if work_root is None:
        return raw

    root = Path(work_root).resolve()
    resolved = candidate.resolve()
    if resolved == root or root not in resolved.parents:
        raise AppException(
            ErrorCode.FORBIDDEN,
            f"artifact_dir 越界（不在受控 work_root 内）：{artifact_dir!r}",
            status_code=403,
        )
    return resolved.relative_to(root).as_posix()


class DockerRunnerModuleVerifier:
    """通过受控 Docker Runner ``POST /verify-module`` 执行单模块快检。"""

    verifier_kind: Literal["docker_runner", "local_process"] = "docker_runner"

    def __init__(
        self,
        runner_client: HttpRunnerClient | Any,
        *,
        work_root: str | Path | None = None,
    ) -> None:
        if isinstance(runner_client, LocalProcessModuleVerifier):
            raise AppException(
                ErrorCode.FORBIDDEN,
                "DockerRunnerModuleVerifier 禁止包装 LocalProcessModuleVerifier",
                status_code=403,
            )
        self._client = runner_client
        self._work_root = Path(work_root).resolve() if work_root is not None else None

    def verify_module(
        self,
        *,
        artifact_dir: str | Path,
        module_id: str,
        stack: Literal["java", "frontend"],
        test_selector: str,
        timeout_seconds: int = DEFAULT_MODULE_TIMEOUT_SECONDS,
    ) -> ModuleVerificationResult:
        clean_mod = validate_module_id(module_id)
        clean_sel = validate_test_selector(stack, test_selector)
        runner_dir = to_runner_artifact_dir(artifact_dir, self._work_root)
        raw = self._client.verify_module(
            artifact_dir=runner_dir,
            module_id=clean_mod,
            stack=stack,
            test_selector=clean_sel,
            timeout_seconds=timeout_seconds,
        )
        return ModuleVerificationResult.model_validate({**raw, "verifier_kind": "docker_runner"})


def _resolve_isolated_artifact_dir(work_root: Path, artifact_dir: str | Path) -> Path:
    root = work_root.resolve()
    raw = str(artifact_dir).strip()
    if not raw or "\x00" in raw:
        raise AppException(ErrorCode.BAD_REQUEST, "artifact_dir 不能为空", status_code=422)
    candidate = Path(raw)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        if ".." in Path(raw).parts:
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"artifact_dir 含路径穿越：{artifact_dir!r}",
                status_code=403,
            )
        resolved = (root / raw).resolve()
    if resolved != root and root not in resolved.parents:
        raise AppException(
            ErrorCode.FORBIDDEN,
            f"artifact_dir 越界（不在受限工作根内）：{artifact_dir!r}",
            status_code=403,
        )
    if not resolved.is_dir():
        raise AppException(
            ErrorCode.NOT_FOUND,
            f"artifact_dir 不存在：{artifact_dir!r}",
            status_code=404,
        )
    return resolved


def _kill_process_tree(proc: subprocess.Popen[bytes]) -> None:
    """强制终结独立会话进程组（进程树），防止超时后遗留孤儿子进程。"""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


class LocalProcessModuleVerifier:
    """受限本地子进程单模块快检器（仅限显式开启 ``AGENTFORGE_LOCAL_DEPT_TEST_RUNNER=1`` 时的部门小循环）。

    安全护栏：
    - 严禁用于后端总装验收或全栈总验收（显式调用 ``verify()``/``accept()`` 直接抛出 ``403 FORBIDDEN``）；
    - 严格工作目录隔离（必须位于 ``work_root`` 之内）；
    - 严格命令白名单（仅 ``mvn`` / ``npm`` / ``npx``），严禁 ``shell=True``；
    - 最小环境变量白名单（彻底剔除 API Key / DB URL / Token）；
    - 硬超时强杀进程树（``start_new_session=True`` + ``os.killpg(..., SIGKILL)``）与日志脱敏截断。
    """

    verifier_kind: Literal["docker_runner", "local_process"] = "local_process"

    def __init__(
        self,
        work_root: str | Path,
        *,
        allowed_commands: frozenset[str] = ALLOWED_LOCAL_COMMANDS,
        log_limit: int = LOG_LIMIT,
    ) -> None:
        self._work_root = Path(work_root).resolve()
        self._allowed_commands = frozenset(allowed_commands)
        self._log_limit = log_limit

    # ---- 双重防线：防止误把 LocalProcessModuleVerifier 当作总验收 RunnerClient ----
    def verify(self, artifact_dir: str) -> NoReturn:
        raise AppException(
            ErrorCode.FORBIDDEN,
            "安全策略拒绝：总验收（verify）强制使用独立 Docker Runner，严禁降级为 LocalProcessModuleVerifier",
            status_code=403,
        )

    def accept(self, artifact_dir: str) -> NoReturn:
        raise AppException(
            ErrorCode.FORBIDDEN,
            "安全策略拒绝：总验收（accept）强制使用独立 Docker Runner，严禁降级为 LocalProcessModuleVerifier",
            status_code=403,
        )

    def _run_whitelisted_command(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> tuple[int, str]:
        if not argv:
            raise AppException(ErrorCode.VALIDATION_ERROR, "执行命令不能为空", status_code=422)
        executable = argv[0]
        if "/" in executable or "\\" in executable or executable not in self._allowed_commands:
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"命令不在受限白名单内：{executable!r}（仅允许 {sorted(self._allowed_commands)}）",
                status_code=403,
            )
        if shutil.which(executable) is None:
            return 127, f"executable file not found: {executable}"

        env = build_sanitized_env()
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout_b, stderr_b = proc.communicate(timeout=timeout_seconds)
            combined = (stdout_b + b"\n" + stderr_b).decode("utf-8", "replace")
            return proc.returncode, combined
        except subprocess.TimeoutExpired as exc:
            _kill_process_tree(proc)
            partial_out = exc.stdout or b""
            partial_err = exc.stderr or b""
            if isinstance(partial_out, str):
                partial_out = partial_out.encode("utf-8", "replace")
            if isinstance(partial_err, str):
                partial_err = partial_err.encode("utf-8", "replace")
            partial = (partial_out + b"\n" + partial_err).decode("utf-8", "replace")
            return TIMEOUT_EXIT_CODE, f"{partial}\nlocal module verifier timed out after {timeout_seconds}s"

    def verify_module(
        self,
        *,
        artifact_dir: str | Path,
        module_id: str,
        stack: Literal["java", "frontend"],
        test_selector: str,
        timeout_seconds: int = DEFAULT_MODULE_TIMEOUT_SECONDS,
    ) -> ModuleVerificationResult:
        if not local_dept_test_runner_enabled():
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"本地受限子进程快检未开启（请显式设置 {LOCAL_DEPT_TEST_RUNNER_ENV}=1 或改用 DockerRunnerModuleVerifier）",
                status_code=403,
            )
        if timeout_seconds < 1 or timeout_seconds > MAX_MODULE_TIMEOUT_SECONDS:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"timeout_seconds 超出范围 [1..{MAX_MODULE_TIMEOUT_SECONDS}]：{timeout_seconds}",
                status_code=422,
            )

        clean_mod = validate_module_id(module_id)
        clean_sel = validate_test_selector(stack, test_selector)
        host_dir = _resolve_isolated_artifact_dir(self._work_root, artifact_dir)

        started = time.monotonic()
        if stack == "java":
            sub_dir = host_dir / "backend"
            steps: list[list[str]] = [
                ["mvn", "-B", "test", f"-Dtest={clean_sel}", "-DfailIfNoTests=true"]
            ]
        else:
            sub_dir = host_dir / "frontend"
            steps = [
                ["npx", "vue-tsc", "--noEmit"],
                ["npx", "vitest", "run", clean_sel],
            ]

        if not sub_dir.is_dir():
            return ModuleVerificationResult(
                artifact_dir=str(artifact_dir),
                module_id=clean_mod,
                stack=stack,
                test_selector=clean_sel,
                status="FAIL",
                verifier_kind="local_process",
                error_code="BUILD_FAILED",
                duration_ms=int((time.monotonic() - started) * 1000),
                command=steps[0],
                detail=f"模块工作目录缺失：{sub_dir.name}/",
            )

        logs: list[str] = []
        deadline = started + timeout_seconds
        last_cmd = steps[0]
        for argv in steps:
            last_cmd = argv
            remaining = max(0.5, deadline - time.monotonic())
            code, output = self._run_whitelisted_command(argv, cwd=sub_dir, timeout_seconds=remaining)
            logs.append(output)
            if code != 0:
                combined = "\n".join(logs)
                err_code = _classify_local_failure(code, combined)
                status: Literal["FAIL", "NOT_RUN"] = (
                    "NOT_RUN" if err_code == "INFRASTRUCTURE" else "FAIL"
                )
                detail = "单模块本地快检超时并已强杀进程树" if code == TIMEOUT_EXIT_CODE else None
                surefire = (
                    extract_surefire_failure_summary(host_dir)
                    if stack == "java" and err_code != "INFRASTRUCTURE"
                    else ""
                )
                effective_log = f"{surefire}\n\n[maven-summary]\n{combined}" if surefire else combined
                return ModuleVerificationResult(
                    artifact_dir=str(artifact_dir),
                    module_id=clean_mod,
                    stack=stack,
                    test_selector=clean_sel,
                    status=status,
                    verifier_kind="local_process",
                    error_code=err_code,
                    exit_code=code,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    command=last_cmd,
                    detail=detail,
                    log_excerpt=redact_and_truncate_log(effective_log, limit=self._log_limit),
                )

        combined = "\n".join(logs)
        excerpt = redact_and_truncate_log(combined, limit=self._log_limit)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if stack == "java" and not re.search(r"Tests run:\s*[1-9]", combined):
            return ModuleVerificationResult(
                artifact_dir=str(artifact_dir),
                module_id=clean_mod,
                stack=stack,
                test_selector=clean_sel,
                status="NOT_RUN",
                verifier_kind="local_process",
                error_code="NO_TESTS",
                exit_code=0,
                duration_ms=elapsed_ms,
                command=last_cmd,
                detail="未执行任何匹配单测",
                log_excerpt=excerpt,
            )

        return ModuleVerificationResult(
            artifact_dir=str(artifact_dir),
            module_id=clean_mod,
            stack=stack,
            test_selector=clean_sel,
            status="PASS",
            verifier_kind="local_process",
            exit_code=0,
            duration_ms=elapsed_ms,
            command=last_cmd,
            log_excerpt=excerpt,
        )


def require_docker_runner_for_final_acceptance(runner_client: Any) -> RunnerClient:
    """总验收安全守卫：强制要求 Docker Runner（``HttpRunnerClient`` 等），严禁 ``LocalProcessModuleVerifier``。"""
    if isinstance(runner_client, LocalProcessModuleVerifier) or (
        getattr(runner_client, "verifier_kind", None) == "local_process"
    ):
        raise AppException(
            ErrorCode.FORBIDDEN,
            "安全策略拒绝：后端总装验收与全栈最终验收强制走独立 Docker Runner，严禁使用 LocalProcessModuleVerifier",
            status_code=403,
        )
    if not (callable(getattr(runner_client, "verify", None)) and callable(getattr(runner_client, "accept", None))):
        raise AppException(
            ErrorCode.FORBIDDEN,
            "总验收客户端必须实现 Docker RunnerClient 协议（verify + accept）",
            status_code=403,
        )
    return runner_client


def _verify_accepts_checks(verify_fn: Any) -> bool:
    try:
        sig = inspect.signature(verify_fn)
    except (TypeError, ValueError):
        return False
    if "checks" in sig.parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


def run_final_acceptance_verify(
    runner_client: HttpRunnerClient | RunnerClient,
    artifact_dir: str,
    *,
    checks: Sequence[str] | None = None,
) -> dict:
    """后端总装 / 全栈最终验证入口（硬编码强制 Docker Runner）。"""
    client = require_docker_runner_for_final_acceptance(runner_client)
    if checks is not None and _verify_accepts_checks(client.verify):
        return client.verify(artifact_dir, checks=list(checks))  # type: ignore[call-arg]
    return client.verify(artifact_dir)


def run_final_acceptance_accept(runner_client: HttpRunnerClient | RunnerClient, artifact_dir: str) -> dict:
    """全栈端到端验收入口（硬编码强制 Docker Runner）。"""
    client = require_docker_runner_for_final_acceptance(runner_client)
    return client.accept(artifact_dir)


def build_module_verifier(
    *,
    runner_client: HttpRunnerClient | Any | None = None,
    work_root: str | Path | None = None,
) -> ModuleVerifier:
    """按开关构造单模块快检器：默认返回 ``DockerRunnerModuleVerifier``；仅显式开启开关时返回本地受限快检器。"""
    if local_dept_test_runner_enabled():
        if work_root is None:
            raise AppException(
                ErrorCode.CONFIG_REQUIRED,
                "开启本地受限子进程快检时必须提供受控 work_root",
                status_code=422,
            )
        return LocalProcessModuleVerifier(work_root=work_root)
    if runner_client is None:
        raise AppException(
            ErrorCode.CONFIG_REQUIRED,
            "默认 DockerRunnerModuleVerifier 需要提供 HttpRunnerClient",
            status_code=422,
        )
    return DockerRunnerModuleVerifier(runner_client, work_root=work_root)


__all__ = [
    "ALLOWED_LOCAL_COMMANDS",
    "DEFAULT_MODULE_TIMEOUT_SECONDS",
    "ENV_WHITELIST",
    "LOCAL_DEPT_TEST_RUNNER_ENV",
    "LOG_LIMIT",
    "MAX_MODULE_TIMEOUT_SECONDS",
    "TIMEOUT_EXIT_CODE",
    "DockerRunnerModuleVerifier",
    "LocalProcessModuleVerifier",
    "ModuleVerificationResult",
    "ModuleVerifier",
    "build_module_verifier",
    "build_sanitized_env",
    "extract_surefire_failure_summary",
    "local_dept_test_runner_enabled",
    "redact_and_truncate_log",
    "require_docker_runner_for_final_acceptance",
    "run_final_acceptance_accept",
    "run_final_acceptance_verify",
    "to_runner_artifact_dir",
    "validate_module_id",
    "validate_test_selector",
]
