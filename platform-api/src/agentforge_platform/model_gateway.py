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
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentforge_platform.budget import effective_budget_micro
from agentforge_platform.clients import Completion, ModelClient, ProviderPreflightError, Usage
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.limits import runtime_limits
from agentforge_platform.models import ModelCall, Project, RunTask

MICRO_PER_YUAN = 1_000_000
UNKNOWN = "UNKNOWN"
_ALLOWED_THINKING_MODES = frozenset({"disabled", "enabled"})
_ALLOWED_REASONING_EFFORTS = frozenset({"low", "high"})


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class Price:
    version: str
    input_micro_per_million: int
    output_micro_per_million: int
    currency: str = "CNY"


@dataclass(frozen=True)
class Route:
    role: str
    provider: str
    model: str
    api_key_env: str
    enabled: bool
    base_url: str | None = None
    max_output_tokens: int | None = None
    supports_tools: bool = True
    price: Price | None = None


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

ROUTES: dict[str, Route] = {
    "user_interaction": Route(
        "user_interaction",
        "deepseek_official",
        "deepseek-flash",
        "DEEPSEEK_API_KEY",
        True,
        base_url=DEFAULT_DEEPSEEK_BASE_URL,
    ),
    # 后续阶段才实现：仅文档占位，MVP 不实例化、不检查密钥
    "technical_planning": Route(
        "technical_planning",
        "dashscope",
        "qwen3.7-plus",
        "DASHSCOPE_API_KEY",
        False,
        base_url=DEFAULT_DASHSCOPE_BASE_URL,
    ),
    "coding": Route(
        "coding",
        "dashscope",
        "qwen3-coder-flash",
        "DASHSCOPE_API_KEY",
        False,
        base_url=DEFAULT_DASHSCOPE_BASE_URL,
    ),
    "complex_repair": Route(
        "complex_repair",
        "dashscope",
        "qwen3.8-max",
        "DASHSCOPE_API_KEY",
        False,
        base_url=DEFAULT_DASHSCOPE_BASE_URL,
    ),
}

# A04：工厂流水线角色及其向已有角色的回退映射。
FACTORY_ROLES: dict[str, str] = {
    "chief_architect": "technical_planning",
    "dept_lead": "technical_planning",
    "dept_coder": "coding",
    "dept_tester": "coding",
}
KNOWN_ROLES: frozenset[str] = frozenset(set(ROUTES) | set(FACTORY_ROLES))
TOOL_REQUIRED_ROLES: frozenset[str] = frozenset(
    {"coding", "complex_repair", "dept_coder", "dept_tester"}
)

_SAFE_API_KEY_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_SUSPECTED_SECRET_PREFIXES = ("sk-", "ghp_", "github_pat_", "ak-", "akia", "bearer ", "eyj")
_FORBIDDEN_CONFIG_KEYS = frozenset({"api_key", "apikey", "secret", "token", "password"})


def _looks_like_raw_secret(value: str) -> bool:
    stripped = value.strip()
    lower = stripped.lower()
    if lower.startswith(_SUSPECTED_SECRET_PREFIXES):
        return True
    return _SAFE_API_KEY_ENV_RE.fullmatch(stripped) is None


def _parse_custom_price(
    raw_price: Any, *, default_version: str, default_currency: str
) -> Price | None:
    if raw_price is None:
        return None
    if not isinstance(raw_price, dict):
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "price_micro 必须为对象（包含 input_micro_per_million / output_micro_per_million）",
            status_code=422,
        )
    if "input_micro_per_million" not in raw_price or "output_micro_per_million" not in raw_price:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "price_micro 缺少 input_micro_per_million 或 output_micro_per_million",
            status_code=422,
        )
    try:
        in_micro = int(raw_price["input_micro_per_million"])
        out_micro = int(raw_price["output_micro_per_million"])
    except (TypeError, ValueError) as exc:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "price_micro 单价必须为非负整数",
            status_code=422,
        ) from exc
    if in_micro < 0 or out_micro < 0:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "price_micro 单价不能为负数",
            status_code=422,
        )
    version = str(raw_price.get("version") or default_version or UNKNOWN).strip() or UNKNOWN
    currency = str(raw_price.get("currency") or default_currency or "CNY").strip().upper()
    return Price(version, in_micro, out_micro, currency)


