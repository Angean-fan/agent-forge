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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform.agent_files import FileBackend
from agentforge_platform.clients import DashScopeClient, ModelClient
from agentforge_platform.errors import ErrorCode
from agentforge_platform.model_gateway import ROUTES, role_enabled
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


def resolved_model_name(role: str) -> str:
    """角色实际模型名：优先 ``AGENTFORGE_<ROLE>_MODEL`` 覆盖，否则用路由表真值。"""
    override = os.environ.get(f"AGENTFORGE_{role.upper()}_MODEL")
    return override or ROUTES[role].model


def build_ledger_model(role: str, binding: LedgerBinding) -> LedgerChatModel:
    """为给定角色构造账本模型（百炼 OpenAI 兼容端点）；缺密钥由调用方在此之前拦截。"""
    api_key = os.environ.get(DASHSCOPE_KEY_ENV)
    if not api_key:
        # 正常路径下不会到达（构造前已按凭据门控）；此处为最后一道防御，且不回显密钥。
        raise RuntimeError(f"环境变量 {DASHSCOPE_KEY_ENV} 未配置")
    return LedgerChatModel(
        ledger_binding=binding,
        model=resolved_model_name(role),
        base_url=dashscope_base_url(),
        api_key=api_key,
        max_tokens=binding.max_output_tokens,
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "flag_on": self.flag_on,
            "key_present": self.key_present,
            "enabled": self.enabled,
            "reason": self.reason,
            "endpoint": dashscope_base_url(),
        }


def _status(role: str) -> RoleStatus:
    route = ROUTES[role]
    flag_on = role_enabled(role)
    key_present = dashscope_key_present()
    enabled = flag_on and key_present
    if not flag_on:
        reason = "DISABLED"
    elif not key_present:
        reason = ErrorCode.CONFIG_REQUIRED.value
    else:
        reason = None
    return RoleStatus(role, route.provider, resolved_model_name(role), flag_on, key_present, enabled, reason)


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
        # 规划与审查同属 technical_planning 角色；共享一个无状态客户端实例。
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
    "build_ledger_model",
    "build_production_routing",
    "dashscope_key_present",
    "resolved_model_name",
]
