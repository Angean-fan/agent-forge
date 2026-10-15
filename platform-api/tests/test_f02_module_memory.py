"""F02：Module Memory Store —— 模块长期记忆、版本隔离与可信更新。

覆盖（**确定性、零模型调用**）：

- 受限恢复：同一模块两次读取恢复**经核验**的决策；
- 可信更新：未验证/未提交、含秘密、超限一律拒绝；模型不得直接覆盖；
- 隔离：模块 A 的私有记忆对模块 B 不可见；
- 失效：冻结 spec 或接口版本变化 → 冲突记忆 `REBUILD_REQUIRED`，不生成隐式规则；
- 幂等与回退：同键同值不双写；按 revision 回滚不悬空；
- 不破坏旧路径：本步不接入生成图。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentforge_platform.errors import AppException
from agentforge_platform.module_memory import (
    READ_CURRENT,
    READ_REBUILD,
    ModuleMemoryStore,
)

SPEC_A = "a" * 64
SPEC_B = "b" * 64
REPO = Path(__file__).resolve().parents[2]


def _store(tmp_path: Path) -> ModuleMemoryStore:
    return ModuleMemoryStore(tmp_path / "memory")


def _record(store: ModuleMemoryStore, *, module="task", key="svc", value="TaskService 负责状态守卫",
            spec_hash=SPEC_A, contract_revision=1, verified=True, committed=True, commit_hash=None):
    return store.record(
        "proj", module, key=key, value=value, spec_hash=spec_hash,
        contract_revision=contract_revision, verified=verified, committed=committed,
        task_id="task-1", commit_hash=commit_hash, verification_ref="runner:verify#1",
    )


# --- 受限恢复 ------------------------------------------------------------------

def test_verified_decision_is_recorded_and_recovered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record(store)

    first = store.read("proj", "task", spec_hash=SPEC_A, contract_revision=1)
    second = store.read("proj", "task", spec_hash=SPEC_A, contract_revision=1)

    assert first.status == READ_CURRENT and first.entries
    assert first.entries[0].key == "svc" and first.entries[0].verification_ref
    assert first.entries == second.entries  # 两次调用恢复一致
    assert first.memory_revision == 1


def test_missing_memory_reads_empty(tmp_path: Path) -> None:
    view = _store(tmp_path).read("proj", "ghost", spec_hash=SPEC_A)
    assert view.entries == () and view.status == READ_CURRENT and view.memory_revision == 0


# --- 可信更新门禁 --------------------------------------------------------------

def test_unverified_or_uncommitted_write_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for kwargs in ({"verified": False}, {"committed": False}):
        with pytest.raises(AppException) as error:
            _record(store, **kwargs)
        assert error.value.code == "MODULE_MEMORY_REJECTED"
    assert store.load("proj", "task") is None  # 未落盘


def test_secret_value_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(AppException):
        _record(store, value="-----BEGIN RSA PRIVATE KEY-----\nMIIxxx\n")
    assert store.load("proj", "task") is None


def test_oversize_value_and_entry_limit_rejected(tmp_path: Path) -> None:
    store = ModuleMemoryStore(tmp_path / "m", max_value_chars=16, max_entries=1)
    with pytest.raises(AppException):
        _record(store, value="x" * 17)

    _record(store, key="k1", value="ok")
    with pytest.raises(AppException):
        _record(store, key="k2", value="ok")  # 超条目上限


# --- 隔离 ---------------------------------------------------------------------

def test_module_isolation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record(store, module="activity", key="a", value="活动模块私有事实")
    _record(store, module="enrollment", key="e", value="报名模块私有事实")

    activity = store.read("proj", "activity", spec_hash=SPEC_A)
    enrollment = store.read("proj", "enrollment", spec_hash=SPEC_A)

    assert [e.key for e in activity.entries] == ["a"]
    assert [e.key for e in enrollment.entries] == ["e"]  # 互不可见
    assert store.path_for("proj", "activity") != store.path_for("proj", "enrollment")


def test_unsafe_ids_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(AppException):
        store.path_for("proj", "../escape")


# --- 失效 ---------------------------------------------------------------------

def test_spec_change_invalidates_conflicting_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record(store, spec_hash=SPEC_A, contract_revision=1)

    view = store.read("proj", "task", spec_hash=SPEC_B, contract_revision=1)

    assert view.status == READ_REBUILD and view.entries == ()
    assert view.notice and "重建" in view.notice
    assert store.load("proj", "task") is not None  # 旧记忆未被篡改，仅"失效"


def test_contract_revision_change_invalidates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record(store, spec_hash=SPEC_A, contract_revision=1)

    view = store.read("proj", "task", spec_hash=SPEC_A, contract_revision=2)

    assert view.status == READ_REBUILD and view.entries == ()


# --- 幂等与回退 ---------------------------------------------------------------

def test_idempotent_record_no_double_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record(store)
    after_first = store.load("proj", "task")

    _record(store)  # 同键同值同证据
    after_second = store.load("proj", "task")

    assert after_second.memory_revision == after_first.memory_revision == 1
    assert len(after_second.entries) == 1  # 未双写


def test_rollback_drops_later_entries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record(store, key="k1", value="v1")  # revision 1
    _record(store, key="k2", value="v2")  # revision 2

    rolled = store.rollback("proj", "task", to_revision=1)

    assert rolled.memory_revision == 1
    assert [e.key for e in rolled.entries] == ["k1"]  # 丢弃悬挂的"已完成"
    assert store.load("proj", "task").memory_revision == 1  # 已持久化


def test_rollback_invalid_target_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record(store)
    with pytest.raises(AppException):
        store.rollback("proj", "task", to_revision=5)
    with pytest.raises(AppException):
        store.rollback("proj", "ghost", to_revision=0)


# --- 持久化与旧路径不受影响 ----------------------------------------------------

def test_persist_and_replay_is_deterministic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record(store)

    replayed = _store(tmp_path).load("proj", "task")
    assert replayed.memory_revision == 1 and replayed.entries[0].key == "svc"
    assert not list((tmp_path / "memory" / "proj").glob("*.tmp"))  # 无半写残留


def test_not_wired_into_generation_graph() -> None:
    """旧 Task 不启用模块记忆仍可原样运行：生成图不引用本模块。"""
    graph = (REPO / "platform-api" / "src" / "agentforge_platform" / "generation_graph.py").read_text(
        encoding="utf-8")
    assert "module_memory" not in graph
