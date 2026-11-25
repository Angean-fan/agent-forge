"""M17 校验管线：静态清单检查 + 后端构建/测试 + 前端构建。

只运行**固定任务**；输出结构化 PASS/FAIL/NOT_RUN 并做失败分类：
构建/测试错误 → `FAIL/BUILD_FAILED`；依赖离线、命令缺失、镜像缺失等基础设施故障 → `NOT_RUN/INFRASTRUCTURE`；
缺少测试执行 → `NOT_RUN/NO_TESTS`。日志脱敏并截断。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agentforge_runner.config import RunnerConfig
from agentforge_runner.container import build_run_argv, container_runtime_available, run_container
from agentforge_runner.tasks import get_task

_STRICT = ConfigDict(extra="forbid")
LOG_LIMIT = 4000

STATIC_REQUIRED = [
    "backend/pom.xml",
    "frontend/package.json",
    "frontend/package-lock.json",
    "compose.yaml",
    "asset-lock.json",
]
BACKEND_STATIC_REQUIRED = [
    "backend/pom.xml",
]
STATIC_GLOBS = ["backend/src/main/resources/db/migration/*.sql"]
CONTAINER_CHECKS = ["backend_test", "backend_build", "frontend_build"]
ALL_CHECKS = ["static_manifest", *CONTAINER_CHECKS]


class CheckResult(BaseModel):
    model_config = _STRICT

    name: str
    status: str
    error_code: str | None = None
    exit_code: int | None = None
    detail: str | None = None
    log_excerpt: str | None = None


class VerificationResult(BaseModel):
    model_config = _STRICT

    artifact_dir: str
    overall: str
    checks: list[CheckResult]


_SECRET_PATTERNS = [
    (re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key)\s*[:=]\s*\S+"), r"\1=***"),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "<jwt>"),
]
_INFRA_MARKERS = (
    "Could not resolve dependencies",
    "Could not transfer artifact",
    "Could not find artifact",
    "Non-resolvable",
    "Failed to read artifact descriptor",
    "Connection refused",
    "UnknownHostException",
    "Network is unreachable",
    "Temporary failure in name resolution",
    "ENOTFOUND",
    "ETIMEDOUT",
    "ECONNREFUSED",
    "ERR_SOCKET_TIMEOUT",
    "getaddrinfo",
    "command not found",
    "executable file not found",
    "Unable to find image",
    "offline mode",
)


_NOISE_LINE_PATTERNS = (
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
    """剔除容器只读 /tmp(noexec) 导致的 jansi 及 Maven Help 样板等无害噪音，避免挤占诊断日志配额。"""
    lines = text.splitlines()
    filtered = [line for line in lines if not any(p.search(line) for p in _NOISE_LINE_PATTERNS)]
    return "\n".join(filtered)


def extract_surefire_failure_summary(host_dir: Path, *, max_chars: int = 2800) -> str:
    """直接读取 ``backend/target/surefire-reports/*.txt`` 提取失败断言、期望/实际值与业务堆栈。"""
    reports_dir = host_dir / "backend" / "target" / "surefire-reports"
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
                # 堆栈结束后若遇到空行或下一段
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


def format_java_failure_excerpt(host_dir: Path, raw_output: str, *, limit: int = LOG_LIMIT) -> str:
    """Java 单测失败时优先拼装 Surefire 精确断言摘要，确保关键 expected/but was 与行号不被截断。"""
    surefire = extract_surefire_failure_summary(host_dir, max_chars=min(2800, max(600, limit - 400)))
    redacted_output = redact(raw_output)
    if not surefire:
        return truncate(redacted_output, limit=limit)
    redacted_surefire = redact(surefire)
    remaining = limit - len(redacted_surefire) - 24
    if remaining <= 200:
        return redacted_surefire[:limit]
    tail_excerpt = truncate(redacted_output, limit=remaining)
    return f"{redacted_surefire}\n\n[maven-summary]\n{tail_excerpt}"


def _extract_error_focused_excerpt(text: str, limit: int) -> str | None:
    """当日志包含编译/单测失败特征时，优先提取关键报错段落，避免头尾截断吞掉根因。"""
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
    marker = "\n...[truncated]...\n"
    if len(extracted) <= limit:
        return f"[error-focused excerpt]\n...[truncated {max(0, len(text) - len(extracted))} chars]...\n{extracted}"
    budget = max(0, limit - len(marker) - 32)
    head = budget * 2 // 3
    tail = budget - head
    return (
        f"[error-focused excerpt]\n{extracted[:head]}"
        f"\n...[truncated {max(0, len(text) - limit)} chars]...\n"
        f"{extracted[-tail:] if tail > 0 else ''}"
    )


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def truncate(text: str, limit: int = LOG_LIMIT) -> str:
    cleaned = _strip_noise_lines(text)
    if len(cleaned) <= limit:
        return cleaned
    focused = _extract_error_focused_excerpt(cleaned, limit)
    if focused is not None:
        return focused
    head = limit * 2 // 3
    tail = limit - head
    return f"{cleaned[:head]}\n...[truncated {len(cleaned) - limit} chars]...\n{cleaned[-tail:]}"


def classify_failure(exit_code: int, log: str) -> str:
    if exit_code == 124:
        return "TIMEOUT"
    if any(marker in log for marker in _INFRA_MARKERS):
        return "INFRASTRUCTURE"
    return "BUILD_FAILED"


def run_static_check(artifact_dir: Path) -> CheckResult:
    missing = [name for name in STATIC_REQUIRED if not (artifact_dir / name).is_file()]
    missing += [glob for glob in STATIC_GLOBS if not list(artifact_dir.glob(glob))]
    if missing:
        return CheckResult(
            name="static_manifest",
            status="FAIL",
            error_code="MISSING_FILES",
            detail="缺失：" + "、".join(missing),
        )
    return CheckResult(name="static_manifest", status="PASS")


def run_backend_static_check(artifact_dir: Path) -> CheckResult:
    """工厂后端总装阶段专用静态清单校验（不强求尚未生成的前端文件）。"""
    missing = [name for name in BACKEND_STATIC_REQUIRED if not (artifact_dir / name).is_file()]
    missing += [glob for glob in STATIC_GLOBS if not list(artifact_dir.glob(glob))]
    if missing:
        return CheckResult(
            name="backend_static_manifest",
            status="FAIL",
            error_code="MISSING_FILES",
            detail="缺失：" + "、".join(missing),
        )
    return CheckResult(name="backend_static_manifest", status="PASS")


def run_container_check(name: str, host_dir: Path, config: RunnerConfig) -> CheckResult:
    task = get_task(name)
    if task is None:
        return CheckResult(name=name, status="NOT_RUN", error_code="UNKNOWN_CHECK", detail="未登记的检查")
    if not container_runtime_available():
        return CheckResult(
            name=name, status="NOT_RUN", error_code="INFRASTRUCTURE", detail="受控执行环境不可用"
        )

    container = f"af-run-{uuid.uuid4().hex[:12]}"
    argv = build_run_argv(task, host_dir, config, container)
    exit_code, output = run_container(argv, timeout=task.timeout_seconds, container_name=container)
    if exit_code != 0 and name == "backend_test":
        excerpt = format_java_failure_excerpt(host_dir, output)
    else:
        excerpt = truncate(redact(output))

    if exit_code == 0:
        if name == "backend_test" and not re.search(r"Tests run:\s*[1-9]", output):
            return CheckResult(
                name=name, status="NOT_RUN", error_code="NO_TESTS", detail="未执行任何测试", log_excerpt=excerpt
            )
        return CheckResult(name=name, status="PASS", exit_code=0, log_excerpt=excerpt)

    error_code = classify_failure(exit_code, output)
    status = "NOT_RUN" if error_code == "INFRASTRUCTURE" else "FAIL"
    return CheckResult(name=name, status=status, error_code=error_code, exit_code=exit_code, log_excerpt=excerpt)


def run_verification(artifact_dir: Path, config: RunnerConfig, names: list[str]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for name in names:
        if name == "static_manifest":
            results.append(run_static_check(artifact_dir))
        elif name == "backend_static_manifest":
            results.append(run_backend_static_check(artifact_dir))
        elif name in CONTAINER_CHECKS:
            results.append(run_container_check(name, artifact_dir, config))
        else:
            results.append(CheckResult(name=name, status="NOT_RUN", error_code="UNKNOWN_CHECK", detail="未登记的检查"))
    return results


def overall_of(results: list[CheckResult]) -> str:
    if any(result.status == "FAIL" for result in results):
        return "FAIL"
    if any(result.status == "NOT_RUN" for result in results):
        return "NOT_RUN"
    return "PASS"
