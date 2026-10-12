"""M21 模型调用计费前置护栏。

- **静态路由**：模型角色 → Provider/model（MVP 仅启用 `user_interaction → deepseek_official/deepseek-flash`；
  其余角色仅占位，调用即拒且**不发生外部请求**）。
- **账本**：`model_call` 记录生成前草稿（project_id + draft_id）与生成期（run_id）用量，按 call_id **幂等结算**。
- **前置拦截**：预算预留 + 草稿次数/频率上限，均在调用 Provider **之前**判定。
- 金额用整数**微单位**（1 元 = 1e6 微），价格来自外部 price 文件与 `price_version`，未知价记 `UNKNOWN` 并保留预留。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentforge_platform.clients import Completion, ModelClient, ProviderPreflightError, Usage
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.models import ModelCall, Project

MICRO_PER_YUAN = 1_000_000
UNKNOWN = "UNKNOWN"
_ALLOWED_THINKING_MODES = frozenset({"disabled", "enabled"})
_ALLOWED_REASONING_EFFORTS = frozenset({"low", "high"})


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class Route:
    role: str
    provider: str
    model: str
    api_key_env: str
    enabled: bool


ROUTES: dict[str, Route] = {
    "user_interaction": Route(
        "user_interaction", "deepseek_official", "deepseek-flash", "DEEPSEEK_API_KEY", True
    ),
    # 后续阶段才实现：仅文档占位，MVP 不实例化、不检查密钥
    "technical_planning": Route(
        "technical_planning", "dashscope", "qwen3.7-plus", "DASHSCOPE_API_KEY", False
    ),
    "coding": Route("coding", "dashscope", "qwen3-coder-flash", "DASHSCOPE_API_KEY", False),
    "complex_repair": Route(
        "complex_repair", "dashscope", "qwen3.8-max", "DASHSCOPE_API_KEY", False
    ),
}


@dataclass(frozen=True)
class Price:
    version: str
    input_micro_per_million: int
    output_micro_per_million: int


def resolve_route(model_role: str) -> Route:
    route = ROUTES.get(model_role)
    if route is None or not route.enabled:
        raise AppException(
            ErrorCode.MODEL_ROLE_DISABLED, f"未启用或未知的模型角色：{model_role}", status_code=422
        )
    return route


def verified_reasoning_efforts() -> frozenset[str]:
    """经真实接口探针验证可用的思考强度集合（R03）。

    未列入 ``AGENTFORGE_VERIFIED_REASONING_EFFORTS``（逗号分隔）的强度一律**阻止启用**，
    既不静默丢弃参数，也不假装已启用思考模式。
    """
    raw = os.environ.get("AGENTFORGE_VERIFIED_REASONING_EFFORTS", "")
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _validate_thinking(thinking_mode: str | None, reasoning_effort: str | None) -> None:
    """校验思考模式与强度的组合；强度必须已经过探针验证。"""
    if thinking_mode is not None and thinking_mode not in _ALLOWED_THINKING_MODES:
        raise AppException(
            ErrorCode.MODEL_UNAVAILABLE, f"未知思考模式：{thinking_mode}", status_code=422
        )
    if reasoning_effort is None:
        return
    if thinking_mode != "enabled":
        raise AppException(
            ErrorCode.MODEL_UNAVAILABLE,
            "reasoning_effort 需与 thinking.type=enabled 同时使用。",
            status_code=422,
        )
    if reasoning_effort not in _ALLOWED_REASONING_EFFORTS:
        raise AppException(
            ErrorCode.MODEL_UNAVAILABLE, f"未知思考强度：{reasoning_effort}", status_code=422
        )
    if reasoning_effort not in verified_reasoning_efforts():
        raise AppException(
            ErrorCode.MODEL_UNAVAILABLE,
            f"思考强度 {reasoning_effort} 尚未通过接口探针验证，已阻止启用"
            "（AGENTFORGE_VERIFIED_REASONING_EFFORTS）。",
            status_code=503,
        )


def project_budget_micro() -> int:
    return int(
        os.environ.get("AGENTFORGE_PROJECT_BUDGET_MICRO", str(10 * MICRO_PER_YUAN))  # 内测默认 10 元
    )


def draft_max_calls() -> int:
    return int(os.environ.get("AGENTFORGE_DRAFT_MAX_CALLS", "20"))


def draft_min_interval_seconds() -> float:
    return float(os.environ.get("AGENTFORGE_DRAFT_MIN_INTERVAL_SECONDS", "0"))


def user_draft_max_calls() -> int:
    return int(os.environ.get("AGENTFORGE_USER_DRAFT_MAX_CALLS", "100"))


def user_draft_min_interval_seconds() -> float:
    return float(os.environ.get("AGENTFORGE_USER_DRAFT_MIN_INTERVAL_SECONDS", "0"))


def default_reserve_micro() -> int:
    return int(os.environ.get("AGENTFORGE_DEFAULT_RESERVE_MICRO", str(MICRO_PER_YUAN)))


def price_table() -> dict[str, Price]:
    path = os.environ.get("AGENTFORGE_PRICE_FILE")
    if not path or not Path(path).is_file():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    version = str(data.get("version", UNKNOWN))
    return {
        model: Price(
            version,
            int(entry["input_micro_per_million"]),
            int(entry["output_micro_per_million"]),
        )
        for model, entry in data.get("models", {}).items()
    }


def lookup_price(model: str) -> Price | None:
    return price_table().get(model)


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _reserve_micro(price: Price | None, max_input_tokens: int, max_output_tokens: int) -> int:
    if price is None:
        return default_reserve_micro()
    micro = _ceil_div(max_input_tokens * price.input_micro_per_million, 1_000_000) + _ceil_div(
        max_output_tokens * price.output_micro_per_million, 1_000_000
    )
    return micro


def _totals(db: Session, project_id: int) -> tuple[int, int]:
    settled = db.scalar(
        select(func.coalesce(func.sum(ModelCall.cost_micro), 0)).where(
            ModelCall.project_id == project_id, ModelCall.status == "SETTLED"
        )
    )
    reserved = db.scalar(
        select(func.coalesce(func.sum(ModelCall.reserved_micro), 0)).where(
            ModelCall.project_id == project_id, ModelCall.status != "SETTLED"
        )
    )
    return int(settled or 0), int(reserved or 0)


def _enforce_draft_limits(db: Session, project_id: int) -> None:
    interval = draft_min_interval_seconds()
    if interval > 0:
        last = db.scalar(
            select(func.max(ModelCall.created_at)).where(
                ModelCall.project_id == project_id, ModelCall.run_id.is_(None),
                ModelCall.status != "NOT_SENT",
            )
        )
        if last is not None and (_now() - last).total_seconds() < interval:
            raise AppException(
                ErrorCode.DRAFT_LIMIT_EXCEEDED, "草稿调用过于频繁。", status_code=429
            )
    count = db.scalar(
        select(func.count())
        .select_from(ModelCall)
        .where(ModelCall.project_id == project_id, ModelCall.run_id.is_(None),
               ModelCall.status != "NOT_SENT")
    )
    if int(count or 0) >= draft_max_calls():
        raise AppException(
            ErrorCode.DRAFT_LIMIT_EXCEEDED, "草稿调用次数已达上限。", status_code=429
        )
    owner_id = db.scalar(select(Project.owner_id).where(Project.id == project_id))
    if owner_id is None:
        return
    user_count = db.scalar(
        select(func.count())
        .select_from(ModelCall)
        .join(Project, ModelCall.project_id == Project.id)
        .where(Project.owner_id == owner_id, ModelCall.run_id.is_(None),
               ModelCall.status != "NOT_SENT")
    )
    if int(user_count or 0) >= user_draft_max_calls():
        raise AppException(
            ErrorCode.DRAFT_LIMIT_EXCEEDED, "用户草稿调用次数已达上限。", status_code=429
        )
    user_interval = user_draft_min_interval_seconds()
    if user_interval > 0:
        last_user_call = db.scalar(
            select(func.max(ModelCall.created_at))
            .join(Project, ModelCall.project_id == Project.id)
            .where(Project.owner_id == owner_id, ModelCall.run_id.is_(None),
                   ModelCall.status != "NOT_SENT")
        )
        if last_user_call is not None and (_now() - last_user_call).total_seconds() < user_interval:
            raise AppException(
                ErrorCode.DRAFT_LIMIT_EXCEEDED, "用户草稿调用过于频繁。", status_code=429
            )


def reserve(
    db: Session,
    *,
    call_id: str,
    model_role: str,
    project_id: int,
    max_input_tokens: int,
    max_output_tokens: int,
    draft_id: int | None = None,
    run_id: int | None = None,
    thinking_mode: str | None = None,
    reasoning_effort: str | None = None,
) -> ModelCall:
    route = resolve_route(model_role)
    _validate_thinking(thinking_mode, reasoning_effort)
    if run_id is None:
        _enforce_draft_limits(db, project_id)

    price = lookup_price(route.model)
    reserve_micro = _reserve_micro(price, max_input_tokens, max_output_tokens)
    settled, reserved = _totals(db, project_id)
    if project_budget_micro() - settled - reserved < reserve_micro:
        raise AppException(ErrorCode.BUDGET_EXCEEDED, "预算不足，未调用模型。", status_code=402)

    row = ModelCall(
        call_id=call_id,
        model_role=route.role,
        provider=route.provider,
        model=route.model,
        project_id=project_id,
        draft_id=draft_id,
        run_id=run_id,
        thinking_mode=thinking_mode,
        reasoning_effort=reasoning_effort,
        price_version=price.version if price else UNKNOWN,
        input_micro_per_million=price.input_micro_per_million if price else None,
        output_micro_per_million=price.output_micro_per_million if price else None,
        reserved_micro=reserve_micro,
        status="RESERVED",
    )
    db.add(row)
    db.flush()
    return row


def settle(
    db: Session,
    call_id: str,
    *,
    usage: Usage | None = None,
    request_id: str | None = None,
    error_class: str | None = None,
) -> ModelCall:
    row = db.get(ModelCall, call_id)
    if row is None:
        raise AppException(ErrorCode.RUN_NOT_FOUND, f"未知 call_id：{call_id}", status_code=404)
    if row.status == "SETTLED":
        return row  # 幂等：已结算不重复计费
    if request_id:
        row.request_id = request_id
    if usage is None:
        row.status = "UNKNOWN"
        row.error_class = error_class
        row.updated_at = _now()
        db.flush()
        return row

    row.input_tokens = usage.input_tokens
    row.output_tokens = usage.output_tokens
    if row.input_micro_per_million is None or row.output_micro_per_million is None:
        row.status = "UNKNOWN"
        row.cost_micro = None
    else:
        row.cost_micro = _ceil_div(
            usage.input_tokens * row.input_micro_per_million, 1_000_000
        ) + _ceil_div(usage.output_tokens * row.output_micro_per_million, 1_000_000)
        row.status = "SETTLED"
    row.updated_at = _now()
    db.flush()
    return row


def invoke(
    db: Session,
    *,
    call_id: str,
    model_role: str,
    client: ModelClient,
    prompt: str,
    project_id: int,
    max_input_tokens: int,
    max_output_tokens: int,
    draft_id: int | None = None,
    run_id: int | None = None,
    thinking_mode: str | None = None,
    json_mode: bool = False,
    reasoning_effort: str | None = None,
) -> Completion:
    route = resolve_route(model_role)
    _validate_thinking(thinking_mode, reasoning_effort)
    # SQLite WAL 允许多读单写。先拿写锁，再检查预算/频率并写入预留，
    # 防止并发请求都从相同的旧余额通过检查。
    db.commit()
    if db.get_bind().dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    reserve(
        db,
        call_id=call_id,
        model_role=model_role,
        project_id=project_id,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        draft_id=draft_id,
        run_id=run_id,
        thinking_mode=thinking_mode,
        reasoning_effort=reasoning_effort,
    )
    # 先持久化预留；远程调用期间不能持有未提交的账本事务。
    db.commit()
    try:
        kwargs: dict[str, object] = {"max_output_tokens": max_output_tokens}
        if json_mode:
            kwargs["json_mode"] = True
        result = client.complete(
            model=route.model,
            prompt=prompt,
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
            **kwargs,
        )
    except ProviderPreflightError as exc:
        row = db.get(ModelCall, call_id)
        assert row is not None
        row.status = "NOT_SENT"
        row.reserved_micro = 0
        row.error_class = type(exc).__name__
        row.updated_at = _now()
        db.commit()
        raise
    except Exception as exc:
        settle(db, call_id, usage=None, error_class=type(exc).__name__)
        db.commit()  # 保留预留与 UNKNOWN 账本，供后续对账（不随调用者回滚）
        raise
    settle(db, call_id, usage=result.usage, request_id=result.request_id)
    db.commit()
    return result
