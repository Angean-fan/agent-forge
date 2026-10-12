"""R13：接口契约先行的 ``TaskPlan`` 契约与 Python 侧**确定性校验**。

模型（技术规划）只能**提议**任务；DAG 的**无环、越界路径、同路径冲突、资产引用、覆盖**一律由本模块校验，
最终执行计划由 Python 冻结决定。校验失败即拒绝，不交由模型自评。
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

PLAN_VERSION = "0.1"
# 仅允许已声明的安全执行模式；新写（agent_gap）在 D4 之前一律转为缺口，不自动执行。
TASK_MODES = frozenset({"copy", "render", "structured_edit", "agent_gap"})
_PATH = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/\-]*\Z")

_STRICT = ConfigDict(extra="forbid")


class ExecutionMode(StrEnum):
    DETERMINISTIC = "deterministic"
    PLANNED = "planned"


class TaskSpec(BaseModel):
    model_config = _STRICT

    task_id: str
    operation: str
    depends_on: list[str] = []
    allowed_paths: list[str] = []
    asset_refs: list[str] = []
    acceptance_ids: list[str] = []
    mode: str = "copy"


class TaskPlan(BaseModel):
    model_config = _STRICT

    plan_version: str = PLAN_VERSION
    execution_mode: str
    input_digest_hash: str
    tasks: list[TaskSpec]
    # 检索拒绝原因（候选 → 理由），供可追溯审计。
    retrieval_reasons: dict[str, list[str]] = {}
    uncovered_acceptance_ids: list[str] = []


def is_safe_task_path(path: str) -> bool:
    """任务 allowed_paths 只允许**相对、无 ``..``、无反斜杠、非绝对**的安装范围。"""
    if not path or path.startswith(("/", "~")) or "\\" in path:
        return False
    stripped = path.rstrip("/")
    if not stripped or re.match(r"^[A-Za-z]:", stripped):
        return False
    if _PATH.fullmatch(stripped) is None:
        return False
    return any(part == ".." for part in stripped.split("/")) is False


def _overlaps(left: str, right: str) -> bool:
    a, b = left.rstrip("/"), right.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _has_cycle(tasks: list[TaskSpec]) -> bool:
    remaining = {task.task_id for task in tasks}
    deps = {task.task_id: set(task.depends_on) for task in tasks}
    while remaining:
        ready = [task_id for task_id in remaining if not (deps[task_id] & remaining)]
        if not ready:
            return True
        for task_id in ready:
            remaining.discard(task_id)
    return False


def validate_plan(
    plan: TaskPlan, *, known_asset_refs: set[str], known_acceptance_ids: set[str]
) -> list[str]:
    """返回校验问题（空 = 通过）：task_id 唯一/依赖存在/无环、路径安全、无同路径冲突、资产引用有效。"""
    issues: list[str] = []
    ids = [task.task_id for task in plan.tasks]
    if len(set(ids)) != len(ids):
        issues.append("task_id 重复")
    idset = set(ids)
    for task in plan.tasks:
        for dependency in task.depends_on:
            if dependency not in idset:
                issues.append(f"{task.task_id} 依赖未知任务：{dependency}")
        for path in task.allowed_paths:
            if not is_safe_task_path(path):
                issues.append(f"{task.task_id} 越界 allowed_paths：{path}")
        for ref in task.asset_refs:
            if ref not in known_asset_refs:
                issues.append(f"{task.task_id} 引用了未锁定资产：{ref}")
        if task.mode not in TASK_MODES:
            issues.append(f"{task.task_id} 未知执行模式：{task.mode}")
    if _has_cycle(plan.tasks):
        issues.append("任务 DAG 存在环")
    for index, left in enumerate(plan.tasks):
        for right in plan.tasks[index + 1:]:
            for left_path in left.allowed_paths:
                for right_path in right.allowed_paths:
                    if _overlaps(left_path, right_path):
                        issues.append(
                            f"同路径冲突：{left.task_id} 的 {left_path} 与 {right.task_id} 的 {right_path}"
                        )
    return issues


__all__ = [
    "PLAN_VERSION",
    "TASK_MODES",
    "ExecutionMode",
    "TaskPlan",
    "TaskSpec",
    "is_safe_task_path",
    "validate_plan",
]
