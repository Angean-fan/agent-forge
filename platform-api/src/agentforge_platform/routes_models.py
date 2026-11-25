"""模型路由配置 API：面向前端左侧「模型配置」面板提供脱敏读取、自定义模型库管理与受控更新。

安全红线（对齐 A04）：
- 绝不接受或持久化明文密钥（仅允许大写环境变量名 ``api_key_env``）；
- 绝不向前端回传环境变量值，只返回 ``api_key_present`` 布尔状态；
- 编码/测试角色（``coding`` / ``complex_repair`` / ``dept_coder`` / ``dept_tester``）强制 ``supports_tools=true``；
- 写入采用临时文件校验 + 原子替换，确保 ``load_custom_routes()`` 立即可用且不破坏配置文件。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from agentforge_platform.accounts import Account
from agentforge_platform.auth import get_current_account, require_csrf
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.middleware import get_request_id
from agentforge_platform.model_gateway import (
    FACTORY_ROLES,
    KNOWN_ROLES,
    ROUTES,
    Route,
    _looks_like_raw_secret,
    _role_enabled,
    load_custom_routes,
)
from agentforge_platform.responses import ApiResponse

router = APIRouter(prefix="/api/models", tags=["models"])
_STRICT = ConfigDict(extra="forbid")

ROLE_ORDER: tuple[str, ...] = (
    "user_interaction",
    "chief_architect",
    "technical_planning",
    "dept_lead",
    "dept_coder",
    "coding",
    "dept_tester",
    "complex_repair",
)

ROLE_LABELS: dict[str, str] = {
    "user_interaction": "需求访谈与澄清",
    "chief_architect": "总管架构拆解",
    "technical_planning": "技术方案规划",
    "dept_lead": "部门副总管",
    "dept_coder": "部门编码引擎",
    "coding": "标准装配编码",
    "dept_tester": "部门测试验证",
    "complex_repair": "疑难缺陷修复",
}


class CustomModelPresetView(BaseModel):
    model_config = _STRICT

    id: str
    name: str
    provider: str
    model: str
    base_url: str
    base_url_host: str
    api_key_env: str
    api_key_present: bool
    supports_tools: bool
    max_output_tokens: int | None
    timeout_seconds: int | None
    removable: bool = True


class CustomModelPresetCreateRequest(BaseModel):
    model_config = _STRICT

    id: str | None = Field(default=None, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=512)
    api_key_env: str = Field(min_length=1, max_length=64)
    supports_tools: bool = True
    max_output_tokens: int | None = 8192
    timeout_seconds: int | None = 180


class ModelRoleConfigView(BaseModel):
    model_config = _STRICT

    role: str
    label: str
    provider: str
    model: str
    base_url: str
    base_url_host: str
    api_key_env: str
    api_key_present: bool
    enabled: bool
    supports_tools: bool
    max_output_tokens: int | None
    timeout_seconds: int | None
    source: str


class ModelConfigView(BaseModel):
    model_config = _STRICT

    config_source: str
    writable: bool
    roles: list[ModelRoleConfigView]
    custom_models: list[CustomModelPresetView] = Field(default_factory=list)


class ModelRoleUpdateItem(BaseModel):
    model_config = _STRICT

    role: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=512)
    api_key_env: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    supports_tools: bool = True
    max_output_tokens: int | None = None
    timeout_seconds: int | None = None


class ModelConfigUpdateRequest(BaseModel):
    model_config = _STRICT

    roles: list[ModelRoleUpdateItem] = Field(min_length=1, max_length=16)


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "models.json"


def _resolve_config_target_path() -> Path:
    env_path = os.environ.get("AGENTFORGE_MODELS_CONFIG", "").strip()
    if env_path:
        return Path(env_path)
    return _default_config_path()


def _load_raw_payload(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _slugify_preset_id(raw: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw.strip().lower()).strip("-")
    return slug[:64] or "custom-model"


def _validate_preset_fields(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key_env: str,
    max_output_tokens: int | None,
    timeout_seconds: int | None,
) -> tuple[str, str, str, str]:
    p = provider.strip()
    m = model.strip()
    url = base_url.strip()
    env_name = api_key_env.strip()
    if not p or not m:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "自定义模型缺少必需字段 provider 或 model",
            status_code=422,
        )
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or " " in url:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"自定义模型的 base_url 无效：{url!r}",
            status_code=422,
        )
    if not env_name or _looks_like_raw_secret(env_name):
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "api_key_env 非法或疑似直接写入了密钥明文（仅允许大写环境变量名）",
            status_code=422,
        )
    if max_output_tokens is not None and max_output_tokens <= 0:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "max_output_tokens 必须为正整数",
            status_code=422,
        )
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "timeout_seconds 必须为正整数",
            status_code=422,
        )
    return p, m, url, env_name


def _preset_dict_to_view(entry: dict[str, Any], *, removable: bool = True) -> CustomModelPresetView | None:
    preset_id = str(entry.get("id") or "").strip()
    name = str(entry.get("name") or "").strip()
    provider = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or "").strip()
    base_url = str(entry.get("base_url") or "").strip()
    api_key_env = str(entry.get("api_key_env") or "").strip()
    if not (preset_id and provider and model and base_url and api_key_env):
        return None
    parsed = urlparse(base_url)
    max_tokens = entry.get("max_output_tokens")
    timeout = entry.get("timeout_seconds")
    return CustomModelPresetView(
        id=preset_id,
        name=name or model,
        provider=provider,
        model=model,
        base_url=base_url,
        base_url_host=parsed.hostname or "",
        api_key_env=api_key_env,
        api_key_present=bool(os.environ.get(api_key_env, "").strip()),
        supports_tools=bool(entry.get("supports_tools", True)),
        max_output_tokens=int(max_tokens) if isinstance(max_tokens, int) else None,
        timeout_seconds=int(timeout) if isinstance(timeout, int) else None,
        removable=removable,
    )


def _route_to_view(role: str, route: Route, *, is_custom: bool, is_enabled: bool) -> ModelRoleConfigView:
    base_url = str(route.base_url or "")
    parsed = urlparse(base_url)
    env_name = str(route.api_key_env or "")
    return ModelRoleConfigView(
        role=role,
        label=ROLE_LABELS.get(role, role),
        provider=str(route.provider),
        model=str(route.model),
        base_url=base_url,
        base_url_host=parsed.hostname or "",
        api_key_env=env_name,
        api_key_present=bool(env_name) and bool(os.environ.get(env_name, "").strip()),
        enabled=is_enabled,
        supports_tools=bool(route.supports_tools),
        max_output_tokens=route.max_output_tokens,
        timeout_seconds=route.timeout_seconds,
        source="custom" if is_custom else "default",
    )


def _default_route_for_role(role: str) -> Route | None:
    if role in ROUTES:
        return ROUTES[role]
    if role in FACTORY_ROLES:
        fallback = ROUTES.get(FACTORY_ROLES[role])
        if fallback is not None:
            override_model = os.environ.get(f"AGENTFORGE_{role.upper()}_MODEL") or fallback.model
            return Route(
                role=role,
                provider=fallback.provider,
                model=override_model,
                api_key_env=fallback.api_key_env,
                enabled=fallback.enabled,
                base_url=fallback.base_url,
                max_output_tokens=fallback.max_output_tokens,
                supports_tools=fallback.supports_tools,
                price=fallback.price,
                timeout_seconds=fallback.timeout_seconds,
            )
    return None


def _read_stored_presets(raw_payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_list = raw_payload.get("custom_models")
    if not isinstance(raw_list, list):
        return []
    return [item for item in raw_list if isinstance(item, dict)]


def _build_custom_models_list(
    raw_payload: dict[str, Any], roles_out: list[ModelRoleConfigView]
) -> list[CustomModelPresetView]:
    presets: list[CustomModelPresetView] = []
    seen_ids: set[str] = set()
    seen_fingerprints: set[tuple[str, str, str, str]] = set()

    for raw in _read_stored_presets(raw_payload):
        view = _preset_dict_to_view(raw, removable=True)
        if view is None or view.id in seen_ids:
            continue
        seen_ids.add(view.id)
        seen_fingerprints.add((view.provider, view.model, view.base_url, view.api_key_env))
        presets.append(view)

    # 自动把当前角色已使用的去重模型补入下拉可选列表，方便直接复用
    for role in roles_out:
        fp = (role.provider, role.model, role.base_url, role.api_key_env)
        if fp in seen_fingerprints or not role.base_url or not role.api_key_env:
            continue
        base_id = _slugify_preset_id(f"{role.provider}-{role.model}")
        candidate_id = base_id
        suffix = 2
        while candidate_id in seen_ids:
            candidate_id = f"{base_id}-{suffix}"
            suffix += 1
        seen_ids.add(candidate_id)
        seen_fingerprints.add(fp)
        presets.append(
            CustomModelPresetView(
                id=candidate_id,
                name=f"{role.model} ({role.provider})",
                provider=role.provider,
                model=role.model,
                base_url=role.base_url,
                base_url_host=role.base_url_host,
                api_key_env=role.api_key_env,
                api_key_present=role.api_key_present,
                supports_tools=role.supports_tools,
                max_output_tokens=role.max_output_tokens,
                timeout_seconds=role.timeout_seconds,
                removable=False,
            )
        )

    return presets


def _build_config_view() -> ModelConfigView:
    env_path = os.environ.get("AGENTFORGE_MODELS_CONFIG", "").strip()
    custom_routes: dict[str, Route] = {}
    config_source = "default"
    if env_path:
        try:
            custom_routes = load_custom_routes()
            config_source = "custom"
        except AppException:
            custom_routes = {}
            config_source = "default"

    roles_out: list[ModelRoleConfigView] = []
    ordered_roles = list(ROLE_ORDER) + sorted(KNOWN_ROLES - set(ROLE_ORDER))
    for role in ordered_roles:
        if role in custom_routes:
            route = custom_routes[role]
            roles_out.append(
                _route_to_view(role, route, is_custom=True, is_enabled=bool(route.enabled))
            )
            continue
        if role in FACTORY_ROLES and FACTORY_ROLES[role] in custom_routes:
            fallback = custom_routes[FACTORY_ROLES[role]]
            roles_out.append(
                _route_to_view(role, fallback, is_custom=True, is_enabled=bool(fallback.enabled))
            )
            continue
        candidate = _default_route_for_role(role)
        if candidate is None:
            continue
        roles_out.append(
            _route_to_view(
                role,
                candidate,
                is_custom=False,
                is_enabled=bool(candidate.enabled or _role_enabled(role)),
            )
        )

    raw_payload = _load_raw_payload(_resolve_config_target_path())
    custom_models = _build_custom_models_list(raw_payload, roles_out)

    return ModelConfigView(
        config_source=config_source,
        writable=True,
        roles=roles_out,
        custom_models=custom_models,
    )


def _build_baseline_roles(existing_raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    existing_roles: dict[str, Any] = {}
    if isinstance(existing_raw.get("roles"), dict):
        existing_roles = dict(existing_raw["roles"])

    merged_roles: dict[str, dict[str, Any]] = {}
    current_view = _build_config_view()
    for current in current_view.roles:
        prev_raw = existing_roles.get(current.role)
        entry: dict[str, Any] = {
            "provider": current.provider,
            "model": current.model,
            "base_url": current.base_url,
            "api_key_env": current.api_key_env,
            "enabled": current.enabled,
            "supports_tools": current.supports_tools,
        }
        if current.max_output_tokens is not None:
            entry["max_output_tokens"] = current.max_output_tokens
        if current.timeout_seconds is not None:
            entry["timeout_seconds"] = current.timeout_seconds
        if isinstance(prev_raw, dict) and "price_micro" in prev_raw:
            entry["price_micro"] = prev_raw["price_micro"]
        merged_roles[current.role] = entry
    return merged_roles


def _atomic_write_document(target_path: Path, document: dict[str, Any]) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target_path.parent),
            prefix=".models-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            tmp_path = Path(handle.name)

        # 严格复用 A04 路由校验器，任何非法字段、明文密钥或不支持工具调用均在落盘前拒绝。
        load_custom_routes(tmp_path)
        tmp_path.replace(target_path)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    os.environ["AGENTFORGE_MODELS_CONFIG"] = str(target_path)


@router.get("/config", response_model=ApiResponse[ModelConfigView])
def get_model_config(
    request: Request,
    _account: Account = Depends(get_current_account),
) -> ApiResponse[ModelConfigView]:
    """返回全部模型角色的脱敏路由配置、自定义模型库与环境变量注入状态。"""
    return ApiResponse(
        request_id=get_request_id(request),
        data=_build_config_view(),
    )


@router.put("/config", response_model=ApiResponse[ModelConfigView])
def update_model_config(
    payload: ModelConfigUpdateRequest,
    request: Request,
    _account: Account = Depends(get_current_account),
    _csrf: None = Depends(require_csrf),
) -> ApiResponse[ModelConfigView]:
    """校验并保存用户自定义模型路由配置（仅允许 api_key_env，严禁明文密钥）。"""
    target_path = _resolve_config_target_path()
    existing_raw = _load_raw_payload(target_path)
    merged_roles = _build_baseline_roles(existing_raw)

    seen_roles: set[str] = set()
    for item in payload.roles:
        if item.role not in KNOWN_ROLES:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"未知的模型角色：{item.role!r}",
                status_code=422,
            )
        if item.role in seen_roles:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"角色重复提交：{item.role!r}",
                status_code=422,
            )
        seen_roles.add(item.role)

        prev_entry = merged_roles.get(item.role, {})
        next_entry: dict[str, Any] = {
            "provider": item.provider.strip(),
            "model": item.model.strip(),
            "base_url": item.base_url.strip(),
            "api_key_env": item.api_key_env.strip(),
            "enabled": item.enabled,
            "supports_tools": item.supports_tools,
        }
        if item.max_output_tokens is not None:
            next_entry["max_output_tokens"] = item.max_output_tokens
        if item.timeout_seconds is not None:
            next_entry["timeout_seconds"] = item.timeout_seconds
        if "price_micro" in prev_entry:
            next_entry["price_micro"] = prev_entry["price_micro"]
        merged_roles[item.role] = next_entry

    document: dict[str, Any] = {
        "version": str(existing_raw.get("version") or "ui-custom-1"),
        "currency": str(existing_raw.get("currency") or "CNY"),
        "roles": merged_roles,
    }
    stored_presets = _read_stored_presets(existing_raw)
    if stored_presets:
        document["custom_models"] = stored_presets

    _atomic_write_document(target_path, document)
    return ApiResponse(
        request_id=get_request_id(request),
        data=_build_config_view(),
    )


@router.post("/presets", response_model=ApiResponse[ModelConfigView])
def upsert_custom_model_preset(
    payload: CustomModelPresetCreateRequest,
    request: Request,
    _account: Account = Depends(get_current_account),
    _csrf: None = Depends(require_csrf),
) -> ApiResponse[ModelConfigView]:
    """添加或更新一个自定义模型到可选模型库（严禁明文密钥）。"""
    provider, model, base_url, api_key_env = _validate_preset_fields(
        provider=payload.provider,
        model=payload.model,
        base_url=payload.base_url,
        api_key_env=payload.api_key_env,
        max_output_tokens=payload.max_output_tokens,
        timeout_seconds=payload.timeout_seconds,
    )

    target_path = _resolve_config_target_path()
    existing_raw = _load_raw_payload(target_path)
    merged_roles = _build_baseline_roles(existing_raw)
    presets = _read_stored_presets(existing_raw)

    preset_id = _slugify_preset_id(payload.id or f"{provider}-{model}")
    existing_ids = {str(p.get("id") or "") for p in presets}
    if not payload.id and preset_id in existing_ids:
        suffix = 2
        while f"{preset_id}-{suffix}" in existing_ids:
            suffix += 1
        preset_id = f"{preset_id}-{suffix}"

    new_entry: dict[str, Any] = {
        "id": preset_id,
        "name": payload.name.strip(),
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "supports_tools": payload.supports_tools,
    }
    if payload.max_output_tokens is not None:
        new_entry["max_output_tokens"] = payload.max_output_tokens
    if payload.timeout_seconds is not None:
        new_entry["timeout_seconds"] = payload.timeout_seconds

    replaced = False
    for idx, item in enumerate(presets):
        if str(item.get("id") or "") == preset_id:
            presets[idx] = new_entry
            replaced = True
            break
    if not replaced:
        presets.append(new_entry)

    document: dict[str, Any] = {
        "version": str(existing_raw.get("version") or "ui-custom-1"),
        "currency": str(existing_raw.get("currency") or "CNY"),
        "roles": merged_roles,
        "custom_models": presets,
    }
    _atomic_write_document(target_path, document)
    return ApiResponse(
        request_id=get_request_id(request),
        data=_build_config_view(),
    )


@router.delete("/presets/{preset_id}", response_model=ApiResponse[ModelConfigView])
def delete_custom_model_preset(
    preset_id: str,
    request: Request,
    _account: Account = Depends(get_current_account),
    _csrf: None = Depends(require_csrf),
) -> ApiResponse[ModelConfigView]:
    """从自定义模型库移除指定预设（不影响已保存的角色配置）。"""
    target_path = _resolve_config_target_path()
    existing_raw = _load_raw_payload(target_path)
    merged_roles = _build_baseline_roles(existing_raw)
    presets = [
        item for item in _read_stored_presets(existing_raw) if str(item.get("id") or "") != preset_id
    ]

    document: dict[str, Any] = {
        "version": str(existing_raw.get("version") or "ui-custom-1"),
        "currency": str(existing_raw.get("currency") or "CNY"),
        "roles": merged_roles,
    }
    if presets:
        document["custom_models"] = presets

    _atomic_write_document(target_path, document)
    return ApiResponse(
        request_id=get_request_id(request),
        data=_build_config_view(),
    )
