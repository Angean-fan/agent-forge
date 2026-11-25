"""C01：生产模型路由注入（S2/S3）。

把 R13 技术规划、R16 编码、R18 复杂修复、R19 语义审查的**已实现调用能力**，按**显式开关与凭据**
注入生产 `worker` / `default_context` / `GenerationContext`。默认全关，**关闭时标准资产路径零 Qwen 请求**。

设计约束（对齐《AgentForge_03》C01 与全局规则）：

- 路由真值仍来自 :mod:`agentforge_platform.model_gateway`（不新增模型、不新增"路由决策模型"）。
- 缺凭据 → 保持**安全禁用**并报 ``CONFIG_REQUIRED``（**不发请求**，不打印密钥）。
- 密钥只在构造客户端时从环境变量读取，不写入代码/配置/日志/产物。
- 编码/修复的受限引擎需 run 级账本上下文（``LedgerBinding`` 需要 ``run_id/project_id``），
  故以**运行级构造器** :class:`CodingRouting` 注入：工厂签名与既有 ``(allowed_paths, task_id)`` /
  ``(role, allowed_paths, task_id)`` 保持一致。

本模块**只做构造**，不改变任何既有关卡；实际调用仍走 `model_gateway.reserve/settle` 与 DeepAgent 受限工具表。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform.agent_files import FileBackend
from agentforge_platform.clients import (
    DashScopeClient,
    DeepSeekClient,
    ModelClient,
    OpenAICompatibleClient,
)
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.limits import model_timeout_seconds
from agentforge_platform.model_gateway import (
    DEFAULT_DEEPSEEK_BASE_URL,
    FACTORY_ROLES,
    ROUTES,
    TOOL_REQUIRED_ROLES,
    Route,
    load_custom_routes,
    role_enabled,
)
from agentforge_platform.patch_engine import (
    LedgerBinding,
    LedgerChatModel,
    build_patch_engine,
    dashscope_base_url,
)
from agentforge_platform.runs import run_directory

PLANNING_ROLE = "technical_planning"
REVIEW_ROLE = "technical_planning"
CODING_ROLE = "coding"
COMPLEX_ROLE = "complex_repair"

DASHSCOPE_KEY_ENV = "DASHSCOPE_API_KEY"

# 受限编码 Agent 的固定系统提示（与业务无关，只约束"最小修改、只写授权路径"）。
CODING_SYSTEM_PROMPT = (
    "你是受控的单任务补缺编码器。仅依据用户提供的**已冻结上下文**做最小修改，"
    "只写 allowed_paths 内的文件，不得读取或写入其他路径，不得执行命令，"
    "不得扩大需求、升级依赖、修改受保护文件或固定测试。完成后停止。"
)

_UNKNOWN = "UNKNOWN"


def dashscope_key_present() -> bool:
    """是否配置了百炼密钥（**只返回布尔**，绝不回显密钥值）。"""
    return bool(os.environ.get(DASHSCOPE_KEY_ENV))


def _effective_route(role: str) -> Route:
    custom = load_custom_routes()
    if role in custom:
        return custom[role]
    if role in FACTORY_ROLES:
        fallback = FACTORY_ROLES[role]
        if fallback in custom:
            base = custom[fallback]
            if role in TOOL_REQUIRED_ROLES and not base.supports_tools:
                raise AppException(
                    ErrorCode.VALIDATION_ERROR,
                    f"角色 {role} 回退到的角色 {fallback} 不支持工具调用（supports_tools=false）",
                    status_code=422,
                )
            return replace(base, role=role)
        if fallback in ROUTES:
            base = ROUTES[fallback]
            override = os.environ.get(f"AGENTFORGE_{role.upper()}_MODEL") or base.model
            return replace(base, role=role, model=override)
    if role in ROUTES:
        return ROUTES[role]
    raise AppException(
        ErrorCode.VALIDATION_ERROR,
        f"未知的模型角色：{role!r}",
        status_code=422,
    )


def _route_endpoint(route: Route) -> str:
    if os.environ.get("AGENTFORGE_MODELS_CONFIG", "").strip() and route.base_url:
        return route.base_url
    if route.provider == "deepseek_official":
        return route.base_url or DEFAULT_DEEPSEEK_BASE_URL
    return dashscope_base_url()


def resolved_model_name(role: str) -> str:
    """角色实际模型名：优先 ``AGENTFORGE_<ROLE>_MODEL`` 覆盖，否则用自定义或默认路由表真值。"""
    override = os.environ.get(f"AGENTFORGE_{role.upper()}_MODEL")
    if override:
        return override
    return _effective_route(role).model


def build_client_for_role(role: str) -> OpenAICompatibleClient:
    """A04：为给定角色（含工厂角色）构造绑定该角色路由的 ``OpenAICompatibleClient``。

    缺密钥时抛 ``AppException(CONFIG_REQUIRED)``，绝不打印密钥。
    """
    route = _effective_route(role)
    if not role_enabled(role):
        raise AppException(
            ErrorCode.MODEL_ROLE_DISABLED,
            f"模型角色 {role} 未启用",
            status_code=422,
        )
    api_key = os.environ.get(route.api_key_env, "").strip()
    if not api_key:
        raise AppException(
            ErrorCode.CONFIG_REQUIRED,
            f"角色 {role} 缺少环境变量密钥：{route.api_key_env}",
            status_code=422,
        )
    return OpenAICompatibleClient(
        provider=route.provider,
        api_key_env=route.api_key_env,
        base_url=_route_endpoint(route),
        default_max_output_tokens=route.max_output_tokens,
        supports_tools=route.supports_tools,
        timeout_seconds=route.timeout_seconds,
    )


def default_user_interaction_client(client: ModelClient | None = None) -> ModelClient:
    """解析 ``user_interaction`` 客户端：优先显式注入，其次按 A04 ``AGENTFORGE_MODELS_CONFIG``，最后回退 ``DeepSeekClient``。"""
    if client is not None:
        return client
    if os.environ.get("AGENTFORGE_MODELS_CONFIG", "").strip():
        return build_client_for_role("user_interaction")
    return DeepSeekClient()


def build_ledger_model(role: str, binding: LedgerBinding) -> LedgerChatModel:
    """为给定角色（含工厂角色）构造账本模型（OpenAI 兼容端点）；缺密钥在构造时即拦截。"""
    route = _effective_route(role)
    if role in TOOL_REQUIRED_ROLES and not route.supports_tools:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"编码/测试角色 {role} 要求 supports_tools=true",
            status_code=422,
        )
    api_key = os.environ.get(route.api_key_env, "").strip()
    if not api_key:
        raise AppException(
            ErrorCode.CONFIG_REQUIRED,
            f"环境变量 {route.api_key_env} 未配置",
            status_code=422,
        )
    max_tokens = route.max_output_tokens or binding.max_output_tokens
    role_binding = replace(binding, model_role=role, max_output_tokens=max_tokens)
    timeout = route.timeout_seconds if route.timeout_seconds is not None else model_timeout_seconds()
    return LedgerChatModel(
        ledger_binding=role_binding,
        model=resolved_model_name(role),
        base_url=_route_endpoint(route),
        api_key=api_key,
        max_tokens=max_tokens,
        timeout=timeout,
        temperature=0,
    )


@dataclass(frozen=True)
class CodingRouting:
    """运行级编码路由：按 ``run_id/project_id`` 生成绑定好账本上下文的受限引擎工厂。

    物理上共享同一 ``session_factory`` 与 ``work_root``；逻辑上每个 run/task 独立 ``LedgerBinding``。
    """

    session_factory: sessionmaker[Session]
    work_root: Path

    def _file_backend(self, run_id: int, allowed_paths: list[str]) -> FileBackend:
        return FileBackend(run_directory(self.work_root, run_id), list(allowed_paths))

    def gap_engine_factory(
        self, *, run_id: int, project_id: int
    ) -> Callable[[list[str], str], tuple[Any, FileBackend]]:
        """R17 缺口 Patch 工厂：``(allowed_paths, task_id) -> (PatchEngine, FileBackend)``。"""

        def factory(allowed_paths: list[str], task_id: str) -> tuple[Any, FileBackend]:
            binding = LedgerBinding(
                session_factory=self.session_factory,
                project_id=project_id,
                run_id=run_id,
                task_id=task_id,
                model_role=CODING_ROLE,
            )
            file_backend = self._file_backend(run_id, allowed_paths)
            engine = build_patch_engine(
                ledger_binding=binding,
                file_backend=file_backend,
                system_prompt=CODING_SYSTEM_PROMPT,
                model=build_ledger_model(CODING_ROLE, binding),
            )
            return engine, file_backend

        return factory

    def repair_engine_factory(
        self, *, run_id: int, project_id: int
    ) -> Callable[[str, list[str], str], tuple[Any, FileBackend]]:
        """R18 失败修复工厂：``(role, allowed_paths, task_id) -> (PatchEngine, FileBackend)``。

        ``role`` 由 :func:`repair.choose_repair_role` 决定（首修 coding，升级 complex_repair）；
        角色是否真正可用仍由 ``model_gateway.resolve_route`` 按开关二次把关。
        """

        def factory(role: str, allowed_paths: list[str], task_id: str) -> tuple[Any, FileBackend]:
            model_role = role if role in (CODING_ROLE, COMPLEX_ROLE) else CODING_ROLE
            binding = LedgerBinding(
                session_factory=self.session_factory,
                project_id=project_id,
                run_id=run_id,
                task_id=task_id,
                model_role=model_role,
            )
            file_backend = self._file_backend(run_id, allowed_paths)
            engine = build_patch_engine(
                ledger_binding=binding,
                file_backend=file_backend,
                system_prompt=CODING_SYSTEM_PROMPT,
                model=build_ledger_model(model_role, binding),
            )
            return engine, file_backend

        return factory


@dataclass(frozen=True)
class RoleStatus:
    """单个角色的**脱敏**审计状态（无密钥值）。"""

    role: str
    provider: str
    model: str
    flag_on: bool
    key_present: bool
    enabled: bool
    reason: str | None
    endpoint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "flag_on": self.flag_on,
            "key_present": self.key_present,
            "enabled": self.enabled,
            "reason": self.reason,
            "endpoint": self.endpoint or dashscope_base_url(),
        }


def role_status(role: str) -> RoleStatus:
    """获取任意已知角色（含工厂角色）的脱敏就绪状态。"""
    route = _effective_route(role)
    flag_on = role_enabled(role)
    key_present = bool(os.environ.get(route.api_key_env, "").strip())
    enabled = flag_on and key_present
    if not flag_on:
        reason = "DISABLED"
    elif not key_present:
        reason = ErrorCode.CONFIG_REQUIRED.value
    else:
        reason = None
    return RoleStatus(
        role=role,
        provider=route.provider,
        model=resolved_model_name(role),
        flag_on=flag_on,
        key_present=key_present,
        enabled=enabled,
        reason=reason,
        endpoint=_route_endpoint(route),
    )


def _status(role: str) -> RoleStatus:
    return role_status(role)


@dataclass(frozen=True)
class ProductionRouting:
    """生产入口可用的已授权注入项；未就绪项一律为 ``None``（安全禁用）。"""

    planning_client: ModelClient | None
    review_client: ModelClient | None
    coding_routing: CodingRouting | None
    status: tuple[RoleStatus, ...]

    def status_dict(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self.status]

    def any_enabled(self) -> bool:
        return any(entry.enabled for entry in self.status)


def build_production_routing(
    *, session_factory: sessionmaker[Session], work_root: str | Path
) -> ProductionRouting:
    """按环境开关与凭据构造生产注入项。**关闭或缺凭据时为 ``None``，不发起任何请求。**"""
    statuses = tuple(_status(role) for role in (PLANNING_ROLE, CODING_ROLE, COMPLEX_ROLE))
    by_role = {entry.role: entry for entry in statuses}

    planning_client: ModelClient | None = None
    review_client: ModelClient | None = None
    if by_role[PLANNING_ROLE].enabled:
        # 规划与审查同属 technical_planning 角色；支持自定义多 Provider 或默认 DashScopeClient。
        if os.environ.get("AGENTFORGE_MODELS_CONFIG", "").strip():
            planning_client = build_client_for_role(PLANNING_ROLE)
        else:
            planning_client = DashScopeClient()
        review_client = planning_client

    coding_routing: CodingRouting | None = None
    if by_role[CODING_ROLE].enabled or by_role[COMPLEX_ROLE].enabled:
        coding_routing = CodingRouting(session_factory=session_factory, work_root=Path(work_root))

    return ProductionRouting(
        planning_client=planning_client,
        review_client=review_client,
        coding_routing=coding_routing,
        status=statuses,
    )


__all__ = [
    "CODING_ROLE",
    "CODING_SYSTEM_PROMPT",
    "COMPLEX_ROLE",
    "DASHSCOPE_KEY_ENV",
    "PLANNING_ROLE",
    "REVIEW_ROLE",
    "CodingRouting",
    "ProductionRouting",
    "RoleStatus",
    "build_client_for_role",
    "build_ledger_model",
    "build_production_routing",
    "dashscope_key_present",
    "default_user_interaction_client",
    "resolved_model_name",
    "role_status",
]
