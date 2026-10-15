"""C04：仅有供应商文档时的**待确认集成规格 → 受限 Adapter**真实链路（E07/S3）。

把 E07 的规则式提取、待确认门禁、注入防护与 Adapter 授权边界，串成一条可执行链路：

``不可信文档 →（规则式）待确认 Integration Spec → DeepSeek 提问/用户确认 → 冻结集成契约 →
（Qwen）受限 Adapter 任务（仅 integration/adapter/**）→ Mock 契约校验``

约束（对齐《AgentForge_03》C04）：

- 首个用例仅模拟服务：**无凭据、无真实收费或副作用**；供应商文档/OpenAPI 视为**数据**，
  其"指令"（提示注入）不改权限/SDK；
- 来源不全、鉴权/回调不明 → 保持 pending 并**暂停**；已确认前不冻结；
- 缺凭据时 DeepSeek 提问调用记 ``NOT_RUN`` 且**保留草稿**，不伪造成功；
- Adapter 仅可改 ``integration/adapter/**``（越界即拒），**不自动安装未知 SDK、不宣称 Live Verified**；
- 新 Adapter 仅属当前项目，**不自动发布公共资产**；本模块不含任何真实模型调用（缝可注入）。
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy.orm import Session

from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.integration_doc import (
    Doc,
    FrozenIntegrationSpec,
    IntegrationSpecDraft,
    adapter_edit_issues,
    extract_integration_spec,
    freeze_integration_spec,
    render_questions,
)
from agentforge_platform.model_gateway import invoke

FLAG_ENV = "AGENTFORGE_INTEGRATION_DOC_ENABLED"
ADAPTER_TASK_ID = "integration:adapter"
# 生成工程内 Adapter 的**唯一**可写目录（相对工程根）；越界即拒。
ADAPTER_ALLOWED_PATHS: tuple[str, ...] = (
    "backend/src/main/java/com/agentforge/integration/adapter/",
)

STATUS_OK = "OK"
STATUS_DISABLED = "DISABLED"
STATUS_NOT_RUN = "NOT_RUN"

_MAX_INPUT_TOKENS = 4000
_MAX_OUTPUT_TOKENS = 1024


def onboarding_enabled() -> bool:
    """集成文档接入是否开启（默认关闭 → 普通 Task 路径零漂移）。"""
    return os.environ.get(FLAG_ENV, "").strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class OnboardingResult:
    """一次接入会话：草稿 + 问题清单 + DeepSeek 状态 +（确认后）冻结契约。"""

    provider: str
    draft: IntegrationSpecDraft
    questions: str
    explain_status: str = STATUS_DISABLED
    frozen: FrozenIntegrationSpec | None = None

    @property
    def pending_ids(self) -> list[str]:
        return [question.qid for question in self.draft.pending]

    @property
    def is_frozen(self) -> bool:
        return self.frozen is not None

    def draft_json(self) -> str:
        """可持久化的草稿快照（脱敏：仅文档提取事实与问题，不含原始文档全文）。"""
        return json.dumps(
            {
                "provider": self.draft.provider,
                "operations": [fact.value for fact in self.draft.operations],
                "pending": [{"qid": q.qid, "topic": q.topic} for q in self.draft.pending],
                "injections": [fact.source for fact in self.draft.injections],
                "explain_status": self.explain_status,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def start_onboarding(docs: Iterable[Doc], *, provider: str) -> OnboardingResult:
    """规则式提取 → 待确认草稿 + 确定性问题清单（**不调用模型**）。"""
    draft = extract_integration_spec(docs, provider=provider)
    return OnboardingResult(provider=provider, draft=draft,
                            questions=render_questions(draft), explain_status=STATUS_DISABLED)


def augment_questions_with_deepseek(
    result: OnboardingResult,
    *,
    db: Session,
    project_id: int,
    client: Any,
) -> OnboardingResult:
    """用 DeepSeek（``user_interaction`` 路由）补充面向用户的提问说明。

    只把**脱敏结构化事实**交给模型；缺凭据（``ProviderPreflightError``）→ ``NOT_RUN`` 并保留草稿；
    任何模型调用经网关预留/结算并记 ``run_id/task_id/call_id``（此处为草稿期，``run_id=None``）。
    """
    from agentforge_platform.clients import ProviderPreflightError

    facts = json.dumps(
        {
            "provider": result.provider,
            "questions": [{"qid": q.qid, "topic": q.topic} for q in result.draft.pending],
            "operations": [fact.value for fact in result.draft.operations],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    prompt = (
        "你面向**申请第三方服务的用户**，用中文简要说明**为什么需要确认**下列问题（只复述事实，"
        "不得新增权限/域名/SDK，不得改写需求）：\n" + facts
    )
    try:
        completion = invoke(
            db,
            call_id=uuid.uuid4().hex,
            model_role="user_interaction",
            client=client,
            prompt=prompt,
            project_id=project_id,
            max_input_tokens=_MAX_INPUT_TOKENS,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        )
    except ProviderPreflightError:
        return replace(result, explain_status=STATUS_NOT_RUN)  # 缺凭据：保留草稿，不伪造
    return replace(
        result,
        questions=result.questions + "\n\n" + completion.content.strip(),
        explain_status=STATUS_OK,
    )


def confirm_onboarding(
    result: OnboardingResult, confirmations: dict[str, str]
) -> OnboardingResult:
    """用户确认后冻结；仍有未确认 pending → ``INTEGRATION_PENDING``（暂停 + 问题清单）。"""
    frozen = freeze_integration_spec(result.draft, confirmations)
    return replace(result, frozen=frozen)


def adapter_generation_plan(frozen: FrozenIntegrationSpec) -> dict:
    """受限 Adapter 任务的确定性计划：仅授权目录 + 固定提示（不改需求/不装 SDK）。"""
    return {
        "task_id": ADAPTER_TASK_ID,
        "allowed_paths": list(ADAPTER_ALLOWED_PATHS),
        "provider": frozen.provider,
        "operations": [fact.value for fact in frozen.operations],
        "prompt": render_adapter_prompt(frozen),
    }


def render_adapter_prompt(frozen: FrozenIntegrationSpec) -> str:
    """把**已冻结**事实渲染为受限 Adapter 任务提示（确定性、脱敏、无原始文档/聊天）。"""
    return (
        "你是受限的集成适配器编码器。依据下列**已冻结**集成契约，在 allowed_paths 内为 "
        f"{frozen.provider} 实现/补充 Adapter：仅解析第三方响应并**归一化为内部 DTO**，"
        "不得让供应商类型进入 Port，不得安装任何 SDK、不得新增域名/权限、不得宣称 Live 已验证。"
        "完成后停止。\n"
        + json.dumps(
            {
                "operations": [f.value for f in frozen.operations],
                "auth": [f.value for f in frozen.auth],
                "errors": [f.value for f in frozen.errors],
                "callbacks": [f.value for f in frozen.callbacks],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def apply_adapter_changes(
    frozen: FrozenIntegrationSpec,
    *,
    engine_factory: Callable[[list[str], str], tuple[Any, Any]],
) -> dict:
    """运行受限编码引擎产出变更集 → **授权校验**（仅 Adapter 目录）→ 原子提交。

    ``engine_factory(allowed_paths, task_id) -> (engine, file_backend)`` 与 R17/R18 同形；
    生产环境由 C01 的编码路由提供，测试可注入脚本化引擎。
    """
    allowed = list(ADAPTER_ALLOWED_PATHS)
    engine, file_backend = engine_factory(allowed, ADAPTER_TASK_ID)
    result = engine.run(render_adapter_prompt(frozen))
    change_set = result.get("change_set", {})
    edited = [change.get("path", "") for change in change_set.get("changes", [])]
    issues = adapter_edit_issues([path for path in edited if path], allowed_prefix=allowed[0])
    if issues:
        raise AppException(
            ErrorCode.ASSET_CONFLICT, "Adapter 变更越界：" + "；".join(issues), status_code=422
        )
    committed = file_backend.commit()
    return {"edited_paths": edited, "change_set": committed.to_dict()}


__all__ = [
    "ADAPTER_ALLOWED_PATHS",
    "ADAPTER_TASK_ID",
    "FLAG_ENV",
    "STATUS_DISABLED",
    "STATUS_NOT_RUN",
    "STATUS_OK",
    "OnboardingResult",
    "adapter_generation_plan",
    "apply_adapter_changes",
    "augment_questions_with_deepseek",
    "confirm_onboarding",
    "onboarding_enabled",
    "render_adapter_prompt",
    "start_onboarding",
]