def load_custom_routes(config_path: str | Path | None = None) -> dict[str, Route]:
    """A04：加载并严格校验用户自定义模型路由配置（``config/models.json`` 或 ``AGENTFORGE_MODELS_CONFIG``）。

    未配置路径时返回空字典（回退至默认路由）。
    """
    raw_path = (
        str(config_path).strip()
        if config_path is not None
        else os.environ.get("AGENTFORGE_MODELS_CONFIG", "").strip()
    )
    if not raw_path:
        return {}
    path = Path(raw_path)
    if not path.is_file():
        raise AppException(
            ErrorCode.CONFIG_REQUIRED,
            f"模型路由配置文件不存在：{path}",
            status_code=422,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"模型路由配置文件不是合法 JSON：{path}",
            status_code=422,
        ) from exc

    if not isinstance(payload, dict):
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "模型路由配置顶层必须为 JSON 对象",
            status_code=422,
        )

    roles_block = payload.get("roles") if "roles" in payload else payload
    if not isinstance(roles_block, dict) or not roles_block:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "模型路由配置必须包含非空的 roles 映射",
            status_code=422,
        )

    default_version = str(payload.get("version") or "custom-1").strip()
    default_currency = str(payload.get("currency") or "CNY").strip().upper()

    custom: dict[str, Route] = {}
    for role, entry in roles_block.items():
        if role not in KNOWN_ROLES:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"未知的模型角色：{role!r}（允许：{sorted(KNOWN_ROLES)}）",
                status_code=422,
            )
        if not isinstance(entry, dict):
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"角色 {role} 配置必须为 JSON 对象",
                status_code=422,
            )

        forbidden_present = {k.lower() for k in entry} & _FORBIDDEN_CONFIG_KEYS
        if forbidden_present:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"角色 {role} 包含禁止直写的密钥字段 {sorted(forbidden_present)}，请仅使用 api_key_env",
                status_code=422,
            )

        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        base_url = str(entry.get("base_url") or "").strip()
        api_key_env = str(entry.get("api_key_env") or "").strip()

        if not provider or not model:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"角色 {role} 缺少必需字段 provider 或 model",
                status_code=422,
            )
        if not base_url:
            raise AppException(
                ErrorCode.CONFIG_REQUIRED,
                f"角色 {role} 缺少必需字段 base_url",
                status_code=422,
            )
        parsed_url = urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc or " " in base_url:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"角色 {role} 的 base_url 无效：{base_url!r}",
                status_code=422,
            )
        if not api_key_env or _looks_like_raw_secret(api_key_env):
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"角色 {role} 的 api_key_env 非法或疑似直接写入了密钥明文（仅允许大写环境变量名）",
                status_code=422,
            )

        supports_tools_raw = entry.get("supports_tools", True)
        if not isinstance(supports_tools_raw, bool):
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"角色 {role} 的 supports_tools 必须为布尔值",
                status_code=422,
            )
        if role in TOOL_REQUIRED_ROLES and not supports_tools_raw:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"编码/测试角色 {role} 要求 supports_tools=true，拒绝绑定不支持工具调用的模型",
                status_code=422,
            )

        max_out_raw = entry.get("max_output_tokens")
        max_out: int | None = None
        if max_out_raw is not None:
            if isinstance(max_out_raw, bool) or not isinstance(max_out_raw, int) or max_out_raw <= 0:
                raise AppException(
                    ErrorCode.VALIDATION_ERROR,
                    f"角色 {role} 的 max_output_tokens 必须为正整数",
                    status_code=422,
                )
            max_out = max_out_raw

        enabled_raw = entry.get("enabled", True)
        if not isinstance(enabled_raw, bool):
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"角色 {role} 的 enabled 必须为布尔值",
                status_code=422,
            )

        price = _parse_custom_price(
            entry.get("price_micro"),
            default_version=default_version,
            default_currency=default_currency,
        )

        custom[role] = Route(
            role=role,
            provider=provider,
            model=model,
            api_key_env=api_key_env,
            enabled=enabled_raw,
            base_url=base_url.rstrip("/"),
            max_output_tokens=max_out,
            supports_tools=supports_tools_raw,
            price=price,
        )
    return custom


def _role_enabled(model_role: str) -> bool:
    """非 ``user_interaction`` 角色默认禁用；仅在其对应 R 步骤完成并按环境变量显式开启后才可用。"""
    if model_role == "technical_planning":
        return os.environ.get(
            "AGENTFORGE_TECHNICAL_PLANNING_ENABLED", ""
        ).strip().lower() in {"1", "true", "yes"}
    if model_role == "coding":
        # R16：DeepAgent 受限 patch engine 接入后，按环境变量显式开启。
        return os.environ.get(
            "AGENTFORGE_CODING_ENABLED", ""
        ).strip().lower() in {"1", "true", "yes"}
    if model_role == "complex_repair":
        # R18：第二次修复的升级角色（Qwen3.8 Max），按环境变量显式开启。
        return os.environ.get(
            "AGENTFORGE_COMPLEX_REPAIR_ENABLED", ""
        ).strip().lower() in {"1", "true", "yes"}
    if model_role in FACTORY_ROLES:
        own_flag = os.environ.get(
            f"AGENTFORGE_{model_role.upper()}_ENABLED", ""
        ).strip().lower()
        if own_flag in {"1", "true", "yes"}:
            return True
        return _role_enabled(FACTORY_ROLES[model_role])
    return False


