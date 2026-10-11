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


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def truncate(text: str, limit: int = LOG_LIMIT) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n...[truncated {len(text) - limit} chars]...\n{text[-tail:]}"


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
