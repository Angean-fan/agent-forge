"""R13：技术规划——确定性优先，仅在**真实规划复杂性**时经 `technical_planning`（Qwen3.7 Plus）提议 TaskPlan。

- 规划前先执行 Asset Finder（R06C），向模型只提供**已校验的候选/缺口摘要**（不提供原始聊天、不提供未确认内容）；
- 模型只能**提议**能力与任务；Python 冻结、校验并决定最终 DAG（:func:`task_plan.validate_plan`）；
- 已覆盖/无复杂性 → 直接确定性转换（与既有生成图一致），**跳过技术模型**；
- 路由未启用或调用/校验失败 → 明确回退到确定性计划并记录原因（不伪装成功）。
"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from agentforge_platform.asset_finder import AssetSearchResult
from agentforge_platform.clients import DashScopeClient, ModelClient
from agentforge_platform.contract import BusinessContract
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.model_gateway import invoke
from agentforge_platform.models import Project
from agentforge_platform.resolver import AssetLock
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.task_plan import ExecutionMode, TaskPlan, TaskSpec, validate_plan


class _ModelPlan(BaseModel):
    """模型仅提议任务列表；``execution_mode``/``input_digest_hash`` 一律由 Python 决定。"""

    model_config = ConfigDict(extra="forbid")

    tasks: list[TaskSpec]

_MAX_OUTPUT_TOKENS = 2048
_MAX_INPUT_TOKENS = 6000
# 既有生成图固定任务（确定性转换使用）。
_FIXED_TASKS = ["load_confirmed_spec", "resolve_assets", "assemble", "verify", "package_or_fail"]


def needs_planning(search: AssetSearchResult, spec: SoftwareSpecification) -> bool:
    """规划复杂性：存在未覆盖验收/缺口、显式请求方法单元、或关系较多（>1）。"""
    return (
        bool(search.uncovered_acceptance_ids)
        or bool(spec.method_requests)
        or len(spec.relations) > 1
    )


def input_digest(
    spec: SoftwareSpecification, contract: BusinessContract, search: AssetSearchResult
) -> str:
    payload = {
        "spec_hash": contract.spec_hash,
        "contract_hash": contract.contract_hash,
        "query_hash": search.query_hash,
        "index_revision": search.index_revision,
        "selected_refs": list(search.selected_refs),
        "uncovered_acceptance_ids": list(search.uncovered_acceptance_ids),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _asset_refs(lock: AssetLock) -> list[str]:
    refs = [f"{asset.kind}:{asset.id}@{asset.version}" for asset in lock.assets]
    refs += [f"method-unit:{unit.id}@{unit.version}" for unit in lock.method_units]
    return sorted(refs)


def deterministic_plan(
    spec: SoftwareSpecification, contract: BusinessContract, search: AssetSearchResult, lock: AssetLock
) -> TaskPlan:
    """确定性转换：与既有生成图一致的任务链，引用冻结资产与检索拒绝原因。"""
    refs = _asset_refs(lock)
    templates = [
        task for task in _FIXED_TASKS
    ]
    tasks = [
        TaskSpec(task_id=templates[0], operation="load_spec", mode="structured_edit"),
        TaskSpec(task_id=templates[1], operation="resolve_assets",
                 depends_on=[templates[0]], asset_refs=refs, mode="copy"),
        TaskSpec(task_id=templates[2], operation="assemble",
                 depends_on=[templates[1]], asset_refs=refs,
                 allowed_paths=["backend/", "frontend/"], mode="render"),
        TaskSpec(task_id=templates[3], operation="verify",
                 depends_on=[templates[2]], mode="structured_edit"),
        TaskSpec(task_id=templates[4], operation="package",
                 depends_on=[templates[3]], mode="structured_edit"),
    ]
    return TaskPlan(
        execution_mode=ExecutionMode.DETERMINISTIC,
        input_digest_hash=input_digest(spec, contract, search),
        tasks=tasks,
        retrieval_reasons={ref: list(reasons) for ref, reasons in search.filter_reasons.items()},
        uncovered_acceptance_ids=list(search.uncovered_acceptance_ids),
    )


def _prompt(spec: SoftwareSpecification, contract: BusinessContract, search: AssetSearchResult) -> str:
    selected = "\n".join(search.selected_refs) or "（无）"
    uncovered = "、".join(search.uncovered_acceptance_ids) or "（无）"
    entities = "、".join(matrix.entity for matrix in contract.permissions)
    return (
        "你是生成任务规划助手。仅根据给定的**已冻结规格与已校验资产摘要**提出 TaskPlan JSON；"
        "不得修改规格、权限、验收或依赖版本；asset_refs 只能取自候选资产列表。\n"
        f"业务实体：{entities}\n"
        f"候选资产（可引用）：\n{selected}\n"
        f"未覆盖验收：{uncovered}\n"
        "输出 JSON：{{\"tasks\":[{{\"task_id\",\"operation\",\"depends_on\",\"allowed_paths\","
        "\"asset_refs\",\"acceptance_ids\",\"mode\"}}]}}；mode 取 copy/render/structured_edit/agent_gap。"
    )


def model_plan(
    db: Session,
    project: Project,
    spec: SoftwareSpecification,
    contract: BusinessContract,
    search: AssetSearchResult,
    lock: AssetLock,
    *,
    run_id: int,
    client: ModelClient,
) -> TaskPlan:
    """经 `technical_planning` 路由提议 TaskPlan；DAG 建立前调用，`task_id` 留空。"""
    prompt = _prompt(spec, contract, search)
    completion = invoke(
        db,
        call_id=uuid4().hex,
        model_role="technical_planning",
        client=client,
        prompt=prompt,
        project_id=project.id,
        run_id=run_id,
        task_id=None,  # DAG 建立前：保留空 task_id
        max_input_tokens=max(_MAX_INPUT_TOKENS, len(prompt.encode("utf-8")) + 1024),
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        thinking_mode="disabled",
        json_mode=True,
    )
    if completion.finish_reason != "stop":
        raise AppException("EXTRACTION_INVALID", "技术规划输出未完成", status_code=422)
    try:
        model_plan_payload = _ModelPlan.model_validate_json(completion.content)
    except ValidationError as exc:
        raise AppException("PLAN_INVALID", "模型 TaskPlan 结构非法", status_code=422) from exc
    return TaskPlan(
        execution_mode=ExecutionMode.PLANNED,
        input_digest_hash="",
        tasks=model_plan_payload.tasks,
    )


def plan_tasks(
    db: Session,
    project: Project,
    spec: SoftwareSpecification,
    contract: BusinessContract,
    search: AssetSearchResult,
    lock: AssetLock,
    *,
    run_id: int,
    client: ModelClient | None = None,
) -> tuple[TaskPlan, list[str]]:
    """返回 ``(最终计划, 回退原因)``：确定性优先；仅在需要规划时调用模型，失败即回退并记录原因。"""
    if not needs_planning(search, spec):
        return deterministic_plan(spec, contract, search, lock), []

    provider = client or DashScopeClient()
    known_refs = set(_asset_refs(lock))
    known_acceptance = {case.id for case in spec.acceptance_cases}
    try:
        plan = model_plan(db, project, spec, contract, search, lock, run_id=run_id, client=provider)
        issues = validate_plan(
            plan, known_asset_refs=known_refs, known_acceptance_ids=known_acceptance
        )
        if issues:
            raise AppException("PLAN_INVALID", "；".join(issues), status_code=422)
    except AppException as exc:
        # R24/R21：预算/调用/输出护栏**不属于可回退的规划失败**——须向上传播（触发暂停或拦截），
        # 不得静默回退到确定性计划而绕过预算与次数限制。
        if exc.code in (
            ErrorCode.BUDGET_EXCEEDED, ErrorCode.CALL_LIMIT_EXCEEDED, ErrorCode.OUTPUT_LIMIT_EXCEEDED
        ):
            raise
        fallback = deterministic_plan(spec, contract, search, lock)
        reasons = dict(fallback.retrieval_reasons)
        reasons["technical_planning"] = [str(exc.code)]
        return fallback.model_copy(update={"retrieval_reasons": reasons}), [str(exc.code)]

    return plan.model_copy(
        update={
            "execution_mode": ExecutionMode.PLANNED,
            "input_digest_hash": input_digest(spec, contract, search),
            "retrieval_reasons": {ref: list(r) for ref, r in search.filter_reasons.items()},
            "uncovered_acceptance_ids": list(search.uncovered_acceptance_ids),
        }
    ), []


__all__ = [
    "deterministic_plan",
    "input_digest",
    "model_plan",
    "needs_planning",
    "plan_tasks",
]
