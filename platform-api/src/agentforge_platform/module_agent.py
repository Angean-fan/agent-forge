"""F04：按模块实例化受限 DeepAgent（静态委派、独立上下文、受控 Patch）。

在 R16/R17 单任务 patch engine 之上引入**按需的逻辑模块 Agent**（``ModuleAgentFactory`` /
``ModuleExecutor``）：为每个已分配任务加载**本模块**配置、专属记忆与**已批准公共契约**，
用原有 DeepAgent 处理真实业务缺口。

隔离与边界（对齐《AgentForge_03》F04）：

- 多个模块是**不同逻辑上下文/权限配置**，不一定是不同模型/进程；调度权归 Python/Coordinator，
  模块 Agent **不得**自发启动其他 Agent、追加工具或自行宣布发布完成；
- 上下文仅含：本模块冻结规格片段 + **本模块私有记忆** + 其他模块的**已批准公共契约摘要** + Gap + 受控源码；
  **不含**他模块私有记忆/源码；
- 写入生效权限 = ``task.allowed_paths ∩ module.owned_paths``；产出 :class:`FileChangeSet`，**不在此提交**；
- 全部子调用继承真实 ``run_id/task_id/call_id`` 与限额（由注入的引擎工厂负责）；
- **默认关闭**：``AGENTFORGE_MODULE_AGENTS_ENABLED`` 未开启时回退既有单执行器，无额外模型调用。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.interface_registry import (
    STATUS_VERIFIED,
    InterfaceRegistry,
    PublicCapability,
)
from agentforge_platform.module_memory import MemoryEntry, MemoryView, ModuleMemoryStore
from agentforge_platform.module_registry import (
    Module,
    ModuleRegistry,
    effective_write_scope,
)

FLAG_ENV = "AGENTFORGE_MODULE_AGENTS_ENABLED"
MODULE_TASK_PREFIX = "module:"


def module_agents_enabled() -> bool:
    """模块 Agents 是否开启（默认关闭 → 回退既有单执行器路径）。"""
    return os.environ.get(FLAG_ENV, "").strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class ModuleContext:
    """单个模块 Agent 的**只读**逻辑上下文（严格隔离）。"""

    module_id: str
    module_name: str
    spec_hash: str
    contract_revision: int
    profile_ref: str
    owned_paths: tuple[str, ...]
    depends_on_modules: tuple[str, ...]
    memory: tuple[MemoryEntry, ...]
    readable_interfaces: tuple[PublicCapability, ...]
    gap: dict | None = None


@dataclass(frozen=True)
class ModuleRunResult:
    module_id: str
    invoked: bool
    coding_calls: int
    allowed_paths: tuple[str, ...]
    change_set: dict | None
    reason: str | None = None


def _readable_interfaces(registry: InterfaceRegistry, *, exclude_module: str) -> tuple[PublicCapability, ...]:
    """他模块的**已批准**（VERIFIED）公共契约摘要；不含 DRAFT/弃用，也不含任何私有实现。"""
    latest: dict[str, PublicCapability] = {}
    for capability in registry.capabilities:
        if capability.owner_module_id == exclude_module or capability.status != STATUS_VERIFIED:
            continue
        current = latest.get(capability.capability_id)
        if current is None or capability.version > current.version:
            latest[capability.capability_id] = capability
    return tuple(latest[key] for key in sorted(latest))


def build_module_context(
    module: Module,
    *,
    memory_view: MemoryView,
    interface_registry: InterfaceRegistry,
    gap: dict | None = None,
) -> ModuleContext:
    """组装模块上下文：**仅**本模块记忆 + 他模块已批准公共契约（摘要）+ Gap。"""
    if memory_view.module_id != module.module_id:
        raise AppException(
            ErrorCode.FORBIDDEN, "模块记忆与模块不匹配：拒绝组装上下文。", status_code=403
        )
    return ModuleContext(
        module_id=module.module_id,
        module_name=module.name,
        spec_hash=module.based_on_spec_hash,
        contract_revision=module.contract_revision,
        profile_ref=module.profile_ref,
        owned_paths=tuple(module.owned_paths),
        depends_on_modules=tuple(module.depends_on_modules),
        memory=tuple(memory_view.entries),
        readable_interfaces=_readable_interfaces(interface_registry, exclude_module=module.module_id),
        gap=gap,
    )


def render_module_prompt(context: ModuleContext) -> str:
    """确定性模块提示：**只**含本模块上下文（不含他模块私有记忆/源码路径）。"""
    payload = {
        "module": context.module_id,
        "profile_ref": context.profile_ref,
        "spec_hash": context.spec_hash,
        "contract_revision": context.contract_revision,
        "owned_paths": list(context.owned_paths),
        "depends_on_modules": list(context.depends_on_modules),
        "private_memory": [
            {"key": e.key, "value": e.value, "verification_ref": e.verification_ref}
            for e in context.memory
        ],
        "readable_interfaces": [
            {"capability_id": c.capability_id, "owner_module_id": c.owner_module_id,
             "interface_kind": c.interface_kind, "symbol": c.symbol, "signature": c.signature,
             "errors": c.errors, "auth_constraints": c.auth_constraints, "version": c.version}
            for c in context.readable_interfaces
        ],
        "gap": context.gap,
    }
    return (
        "你是**单模块受限编码 Agent**。仅依据下列**本模块**上下文做最小修改，只写 owned_paths 内文件，"
        "不得读取或写入他模块私有内容，不得启动其他 Agent、追加工具或自行宣布发布完成；完成后停止。\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    )


def _within_any(path: str, prefixes: list[str]) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/") for p in prefixes)


class ModuleExecutor:
    """一次受限模块运行：无缺口不启动 Agent；有缺口 → 交接受限引擎产出变更集（不提交）。"""

    def __init__(self, *, context: ModuleContext, engine_factory: Callable[[list[str], str], tuple[Any, Any]]):
        self.context = context
        self._engine_factory = engine_factory

    def run(self, *, task_allowed_paths: list[str]) -> ModuleRunResult:
        if self.context.gap is None:
            return ModuleRunResult(self.context.module_id, False, 0, (), None,
                                   "无缺口：不启动编码 Agent")
        allowed, issues = effective_write_scope(task_allowed_paths, list(self.context.owned_paths))
        if issues:
            raise AppException(
                ErrorCode.FORBIDDEN, "；".join(issues) + f"（module={self.context.module_id}）",
                status_code=403,
            )
        engine, _backend = self._engine_factory(allowed, f"{MODULE_TASK_PREFIX}{self.context.module_id}")
        result = engine.run(render_module_prompt(self.context))
        change_set = result.get("change_set", {}) or {}
        for change in change_set.get("changes", []):
            path = change.get("path", "")
            if not _within_any(path, allowed):
                raise AppException(
                    ErrorCode.FORBIDDEN,
                    f"{self.context.module_id} 越权写入：{path} 不在 {allowed}",
                    status_code=403,
                )
        return ModuleRunResult(self.context.module_id, True, 1, tuple(allowed), change_set)


class ModuleAgentFactory:
    """按模块构造逻辑 Agent：共享模型/进程，**隔离**上下文、记忆与权限。"""

    def __init__(
        self,
        *,
        module_registry: ModuleRegistry,
        memory_store: ModuleMemoryStore,
        interface_registry: InterfaceRegistry,
        engine_factory: Callable[[list[str], str], tuple[Any, Any]],
        contract_revision: int | None = None,
    ) -> None:
        self._modules = {m.module_id: m for m in module_registry.modules}
        self._memory_store = memory_store
        self._interfaces = interface_registry
        self._engine_factory = engine_factory
        self._contract_revision = contract_revision

    def context_for(self, module_id: str, *, gap: dict | None = None) -> ModuleContext:
        module = self._modules.get(module_id)
        if module is None:
            raise AppException(ErrorCode.NOT_FOUND, f"未知模块：{module_id}", status_code=404)
        view = self._memory_store.read(
            module.project_id, module_id, spec_hash=module.based_on_spec_hash,
            contract_revision=self._contract_revision if self._contract_revision is not None
            else module.contract_revision,
        )
        return build_module_context(
            module, memory_view=view, interface_registry=self._interfaces, gap=gap
        )

    def executor_for(self, module_id: str, *, gap: dict | None = None) -> ModuleExecutor:
        return ModuleExecutor(
            context=self.context_for(module_id, gap=gap), engine_factory=self._engine_factory
        )


__all__ = [
    "FLAG_ENV",
    "MODULE_TASK_PREFIX",
    "ModuleAgentFactory",
    "ModuleContext",
    "ModuleExecutor",
    "ModuleRunResult",
    "build_module_context",
    "module_agents_enabled",
    "render_module_prompt",
]
