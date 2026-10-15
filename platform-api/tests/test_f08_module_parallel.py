"""F08：独立模块并行调度（可选、默认禁用、需人工批准）。

覆盖（**确定性**）：

- **双重开关**：开关 + 人工批准缺一不可；否则回退 F07 串行模式（父步不改）；
- 分波：依赖不同波；**migration 命名空间 / DTO 命名冲突 → 自动串行**；无冲突独立模块同波并行；
- **每模块暂存隔离** + **合并冲突检测**（同路径异内容 → 冲突）；
- **并发预算不双扣**；**任务幂等**（已完成的模块不重复执行）；
- 负例：并发提交、取消/崩溃（单模块异常不拖垮同波）、预算耗尽。
"""

from __future__ import annotations

import threading
from pathlib import Path

from agentforge_platform.module_parallel import (
    APPROVAL_ENV,
    FLAG_ENV,
    BudgetPool,
    ModuleStaging,
    ParallelPlan,
    parallel_enabled,
    plan_waves,
    run_wave,
)
from agentforge_platform.module_registry import Module, ModuleRegistry, derive_modules
from agentforge_platform.spec import SoftwareSpecification

REPO = Path(__file__).resolve().parents[2]
CAMPUS = REPO / "schemas" / "campus-activity.spec.json"
_HASH = "a" * 64


def _module(module_id: str, paths: list[str], *, deps: list[str] | None = None) -> Module:
    return Module(project_id="p", module_id=module_id, name=module_id.capitalize(),
                  based_on_spec_hash=_HASH, profile_ref="demo-ts-mp@0.4",
                  owned_paths=paths, depends_on_modules=deps or [])


def _registry(*modules: Module) -> ModuleRegistry:
    return ModuleRegistry(project_id="p", based_on_spec_hash=_HASH,
                          profile_ref="demo-ts-mp@0.4", modules=list(modules))


# --- 双开关 -------------------------------------------------------------------

def test_disabled_by_default_and_requires_dual_approval(monkeypatch) -> None:
    monkeypatch.delenv(FLAG_ENV, raising=False)
    monkeypatch.delenv(APPROVAL_ENV, raising=False)
    assert parallel_enabled() is False

    monkeypatch.setenv(FLAG_ENV, "1")  # 仅开关
    assert parallel_enabled() is False  # 未人工批准 → 仍串行

    monkeypatch.setenv(APPROVAL_ENV, "1")  # 开关 + 批准
    assert parallel_enabled() is True


# --- 分波与冲突 ---------------------------------------------------------------

def test_dependency_places_modules_in_different_waves() -> None:
    spec = SoftwareSpecification.model_validate_json(CAMPUS.read_text(encoding="utf-8"))
    plan = plan_waves(derive_modules(spec, project_id="p"))

    assert plan.parallel is False  # enrollment 依赖 activity → 串行
    assert plan.serial_order() == ("activity", "enrollment")


def test_migration_namespace_forces_serialization() -> None:
    plan = plan_waves(_registry(
        _module("a", ["backend/src/main/resources/db/migration/V2__create_a.sql"]),
        _module("b", ["backend/src/main/resources/db/migration/V3__create_b.sql"]),
    ))

    assert plan.parallel is False and len(plan.waves) == 2  # 迁移命名空间互斥 → 串行
    assert any("共享资源冲突" in r for r in plan.serialized_reasons)


def test_dto_name_collision_serializes() -> None:
    shared = "backend/src/main/java/com/agentforge/template/dto/SharedCreateRequest.java"
    plan = plan_waves(_registry(_module("a", [shared]), _module("b", [shared])))

    assert plan.parallel is False  # 同名 DTO → 串行


def test_independent_modules_share_wave() -> None:
    plan = plan_waves(_registry(
        _module("a", ["backend/src/main/java/com/agentforge/template/entity/A.java"]),
        _module("b", ["backend/src/main/java/com/agentforge/template/entity/B.java"]),
    ))

    assert plan.parallel is True and plan.waves[0] == ("a", "b")  # 真独立 → 同波


# --- 暂存隔离与合并冲突 -------------------------------------------------------

def test_per_module_staging_and_merge_conflict(tmp_path: Path) -> None:
    staging = ModuleStaging(tmp_path / "stage")
    staging.stage("a", {"backend/x/Shared.java": b"class A {}"})
    staging.stage("b", {"backend/x/Shared.java": b"class B {}"})

    assert (tmp_path / "stage" / "a" / "backend/x/Shared.java").is_file()  # 隔离不互相覆盖
    assert any("合并冲突" in i for i in staging.merge_issues())


def test_identical_content_is_not_a_merge_conflict(tmp_path: Path) -> None:
    staging = ModuleStaging(tmp_path / "stage")
    staging.stage("a", {"backend/x/Shared.java": b"same"})
    staging.stage("b", {"backend/x/Shared.java": b"same"})
    assert staging.merge_issues() == []


# --- 并发预算与幂等 -----------------------------------------------------------

def test_budget_no_double_spend_under_concurrency() -> None:
    pool = BudgetPool(total_micro=100)
    granted: list[bool] = []
    lock = threading.Lock()

    def _reserve() -> None:
        ok = pool.reserve(30)
        with lock:
            granted.append(ok)

    threads = [threading.Thread(target=_reserve) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(granted) == 3  # 100 // 30 → 仅 3 次成功，其余阻塞
    assert pool.state["reserved"] == 90 and pool.state["available"] == 10


def test_run_wave_idempotent_budget_blocked_and_failure_isolated() -> None:
    calls: list[str] = []

    def runner(module_id: str) -> dict:
        calls.append(module_id)
        if module_id == "boom":
            raise RuntimeError("crash")
        return {"cost_micro": 20}

    pool = BudgetPool(total_micro=30)
    results = run_wave(
        ["a", "boom", "b"], runner, budget=pool, reserve_micro=20,
        done={"a"},  # a 已完成 → 幂等跳过
    )

    assert results["a"]["status"] == "SKIPPED_IDEMPOTENT"
    assert results["boom"]["status"] == "FAILED"  # 失败隔离，不拖垮同波
    assert results["b"]["status"] in {"DONE", "BUDGET_BLOCKED"}  # 预算不足则阻塞
    assert sum(1 for v in results.values() if v["status"] == "DONE") <= 1  # 不双扣（30//20=1）


def test_plan_serial_order_is_stable_for_fallback() -> None:
    plan = ParallelPlan(waves=(("a",), ("b", "c")))
    assert plan.serial_order() == ("a", "b", "c")  # 关闭开关时按此串行（F07 模式）
