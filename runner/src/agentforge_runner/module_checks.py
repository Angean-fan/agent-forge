"""A08：Runner 单模块定向快检（`POST /verify-module`）。

仅执行单模块定向编译与单测：
- ``java``：``mvn -B -o -Dmaven.repo.local=/opt/agentforge/maven-cache test -Dtest=<selector> -DfailIfNoTests=true``
- ``frontend``：两段无 shell 受控调用（``npx vue-tsc --noEmit`` → ``npx vitest run <selector>``）

所有参数经严格正则白名单校验，杜绝命令注入、选项注入与路径穿越；
实际执行仅通过受限 ``docker run``（无网络、只读根、丢弃全部 capability、超时强杀清理）。
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentforge_runner.checks import classify_failure, redact, truncate
from agentforge_runner.config import RunnerConfig
from agentforge_runner.container import (
    TIMEOUT_EXIT_CODE,
    build_run_argv,
    container_runtime_available,
    run_container,
)
from agentforge_runner.tasks import Task

_STRICT = ConfigDict(extra="forbid")

_SAFE_MODULE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SAFE_JAVA_SELECTOR = re.compile(r"^[A-Za-z0-9_.*#,]+$")
_SAFE_FRONTEND_SELECTOR = re.compile(r"^[A-Za-z0-9_./-]+$")
_FORBIDDEN_SELECTOR_SUBSTRINGS = ("..", ";", "|", "&", "$", "`", ">", "<", "\n", "\r", "\x00", " ")

DEFAULT_MODULE_TIMEOUT = 120
MIN_MODULE_TIMEOUT = 5
MAX_MODULE_TIMEOUT = 600


def validate_module_id(module_id: str) -> str:
    cleaned = module_id.strip()
    if not _SAFE_MODULE_ID.fullmatch(cleaned):
        raise ValueError(f"非法 module_id：{module_id!r}")
    return cleaned


def validate_module_selector(stack: str, test_selector: str) -> str:
    cleaned = test_selector.strip()
    if not cleaned or len(cleaned) > 200:
        raise ValueError("test_selector 不能为空且长度不得超过 200")
    if any(bad in cleaned for bad in _FORBIDDEN_SELECTOR_SUBSTRINGS):
        raise ValueError(f"test_selector 包含禁止字符或路径穿越：{test_selector!r}")
    if cleaned.startswith(("-", "/")):
        raise ValueError(f"test_selector 不得以 '-' 或 '/' 开头：{test_selector!r}")

    if stack == "java":
        if not _SAFE_JAVA_SELECTOR.fullmatch(cleaned):
            raise ValueError(f"非法 Java 测试选择器：{test_selector!r}")
        for part in cleaned.split(","):
            if not part or part.startswith(("-", ".")) or part.endswith("."):
                raise ValueError(f"非法 Java 测试选择器片段：{part!r}")
        return cleaned

    if stack == "frontend":
        if not _SAFE_FRONTEND_SELECTOR.fullmatch(cleaned):
            raise ValueError(f"非法前端测试选择器：{test_selector!r}")
        return cleaned

    raise ValueError(f"不支持的技术栈：{stack!r}")


class ModuleVerifyRequest(BaseModel):
    model_config = _STRICT

    artifact_dir: str
    module_id: str
    stack: Literal["java", "frontend"]
    test_selector: str
    timeout_seconds: int = Field(
        default=DEFAULT_MODULE_TIMEOUT,
        ge=MIN_MODULE_TIMEOUT,
        le=MAX_MODULE_TIMEOUT,
    )

    @field_validator("module_id")
    @classmethod
    def _check_module_id(cls, value: str) -> str:
        return validate_module_id(value)

    @field_validator("test_selector")
    @classmethod
    def _check_selector(cls, value: str, info) -> str:
        stack = info.data.get("stack")
        if stack in {"java", "frontend"}:
            return validate_module_selector(stack, value)
        # 若 stack 字段尚未通过校验，先做基础注入拦截
        cleaned = value.strip()
        if not cleaned or any(bad in cleaned for bad in _FORBIDDEN_SELECTOR_SUBSTRINGS):
            raise ValueError(f"非法 test_selector：{value!r}")
        return cleaned


class ModuleVerificationResult(BaseModel):
    model_config = _STRICT

    artifact_dir: str
    module_id: str
    stack: Literal["java", "frontend"]
    test_selector: str
    status: Literal["PASS", "FAIL", "NOT_RUN"]
    error_code: str | None = None
    exit_code: int | None = None
    duration_ms: int = 0
    command: list[str] = []
    detail: str | None = None
    log_excerpt: str | None = None


def build_module_tasks(
    *,
    module_id: str,
    stack: Literal["java", "frontend"],
    test_selector: str,
    timeout_seconds: int = DEFAULT_MODULE_TIMEOUT,
) -> list[Task]:
    """为单模块快检构造无 shell 固定 argv 任务列表。"""
    clean_module = validate_module_id(module_id)
    clean_selector = validate_module_selector(stack, test_selector)

    if stack == "java":
        return [
            Task(
                name=f"module_java_{clean_module}",
                workdir="/workspace/backend",
                command=[
                    "mvn",
                    "-B",
                    "-o",
                    "-Dmaven.repo.local=/opt/agentforge/maven-cache",
                    "test",
                    f"-Dtest={clean_selector}",
                    "-DfailIfNoTests=true",
                ],
                timeout_seconds=timeout_seconds,
                description=f"单模块 Java 定向快检：{clean_module}",
            )
        ]

    return [
        Task(
            name=f"module_frontend_tsc_{clean_module}",
            workdir="/workspace/frontend",
            command=["npx", "vue-tsc", "--noEmit"],
            timeout_seconds=timeout_seconds,
            description=f"单模块前端类型检查：{clean_module}",
        ),
        Task(
            name=f"module_frontend_vitest_{clean_module}",
            workdir="/workspace/frontend",
            command=["npx", "vitest", "run", clean_selector],
            timeout_seconds=timeout_seconds,
            description=f"单模块前端定向单测：{clean_module}",
        ),
    ]


def run_module_verification(
    host_dir: Path,
    config: RunnerConfig,
    request: ModuleVerifyRequest,
) -> ModuleVerificationResult:
    """在受控 Docker 容器内执行单模块快检并返回结构化结果。"""
    started = time.monotonic()
    tasks = build_module_tasks(
        module_id=request.module_id,
        stack=request.stack,
        test_selector=request.test_selector,
        timeout_seconds=request.timeout_seconds,
    )
    executed_cmd = list(tasks[-1].command)

    if not container_runtime_available():
        return ModuleVerificationResult(
            artifact_dir=request.artifact_dir,
            module_id=request.module_id,
            stack=request.stack,
            test_selector=request.test_selector,
            status="NOT_RUN",
            error_code="INFRASTRUCTURE",
            duration_ms=int((time.monotonic() - started) * 1000),
            command=executed_cmd,
            detail="受控执行环境不可用（无 Docker）",
        )

    logs: list[str] = []
    deadline = started + request.timeout_seconds

    for task in tasks:
        remaining = max(1, int(deadline - time.monotonic()))
        container_name = f"af-mod-{request.module_id[:16]}-{uuid.uuid4().hex[:10]}"
        argv = build_run_argv(task, host_dir, config, container_name)
        exit_code, output = run_container(argv, timeout=remaining, container_name=container_name)
        logs.append(output)
        executed_cmd = list(task.command)

        if exit_code != 0:
            combined = "\n".join(logs)
            error_code = classify_failure(exit_code, combined)
            status: Literal["FAIL", "NOT_RUN"] = (
                "NOT_RUN" if error_code == "INFRASTRUCTURE" else "FAIL"
            )
            detail = "单模块快检超时并已清理容器" if exit_code == TIMEOUT_EXIT_CODE else None
            return ModuleVerificationResult(
                artifact_dir=request.artifact_dir,
                module_id=request.module_id,
                stack=request.stack,
                test_selector=request.test_selector,
                status=status,
                error_code=error_code,
                exit_code=exit_code,
                duration_ms=int((time.monotonic() - started) * 1000),
                command=executed_cmd,
                detail=detail,
                log_excerpt=truncate(redact(combined)),
            )

    combined = "\n".join(logs)
    excerpt = truncate(redact(combined))
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if request.stack == "java" and not re.search(r"Tests run:\s*[1-9]", combined):
        return ModuleVerificationResult(
            artifact_dir=request.artifact_dir,
            module_id=request.module_id,
            stack=request.stack,
            test_selector=request.test_selector,
            status="NOT_RUN",
            error_code="NO_TESTS",
            exit_code=0,
            duration_ms=elapsed_ms,
            command=executed_cmd,
            detail="未执行任何匹配单测",
            log_excerpt=excerpt,
        )

    return ModuleVerificationResult(
        artifact_dir=request.artifact_dir,
        module_id=request.module_id,
        stack=request.stack,
        test_selector=request.test_selector,
        status="PASS",
        exit_code=0,
        duration_ms=elapsed_ms,
        command=executed_cmd,
        log_excerpt=excerpt,
    )


__all__ = [
    "DEFAULT_MODULE_TIMEOUT",
    "MAX_MODULE_TIMEOUT",
    "MIN_MODULE_TIMEOUT",
    "ModuleVerificationResult",
    "ModuleVerifyRequest",
    "build_module_tasks",
    "run_module_verification",
    "validate_module_id",
    "validate_module_selector",
]
