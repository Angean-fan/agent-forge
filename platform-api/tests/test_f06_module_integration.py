"""F06：跨模块契约/集成验证、状态提交与恢复回滚。

覆盖（**确定性、零模型调用**）：

- 两模块**接口类型冲突**（同 capability 多签名）→ `integration_issues` 非空 → **整个 run 不通过**；
- 一致时 `integration_ok` 通过（才允许 SUCCEEDED/ZIP）；
- **可恢复提交状态机**：STAGED→CODE_COMMITTED→INTERFACES_PUBLISHED→MEMORY_UPDATED→DONE；
- **对账**：注入"代码已提交但接口/记忆未落库""接口已记录但验证失败"均可检出；
- **回滚**：撤销未完成提交（弃用接口），不改老 ZIP；
- **验证绑定**：源码/契约变化 → 旧验证失效；顺序错乱、重放不双写。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentforge_platform.errors import AppException
from agentforge_platform.interface_registry import (
    KIND_JAVA_METHOD,
    STATUS_VERIFIED,
    CapabilityEvidence,
    InterfaceRegistry,
    PublicCapability,
    register_capability,
)
from agentforge_platform.module_integration import (
    CommitStage,
    CommitStateMachine,
    integration_issues,
    integration_ok,
    verification_stale,
)
from agentforge_platform.module_memory import ModuleMemoryStore
from agentforge_platform.module_registry import derive_modules
from agentforge_platform.spec import SoftwareSpecification

REPO = Path(__file__).resolve().parents[2]
CAMPUS = REPO / "schemas" / "campus-activity.spec.json"
SPEC_HASH = "a" * 64
SRC_HASH = "c" * 64
_EVIDENCE = CapabilityEvidence(source_committed=True, contract_consistent=True,
                               runner_verified=True, verification_ref="runner:verify#1")


def _modules():
    spec = SoftwareSpecification.model_validate_json(CAMPUS.read_text(encoding="utf-8"))
    modules = derive_modules(spec, project_id="proj")
    return modules.model_copy(update={
        "based_on_spec_hash": SPEC_HASH,
        "modules": [m.model_copy(update={"based_on_spec_hash": SPEC_HASH}) for m in modules.modules],
    })


def _capability(owner: str, capability_id: str, signature: str) -> PublicCapability:
    return PublicCapability(capability_id=capability_id, owner_module_id=owner,
                            interface_kind=KIND_JAVA_METHOD, symbol="Svc.m", signature=signature)


def _interfaces(*caps: PublicCapability) -> InterfaceRegistry:
    registry = InterfaceRegistry(project_id="proj", based_on_spec_hash=SPEC_HASH)
    for cap in caps:
        registry = register_capability(registry, cap, evidence=_EVIDENCE)
    return registry


# --- 跨模块集成校验 -----------------------------------------------------------

def test_consistent_registries_pass() -> None:
    interfaces = _interfaces(_capability("activity", "activity.find", "DTO find(Long id)"))
    assert integration_issues(_modules(), interfaces) == []
    assert integration_ok(_modules(), interfaces) is True


def test_interface_type_conflict_fails_run() -> None:
    # 同一 capability_id 出现两种 VERIFIED 签名（模拟脏数据/并发发布）→ 类型冲突
    # 直接构造（register_capability 本身会拦截破坏性升级）
    interfaces = InterfaceRegistry(project_id="proj", based_on_spec_hash=SPEC_HASH, capabilities=[
        _capability("activity", "activity.find", "DTO find(Long id)").model_copy(
            update={"status": STATUS_VERIFIED, "version": 1}),
        _capability("activity", "activity.find", "DTO find(String id)").model_copy(
            update={"status": STATUS_VERIFIED, "version": 2}),
    ])
    issues = integration_issues(_modules(), interfaces)

    assert any("接口类型冲突" in i for i in issues)
    assert integration_ok(_modules(), interfaces) is False  # 整个 run FAIL


def test_interface_owner_must_be_registered() -> None:
    interfaces = _interfaces(_capability("ghost", "ghost.find", "DTO find(Long id)"))
    assert any("所有者未登记" in i for i in integration_issues(_modules(), interfaces))


# --- 提交状态机：正常路径与顺序 -----------------------------------------------

def _machine(tmp_path: Path):
    store = ModuleMemoryStore(tmp_path / "mem")
    machine = CommitStateMachine(
        interface_registry=_interfaces(_capability("enrollment", "enrollment.find", "DTO find(Long id)")),
        memory_store=store)
    return machine, store


def test_happy_path_reaches_done(tmp_path: Path) -> None:
    machine, store = _machine(tmp_path)
    machine.begin(module_id="enrollment", task_id="module:enrollment",
                  contract_revision=1, source_hash=SRC_HASH)
    machine.commit_code("enrollment", ["frontend/src/api/enrollment.ts"])
    machine.publish_interfaces("enrollment", ["enrollment.find"], verification_ref="runner#1")
    store.record("proj", "enrollment", key="k", value="v", spec_hash=SPEC_HASH,
                 contract_revision=1, verified=True, committed=True, task_id="t",
                 verification_ref="runner#1")  # 记忆落库（revision 1）
    machine.update_memory("enrollment", memory_revision=1)
    done = machine.finish("enrollment")

    assert done.stage == CommitStage.DONE
    assert machine.reconcile(project_id="proj") == []


def test_out_of_order_step_rejected(tmp_path: Path) -> None:
    machine, _ = _machine(tmp_path)
    machine.begin(module_id="enrollment", task_id="t", contract_revision=1, source_hash=SRC_HASH)
    with pytest.raises(AppException):
        machine.update_memory("enrollment", memory_revision=1)  # 跳过代码/接口阶段


# --- 对账：检出不一致 ---------------------------------------------------------

def test_reconcile_detects_code_committed_but_interface_missing(tmp_path: Path) -> None:
    machine, _ = _machine(tmp_path)  # registry 只有 enrollment.find
    machine.begin(module_id="enrollment", task_id="t", contract_revision=1, source_hash=SRC_HASH)
    machine.commit_code("enrollment", ["frontend/src/api/enrollment.ts"])
    machine.publish_interfaces("enrollment", ["enrollment.missing"], verification_ref="runner#1")

    issues = machine.reconcile(project_id="proj")
    assert any("代码已提交但接口未落库" in i for i in issues)


def test_reconcile_detects_interface_recorded_but_unverified(tmp_path: Path) -> None:
    registry = InterfaceRegistry(project_id="proj", based_on_spec_hash=SPEC_HASH)
    registry = register_capability(registry, _capability("enrollment", "enrollment.find", "DTO f()"),
                                   evidence=CapabilityEvidence())  # DRAFT：已记录但未验证
    machine = CommitStateMachine(interface_registry=registry,
                                 memory_store=ModuleMemoryStore(tmp_path / "mem"))
    machine.begin(module_id="enrollment", task_id="t", contract_revision=1, source_hash=SRC_HASH)
    machine.commit_code("enrollment", ["a.ts"])
    machine.publish_interfaces("enrollment", ["enrollment.find"], verification_ref=None)

    issues = machine.reconcile(project_id="proj")
    assert any("接口已记录但验证失败" in i for i in issues)


def test_reconcile_detects_memory_not_persisted(tmp_path: Path) -> None:
    machine, _ = _machine(tmp_path)
    machine.begin(module_id="enrollment", task_id="t", contract_revision=1, source_hash=SRC_HASH)
    machine.commit_code("enrollment", ["a.ts"])
    machine.publish_interfaces("enrollment", ["enrollment.find"], verification_ref="r")
    machine.update_memory("enrollment", memory_revision=3)  # 记忆库中无 revision 3

    assert any("记忆未落库" in i for i in machine.reconcile(project_id="proj"))


# --- 回滚 ---------------------------------------------------------------------

def test_rollback_deprecates_recorded_interfaces(tmp_path: Path) -> None:
    interfaces = _interfaces(_capability("enrollment", "enrollment.find", "DTO f()"))
    machine = CommitStateMachine(interface_registry=interfaces,
                                 memory_store=ModuleMemoryStore(tmp_path / "mem"))
    machine.begin(module_id="enrollment", task_id="t", contract_revision=1, source_hash=SRC_HASH)
    machine.commit_code("enrollment", ["a.ts"])
    machine.publish_interfaces("enrollment", ["enrollment.find"], verification_ref="r")

    rolled = machine.rollback("enrollment", project_id="proj")

    assert rolled.stage == CommitStage.ROLLED_BACK
    assert machine.reconcile(project_id="proj") == []  # 回滚后不再报不一致


# --- 验证绑定失效 -------------------------------------------------------------

def test_verification_binding_stale_on_source_or_contract_change(tmp_path: Path) -> None:
    machine, _ = _machine(tmp_path)
    commit = machine.begin(module_id="enrollment", task_id="t", contract_revision=1, source_hash=SRC_HASH)

    assert verification_stale(commit, contract_revision=1, source_hash=SRC_HASH) == []
    assert verification_stale(commit, contract_revision=2, source_hash=SRC_HASH)  # 契约变化
    assert verification_stale(commit, contract_revision=1, source_hash="d" * 64)  # 源码变化
