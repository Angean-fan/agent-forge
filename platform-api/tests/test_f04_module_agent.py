"""F04：按模块实例化受限 DeepAgent（独立上下文、静态委派、受控 Patch）。

覆盖（**确定性、零真实模型调用**；脚本化引擎）：

- 默认关闭 → 回退既有单执行器（生成图不引用本模块）；
- 双模块**上下文隔离**：A 的上下文含本模块私有记忆与他模块**已批准公共契约摘要**，**不含**他模块私有记忆/路径；
- 无缺口 → 编码请求数为零；有缺口 → 仅该模块经**交集** allowed_paths 才启动受限引擎；
- 越权写入（越出交集）被工具层/校验拒绝；空交集阻塞；
- 不自动提交（产出 FileChangeSet）；重复运行结果一致、不重复提交。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentforge_platform.errors import AppException
from agentforge_platform.interface_registry import (
    KIND_JAVA_METHOD,
    CapabilityEvidence,
    InterfaceRegistry,
    PublicCapability,
    register_capability,
)
from agentforge_platform.module_agent import (
    FLAG_ENV,
    ModuleAgentFactory,
    build_module_context,
    module_agents_enabled,
    render_module_prompt,
)
from agentforge_platform.module_memory import ModuleMemoryStore
from agentforge_platform.module_registry import derive_modules
from agentforge_platform.spec import SoftwareSpecification

REPO = Path(__file__).resolve().parents[2]
CAMPUS = REPO / "schemas" / "campus-activity.spec.json"
SPEC_HASH = "a" * 64
_EVIDENCE = CapabilityEvidence(source_committed=True, contract_consistent=True,
                               runner_verified=True, verification_ref="runner:verify#1")


def _spec() -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(CAMPUS.read_text(encoding="utf-8"))


def _memory(tmp_path: Path, module_id: str, value: str) -> ModuleMemoryStore:
    store = ModuleMemoryStore(tmp_path / "mem")
    store.record("proj", module_id, key="decision", value=value, spec_hash=SPEC_HASH,
                 contract_revision=0, verified=True, committed=True,
                 task_id="t1", verification_ref="runner#1")
    return store


def _interfaces(module_id: str, capability_id: str) -> InterfaceRegistry:
    registry = InterfaceRegistry(project_id="proj", based_on_spec_hash=SPEC_HASH)
    return register_capability(registry, PublicCapability(
        capability_id=capability_id, owner_module_id=module_id, interface_kind=KIND_JAVA_METHOD,
        symbol="Svc.find", signature="DTO find(Long id)", errors=["NOT_FOUND"],
        auth_constraints=["authenticated"], version=1), evidence=_EVIDENCE)


def _modules():
    """模块登记绑定到 SPEC_HASH（与记忆的 spec_hash 一致）。"""
    modules = derive_modules(_spec(), project_id="proj")
    return modules.model_copy(update={
        "based_on_spec_hash": SPEC_HASH,
        "modules": [m.model_copy(update={"based_on_spec_hash": SPEC_HASH}) for m in modules.modules],
    })


def _factory(tmp_path: Path) -> ModuleAgentFactory:
    # activity 模块提供公共接口；enrollment 消费它
    interfaces = _interfaces("activity", "activity.find")
    store = ModuleMemoryStore(tmp_path / "mem")
    store.record("proj", "enrollment", key="rules", value="报名需唯一",
                 spec_hash=SPEC_HASH, contract_revision=0, verified=True, committed=True,
                 task_id="t", verification_ref="runner#1")
    return ModuleAgentFactory(
        module_registry=_modules(), memory_store=store, interface_registry=interfaces,
        engine_factory=_engine_factory(), contract_revision=0)


def _engine_factory():
    def factory(allowed, task_id):
        class _Engine:
            def run(self, prompt):
                return {"change_set": {"changes": [
                    {"path": p, "action": "create"} for p in _TARGETS.get(task_id, [])
                ]}}

        factory.allowed = allowed  # 供断言
        factory.task_id = task_id
        return _Engine(), None

    return factory


_TARGETS: dict[str, list[str]] = {}


# --- 开关与回退 ---------------------------------------------------------------

def test_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv(FLAG_ENV, raising=False)
    assert module_agents_enabled() is False

    monkeypatch.setenv(FLAG_ENV, "1")
    assert module_agents_enabled() is True


def test_not_wired_into_generation_graph() -> None:
    graph = (REPO / "platform-api" / "src" / "agentforge_platform" / "generation_graph.py").read_text(
        encoding="utf-8")
    assert "module_agent" not in graph  # 关闭开关时旧路径不变


# --- 上下文隔离 ---------------------------------------------------------------

def test_context_isolation_between_modules(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    enrollment = factory.context_for("enrollment")

    assert enrollment.module_id == "enrollment"
    assert [e.value for e in enrollment.memory] == ["报名需唯一"]  # 仅本模块私有记忆
    # 他模块的**已批准公共契约摘要**可见（供编译调用），但无他模块私有记忆
    assert [c.capability_id for c in enrollment.readable_interfaces] == ["activity.find"]
    assert "activity" not in {e.value for e in enrollment.memory}


def test_prompt_contains_only_own_context(tmp_path: Path) -> None:
    prompt = render_module_prompt(_factory(tmp_path).context_for("enrollment"))

    assert "报名需唯一" in prompt
    assert "activity.find" in prompt and "Svc.find" in prompt  # 只含公共契约摘要
    # 不含他模块的私有 owned_paths
    activity = derive_modules(_spec(), project_id="proj").modules
    activity_paths = next(m.owned_paths for m in activity if m.module_id == "activity")
    assert not any(p in prompt for p in activity_paths)


def test_readable_interfaces_only_verified_from_others(tmp_path: Path) -> None:
    store = ModuleMemoryStore(tmp_path / "mem")
    registry = InterfaceRegistry(project_id="proj", based_on_spec_hash=SPEC_HASH)
    registry = register_capability(registry, PublicCapability(  # 他模块 DRAFT → 不可见
        capability_id="activity.draft", owner_module_id="activity", interface_kind=KIND_JAVA_METHOD,
        symbol="Svc.d", signature="void d()"), evidence=CapabilityEvidence())
    registry = register_capability(registry, PublicCapability(  # 本模块自身 → 不可见
        capability_id="enrollment.own", owner_module_id="enrollment", interface_kind=KIND_JAVA_METHOD,
        symbol="Svc.o", signature="void o()"), evidence=_EVIDENCE)
    factory = ModuleAgentFactory(module_registry=_modules(), memory_store=store,
                                 interface_registry=registry, engine_factory=_engine_factory(),
                                 contract_revision=0)

    assert factory.context_for("enrollment").readable_interfaces == ()


def test_memory_mismatch_rejected(tmp_path: Path) -> None:
    activity = next(m for m in _modules().modules if m.module_id == "activity")
    store = _memory(tmp_path, "enrollment", "x")
    view = store.read("proj", "enrollment", spec_hash=SPEC_HASH, contract_revision=0)

    with pytest.raises(AppException):
        build_module_context(activity, memory_view=view, interface_registry=InterfaceRegistry(
            project_id="proj", based_on_spec_hash=SPEC_HASH))


# --- 执行：无缺口零调用 / 有缺口按交集受控 ------------------------------------

def test_no_gap_makes_zero_coding_calls(tmp_path: Path) -> None:
    result = _factory(tmp_path).executor_for("enrollment").run(
        task_allowed_paths=["backend/", "frontend/"])

    assert result.invoked is False and result.coding_calls == 0 and result.change_set is None


def test_gap_uses_intersected_allowed_paths(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    executor = factory.executor_for("enrollment", gap={"acceptance_id": "AC01"})
    module_paths = executor.context.owned_paths
    target = next(p for p in module_paths if p.startswith("backend/"))
    _TARGETS["module:enrollment"] = [target]

    result = executor.run(task_allowed_paths=["backend/", "frontend/"])  # 宽范围

    assert result.invoked is True and result.coding_calls == 1
    assert set(result.allowed_paths) <= set(module_paths)  # 交集：仅本模块文件
    assert result.change_set["changes"][0]["path"] == target


def test_out_of_scope_write_rejected(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    executor = factory.executor_for("enrollment", gap={"acceptance_id": "AC01"})
    _TARGETS["module:enrollment"] = ["backend/src/main/java/com/agentforge/template/secret/Leak.java"]

    with pytest.raises(AppException) as error:
        executor.run(task_allowed_paths=["backend/"])
    assert error.value.code == "FORBIDDEN"


def test_empty_intersection_blocked(tmp_path: Path) -> None:
    executor = _factory(tmp_path).executor_for("enrollment", gap={"acceptance_id": "AC01"})
    with pytest.raises(AppException):
        executor.run(task_allowed_paths=["docs/"])  # 与本模块无交集


def test_unknown_module_rejected(tmp_path: Path) -> None:
    with pytest.raises(AppException):
        _factory(tmp_path).context_for("ghost")


# --- 重试不重复提交 -----------------------------------------------------------

def test_repeat_run_is_deterministic_and_not_auto_committed(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    executor = factory.executor_for("enrollment", gap={"acceptance_id": "AC01"})
    module_paths = executor.context.owned_paths
    target = next(p for p in module_paths if p.startswith("backend/"))
    _TARGETS["module:enrollment"] = [target]

    first = executor.run(task_allowed_paths=["backend/"])
    second = executor.run(task_allowed_paths=["backend/"])

    assert first.change_set == second.change_set  # 重试结果一致
    # run 不落盘（未提交）：模块 Agent 层不产生副作用，交由 Runner 验收后提交
    assert list((tmp_path / "mem").rglob("*.java")) == []
