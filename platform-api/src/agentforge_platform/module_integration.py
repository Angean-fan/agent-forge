"""F06：跨模块契约/集成验证、状态提交与恢复回滚。

把 F01（模块登记）、F03（接口登记）与 F02（模块记忆）在**提交一致性**上收口：不仅"模块各自成功"，
而要"项目整体成功"。

- **跨模块集成校验**（:func:`integration_issues`）：静态核对依赖图、接口所有者、Java/HTTP 边界、
  类型/权限一致性；**任一冲突 → 整个 run 不通过**（:func:`integration_ok`）。
- **可恢复提交状态机**（:class:`CommitStateMachine`）：``STAGED → CODE_COMMITTED → INTERFACES_PUBLISHED
  → MEMORY_UPDATED → DONE``；**先提交后对账**，不"先宣布成功后遗漏更新"。
- **对账**（:meth:`CommitStateMachine.reconcile`）：检出"代码已提交但接口/记忆未落库""接口已记录但验证失败"
  等不一致，供恢复或回滚。
- **回滚**（:meth:`CommitStateMachine.rollback`）：撤销未完成提交（弃用已记录接口、回退记忆指针），
  **不修改老 ZIP**。
- **验证绑定**（:func:`verification_stale`）：已验证的模块/接口绑定 ``contract_revision + source_hash +
  verification_ref``；**源码或依赖改变则旧验证失效**。

纯确定性、零模型调用；保留原 Runner/取消/预算/修复上限语义（本模块只提供一致性门禁与状态机）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.interface_registry import (
    STATUS_VERIFIED,
    InterfaceRegistry,
    deprecate,
    lookup,
)
from agentforge_platform.interface_registry import registry_issues as interface_issues
from agentforge_platform.module_memory import ModuleMemoryStore
from agentforge_platform.module_registry import (
    ModuleRegistry,
)
from agentforge_platform.module_registry import (
    registry_issues as module_issues,
)

INTEGRATION_VERSION = "0.1"
_STRICT = ConfigDict(extra="forbid")


class CommitStage(StrEnum):
    STAGED = "STAGED"
    CODE_COMMITTED = "CODE_COMMITTED"
    INTERFACES_PUBLISHED = "INTERFACES_PUBLISHED"
    MEMORY_UPDATED = "MEMORY_UPDATED"
    DONE = "DONE"
    ROLLED_BACK = "ROLLED_BACK"


class ModuleCommit(BaseModel):
    """一次模块提交的可恢复状态（`contract_revision + source_hash + verification_ref` 绑定验证）。"""

    model_config = _STRICT

    module_id: str
    task_id: str
    contract_revision: int
    source_hash: str
    stage: str = CommitStage.STAGED
    applied_code: list[str] = []
    published_capabilities: list[str] = []
    verification_ref: str | None = None
    memory_revision: int | None = None

    @field_validator("source_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        if len(value) != 64:
            raise ValueError(f"非法 source_hash（应为 64 位十六进制）：{value!r}")
        return value


# --------------------------------------------------------------------------- #
# 跨模块集成校验
# --------------------------------------------------------------------------- #
def integration_issues(
    module_registry: ModuleRegistry,
    interface_registry: InterfaceRegistry,
) -> list[str]:
    """静态一致性：模块登记 + 接口登记 + 接口所有者存在 + 依赖存在 + 类型/权限无冲突。"""
    issues = [f"模块：{i}" for i in module_issues(module_registry)]
    issues += [f"接口：{i}" for i in interface_issues(interface_registry)]

    module_ids = {m.module_id for m in module_registry.modules}
    for module in module_registry.modules:
        for dep in module.depends_on_modules:
            if dep not in module_ids:
                issues.append(f"{module.module_id} 依赖未知模块：{dep}")

    # 接口所有者必须是已登记模块；同一 capability_id 不得有多个 VERIFIED（类型冲突）
    verified: dict[str, list[str]] = {}
    for capability in interface_registry.capabilities:
        if capability.owner_module_id not in module_ids:
            issues.append(f"接口 {capability.capability_id} 所有者未登记：{capability.owner_module_id}")
        if capability.status == STATUS_VERIFIED:
            verified.setdefault(capability.capability_id, []).append(capability.signature)
    for capability_id, signatures in verified.items():
        if len(set(signatures)) > 1:
            issues.append(f"接口类型冲突：{capability_id} 存在多种签名 {'、'.join(sorted(set(signatures)))}")
    return issues


def integration_ok(module_registry: ModuleRegistry, interface_registry: InterfaceRegistry) -> bool:
    """完整一致性通过才允许生成 SUCCEEDED/ZIP。"""
    return not integration_issues(module_registry, interface_registry)


def verification_stale(
    commit: ModuleCommit, *, contract_revision: int, source_hash: str
) -> list[str]:
    """源码或契约变化 → 旧验证失效（绑定 `contract_revision + source_hash`）。"""
    reasons: list[str] = []
    if commit.contract_revision != contract_revision:
        reasons.append(f"契约版本变化：{commit.contract_revision} → {contract_revision}")
    if commit.source_hash != source_hash:
        reasons.append("源码 hash 变化")
    return reasons


class _InconsistentCommit(AppException):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.VALIDATION_ERROR, message, status_code=409)


class CommitStateMachine:
    """模块提交状态机：顺序推进 + 对账 + 回滚；崩溃/重放不产生双写。"""

    def __init__(self, *, interface_registry: InterfaceRegistry, memory_store: ModuleMemoryStore):
        self._interfaces = interface_registry
        self._memory = memory_store
        self._commits: dict[str, ModuleCommit] = {}

    def begin(
        self, *, module_id: str, task_id: str, contract_revision: int, source_hash: str
    ) -> ModuleCommit:
        commit = ModuleCommit(module_id=module_id, task_id=task_id,
                              contract_revision=contract_revision, source_hash=source_hash)
        self._commits[module_id] = commit
        return commit

    def _advance(self, module_id: str, *, expect: CommitStage, to: CommitStage, **updates) -> ModuleCommit:
        commit = self._commits.get(module_id)
        if commit is None:
            raise _InconsistentCommit(f"未知模块提交：{module_id}")
        if commit.stage != expect:
            raise _InconsistentCommit(
                f"{module_id} 阶段不符：期望 {expect}，实为 {commit.stage}")
        advanced = commit.model_copy(update={"stage": to, **updates})
        self._commits[module_id] = advanced
        return advanced

    def commit_code(self, module_id: str, applied_paths: list[str]) -> ModuleCommit:
        return self._advance(module_id, expect=CommitStage.STAGED, to=CommitStage.CODE_COMMITTED,
                             applied_code=sorted(set(applied_paths)))

    def publish_interfaces(
        self, module_id: str, capability_ids: list[str], *, verification_ref: str | None = None
    ) -> ModuleCommit:
        """登记待发布接口；**验证证据**由对账核验（缺失/未 VERIFIED → 不一致）。"""
        return self._advance(
            module_id, expect=CommitStage.CODE_COMMITTED, to=CommitStage.INTERFACES_PUBLISHED,
            published_capabilities=sorted(set(capability_ids)), verification_ref=verification_ref)

    def update_memory(self, module_id: str, *, memory_revision: int) -> ModuleCommit:
        return self._advance(module_id, expect=CommitStage.INTERFACES_PUBLISHED,
                             to=CommitStage.MEMORY_UPDATED, memory_revision=memory_revision)

    def finish(self, module_id: str) -> ModuleCommit:
        return self._advance(module_id, expect=CommitStage.MEMORY_UPDATED, to=CommitStage.DONE)

    def state(self, module_id: str) -> ModuleCommit | None:
        return self._commits.get(module_id)

    def reconcile(self, *, project_id: str) -> list[str]:
        """对账：检出"代码已提交但接口/记忆未落库""接口已记录但验证失败"等不一致。"""
        issues: list[str] = []
        for commit in self._commits.values():
            if commit.stage == CommitStage.ROLLED_BACK:
                continue
            for capability_id in commit.published_capabilities:
                capability = lookup(self._interfaces, capability_id, include_draft=True)
                if capability is None:
                    issues.append(f"{commit.module_id}: 代码已提交但接口未落库：{capability_id}")
                elif capability.status != STATUS_VERIFIED:
                    issues.append(
                        f"{commit.module_id}: 接口已记录但验证失败/未通过：{capability_id}（{capability.status}）")
            if commit.memory_revision is not None:
                memory = self._memory.load(project_id, commit.module_id)
                if memory is None or memory.memory_revision < commit.memory_revision:
                    issues.append(f"{commit.module_id}: 记忆未落库 revision={commit.memory_revision}")
            if commit.stage != CommitStage.DONE and (
                commit.applied_code or commit.published_capabilities
            ):
                issues.append(f"{commit.module_id}: 提交未完成（阶段 {commit.stage}）")
        return issues

    def rollback(self, module_id: str, *, project_id: str) -> ModuleCommit:
        """回滚未完成提交：弃用已记录接口 + 回退记忆指针；**不修改老 ZIP**。"""
        commit = self._commits.get(module_id)
        if commit is None:
            raise _InconsistentCommit(f"未知模块提交：{module_id}")
        for capability_id in commit.published_capabilities:
            capability = lookup(self._interfaces, capability_id, include_draft=True)
            if capability is None or capability.status != STATUS_VERIFIED:
                continue
            self._interfaces = deprecate(self._interfaces, capability_id, by_module_id=module_id)
        rolled = commit.model_copy(update={
            "stage": CommitStage.ROLLED_BACK, "published_capabilities": [],
            "memory_revision": None, "applied_code": [],
        })
        self._commits[module_id] = rolled
        return rolled


__all__ = [
    "INTEGRATION_VERSION",
    "CommitStage",
    "CommitStateMachine",
    "ModuleCommit",
    "integration_issues",
    "integration_ok",
    "verification_stale",
]