def _find_route_candidate(model_role: str) -> Route | None:
    custom = load_custom_routes()
    if model_role in custom:
        return custom[model_role]
    if model_role in FACTORY_ROLES:
        fallback_role = FACTORY_ROLES[model_role]
        if fallback_role in custom:
            base = custom[fallback_role]
            if model_role in TOOL_REQUIRED_ROLES and not base.supports_tools:
                raise AppException(
                    ErrorCode.VALIDATION_ERROR,
                    f"编码/测试角色 {model_role} 回退到的角色 {fallback_role} 不支持工具调用（supports_tools=false）",
                    status_code=422,
                )
            return replace(base, role=model_role)
        if fallback_role in ROUTES:
            base = ROUTES[fallback_role]
            override_model = os.environ.get(f"AGENTFORGE_{model_role.upper()}_MODEL") or base.model
            return replace(base, role=model_role, model=override_model)
    return ROUTES.get(model_role)


def role_enabled(model_role: str) -> bool:
    """该模型角色当前是否可用（内建启用、自定义配置启用或经环境变量显式开启）。不发起任何请求。"""
    custom = load_custom_routes()
    if model_role in custom:
        return custom[model_role].enabled
    if model_role in FACTORY_ROLES and FACTORY_ROLES[model_role] in custom:
        return custom[FACTORY_ROLES[model_role]].enabled
    route = _find_route_candidate(model_role)
    return bool(route and (route.enabled or _role_enabled(model_role)))


def resolve_route(model_role: str) -> Route:
    custom = load_custom_routes()
    if model_role in custom:
        route = custom[model_role]
        if not route.enabled:
            raise AppException(
                ErrorCode.MODEL_ROLE_DISABLED, f"未启用或未知的模型角色：{model_role}", status_code=422
            )
        return route
    if model_role in FACTORY_ROLES and FACTORY_ROLES[model_role] in custom:
        route = _find_route_candidate(model_role)
        if route is None or not route.enabled:
            raise AppException(
                ErrorCode.MODEL_ROLE_DISABLED, f"未启用或未知的模型角色：{model_role}", status_code=422
            )
        return route

    route = _find_route_candidate(model_role)
    if route is None or not (route.enabled or _role_enabled(model_role)):
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
    default_currency = str(data.get("currency", "CNY")).upper()
    return {
        model: Price(
            version,
            int(entry["input_micro_per_million"]),
            int(entry["output_micro_per_million"]),
            str(entry.get("currency", default_currency)).upper(),
        )
        for model, entry in data.get("models", {}).items()
    }


def lookup_price(model: str) -> Price | None:
    table_hit = price_table().get(model)
    if table_hit is not None:
        return table_hit
    if os.environ.get("AGENTFORGE_MODELS_CONFIG", "").strip():
        for route in load_custom_routes().values():
            if route.model == model and route.price is not None:
                return route.price
    return None


# --------------------------------------------------------------------------- #
# R20：跨币种共享预算——按**锁定汇率快照**折算为人民币微单位
# --------------------------------------------------------------------------- #
CNY = "CNY"
_IDENTITY_RATE_MICRO = MICRO_PER_YUAN  # 1 CNY = 1_000_000 微


@dataclass(frozen=True)
class FxSnapshot:
    version: str
    rates_micro: dict[str, int]
    captured_at: str | None = None


def fx_snapshot() -> FxSnapshot | None:
    """锁定汇率快照：``{"version","captured_at","rates":{"USD": 7200000}}``（1 USD = 7.2 CNY）。"""
    path = os.environ.get("AGENTFORGE_FX_FILE")
    if not path or not Path(path).is_file():
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return FxSnapshot(
        version=str(data.get("version", UNKNOWN)),
        rates_micro={str(k).upper(): int(v) for k, v in data.get("rates", {}).items()},
        captured_at=data.get("captured_at"),
    )


def fx_rate_micro(currency: str) -> tuple[int, str | None] | None:
    """返回 ``(人民币微/1 单位外币, 快照时间)``；人民币恒为 1:1；未知币种/缺失快照返回 ``None``（不写零）。"""
    currency = (currency or CNY).upper()
    if currency == CNY:
        return _IDENTITY_RATE_MICRO, None
    snapshot = fx_snapshot()
    if snapshot is None or currency not in snapshot.rates_micro:
        return None
    return snapshot.rates_micro[currency], snapshot.captured_at


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _foreign_micro(price: Price, input_tokens: int, output_tokens: int) -> int:
    """按定价币种计算的**外币**微单位金额。"""
    return _ceil_div(input_tokens * price.input_micro_per_million, MICRO_PER_YUAN) + _ceil_div(
        output_tokens * price.output_micro_per_million, MICRO_PER_YUAN
    )


def _reserve_micro(price: Price | None, max_input_tokens: int, max_output_tokens: int) -> int:
    """预留统一折为**人民币微单位**：外币按锁定汇率快照折算；无价/无汇率 → 保守默认预留（不写零）。"""
    if price is None:
        return default_reserve_micro()
    foreign = _foreign_micro(price, max_input_tokens, max_output_tokens)
    if price.currency == CNY:
        return foreign
    fx = fx_rate_micro(price.currency)
    if fx is None:
        return default_reserve_micro()
    return _ceil_div(foreign * fx[0], MICRO_PER_YUAN)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)  # 3.11+ 原生支持末尾 Z
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


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


def _enforce_run_limits(db: Session, run_id: int, max_output_tokens: int) -> None:
    """R21：生成期统一护栏——单次输出上限 + 全 run 模型调用次数（所有角色共用）。"""
    limits = runtime_limits()
    if max_output_tokens > limits.max_output_tokens_per_call:
        raise AppException(
            ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            f"单次输出超上限：{max_output_tokens} > {limits.max_output_tokens_per_call}",
            status_code=422,
        )
    used = db.scalar(
        select(func.count())
        .select_from(ModelCall)
        .where(ModelCall.run_id == run_id, ModelCall.status != "NOT_SENT")
    )
    if int(used or 0) >= limits.max_model_calls_per_run:
        raise AppException(
            ErrorCode.CALL_LIMIT_EXCEEDED,
            f"全 run 模型调用超上限：{limits.max_model_calls_per_run}",
            status_code=429,
        )


def _require_task_context(db: Session, run_id: int, task_id: str) -> None:
    """R13：任务内生成期调用必须携带属于该 run 的 ``task_id``（DAG 上下文），否则拒绝。"""
    exists = db.scalar(
        select(RunTask.id).where(RunTask.run_id == run_id, RunTask.task_id == task_id)
    )
    if exists is None:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"task_id '{task_id}' 不属于 run {run_id}",
            status_code=422,
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
    task_id: str | None = None,
    thinking_mode: str | None = None,
    reasoning_effort: str | None = None,
) -> ModelCall:
    route = resolve_route(model_role)
    _validate_thinking(thinking_mode, reasoning_effort)
    if run_id is None and task_id is not None:
        raise AppException(
            ErrorCode.VALIDATION_ERROR, "task_id 仅适用于带 run_id 的生成期调用", status_code=422
        )
    if run_id is not None and task_id is not None:
        _require_task_context(db, run_id, task_id)
    if run_id is None:
        _enforce_draft_limits(db, project_id)
    else:
        _enforce_run_limits(db, run_id, max_output_tokens)

    price = route.price or lookup_price(route.model)
    reserve_micro = _reserve_micro(price, max_input_tokens, max_output_tokens)
    settled, reserved = _totals(db, project_id)
    # R24：生效预算 = 基础额度 + 可追溯调整；调用方据此在预算不足时暂停而非硬失败。
    if effective_budget_micro(db, project_id) - settled - reserved < reserve_micro:
        raise AppException(ErrorCode.BUDGET_EXCEEDED, "预算不足，未调用模型。", status_code=402)

    row = ModelCall(
        call_id=call_id,
        model_role=route.role,
        provider=route.provider,
        model=route.model,
        project_id=project_id,
        draft_id=draft_id,
        run_id=run_id,
        task_id=task_id,
        thinking_mode=thinking_mode,
        reasoning_effort=reasoning_effort,
        price_version=price.version if price else UNKNOWN,
        price_currency=price.currency if price else None,
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
        row.updated_at = _now()
        db.flush()
        return row
    foreign = _ceil_div(
        usage.input_tokens * row.input_micro_per_million, MICRO_PER_YUAN
    ) + _ceil_div(usage.output_tokens * row.output_micro_per_million, MICRO_PER_YUAN)
    fx = fx_rate_micro(row.price_currency or CNY)
    if fx is None:  # 未知汇率：暂停对账，保留 UNKNOWN，不写零
        row.status = "UNKNOWN"
        row.cost_micro = None
    else:
        rate_micro, captured_at = fx
        row.cost_micro = _ceil_div(foreign * rate_micro, MICRO_PER_YUAN)
        row.fx_rate_micro = rate_micro
        row.fx_captured_at = _parse_dt(captured_at)
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
    task_id: str | None = None,
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
        task_id=task_id,
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
