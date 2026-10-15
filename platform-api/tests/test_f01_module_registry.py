"""F01：模块边界识别、Module Registry 与唯一写入所有权。

覆盖（**确定性、零模型调用**）：

- 单实体 → 稳定模块登记；跨实体共享文件归**模板层唯一 owner**，不归任何业务模块；
- 双实体（campus：Activity/Enrollment + 关系）→ 无环依赖图（Enrollment 依赖 Activity）、逐实体独占路径；
- 唯一写入权：两模块声明同一文件/对方目录被拒；非法/越界路径、未知依赖、依赖环被拒；
- 写入生效权限 = ``task.allowed_paths ∩ module.owned_paths``，空交集/越界阻塞；
- Registry 持久化/重放（`modules.json`）确定一致；开关关闭时走旧任务调度（本步不接生成图）。
"""

from __future__ import annotations

import json
from pathlib import Path

from agentforge_platform.module_registry import (
    SHARED_OWNER,
    Module,
    ModuleRegistry,
    derive_modules,
    effective_write_scope,
    load_registry,
    owners_of,
    registry_issues,
    save_registry,
    task_scope_issues,
    write_issues,
)
from agentforge_platform.spec import SoftwareSpecification

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
CAMPUS = REPO / "schemas" / "campus-activity.spec.json"


def _spec(path: Path) -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(path.read_text(encoding="utf-8"))


def _module(registry: ModuleRegistry, module_id: str) -> Module:
    return next(m for m in registry.modules if m.module_id == module_id)


# --- 派生与唯一所有权 ----------------------------------------------------------

def test_single_entity_registry_is_clean() -> None:
    registry = derive_modules(_spec(TASK), project_id="p1")

    assert [m.module_id for m in registry.modules] == ["task"]
    assert registry_issues(registry) == []
    # 共享文件由模板层唯一 owner 持有，不属于业务模块
    assert registry.shared_owner == SHARED_OWNER
    assert registry.shared_paths
    for shared in registry.shared_paths:
        assert owners_of(shared, registry) == []


def test_two_modules_unique_ownership_and_acyclic() -> None:
    registry = derive_modules(_spec(CAMPUS), project_id="p2")

    assert sorted(m.module_id for m in registry.modules) == ["activity", "enrollment"]
    assert registry_issues(registry) == []  # 路径不重叠 + 无环
    enrollment = _module(registry, "enrollment")
    assert enrollment.depends_on_modules == ["activity"]  # child → parent
    # 逐实体路径互不相同、互不重叠
    a_paths, e_paths = set(_module(registry, "activity").owned_paths), set(enrollment.owned_paths)
    assert a_paths and e_paths and not (a_paths & e_paths)


def test_shared_file_cannot_be_claimed_by_business_module() -> None:
    registry = derive_modules(_spec(TASK), project_id="p")
    spoiled = registry.model_copy(update={"modules": [
        registry.modules[0].model_copy(update={
            "owned_paths": [*registry.modules[0].owned_paths, registry.shared_paths[0]]})
    ]})

    assert any("共享文件" in issue for issue in registry_issues(spoiled))


# --- 冲突 / 非法 / 环 ----------------------------------------------------------

def test_two_modules_claiming_same_file_rejected() -> None:
    registry = derive_modules(_spec(CAMPUS), project_id="p")
    activity, enrollment = _module(registry, "activity"), _module(registry, "enrollment")
    clashing = registry.model_copy(update={"modules": [
        enrollment.model_copy(update={"owned_paths": [
            *enrollment.owned_paths, activity.owned_paths[0]]}),
        activity,
    ]})

    assert any("唯一写入权冲突" in issue for issue in registry_issues(clashing))


def test_unsafe_path_and_unknown_dependency_rejected() -> None:
    registry = derive_modules(_spec(TASK), project_id="p")
    broken = registry.model_copy(update={"modules": [
        registry.modules[0].model_copy(update={
            "owned_paths": ["../escape/File.java"], "depends_on_modules": ["ghost"]})
    ]})
    issues = registry_issues(broken)

    assert any("越界 owned_paths" in issue for issue in issues)
    assert any("依赖未知模块" in issue for issue in issues)


def test_dependency_cycle_rejected() -> None:
    activity = Module(project_id="p", module_id="a", name="A", based_on_spec_hash="h",
                      profile_ref="demo-ts-mp@0.4", owned_paths=["backend/a/A.java"],
                      depends_on_modules=["b"])
    b = Module(project_id="p", module_id="b", name="B", based_on_spec_hash="h",
               profile_ref="demo-ts-mp@0.4", owned_paths=["backend/b/B.java"],
               depends_on_modules=["a"])
    registry = ModuleRegistry(project_id="p", based_on_spec_hash="h",
                              profile_ref="demo-ts-mp@0.4", modules=[activity, b])

    assert any("环" in issue for issue in registry_issues(registry))


# --- 写入生效权限 --------------------------------------------------------------

def test_effective_write_scope_intersects_and_blocks_empty() -> None:
    module_paths = ["backend/src/main/java/com/agentforge/template/service/TaskService.java",
                    "frontend/src/api/task.ts"]

    in_scope, issues = effective_write_scope(
        ["backend/src/main/java/com/agentforge/template/service/"], module_paths)
    assert in_scope == [module_paths[0]] and issues == []

    empty, blocked = effective_write_scope(["backend/src/main/java/com/agentforge/other/"], module_paths)
    assert empty == [] and blocked  # 空交集 → 阻塞


def test_write_outside_module_or_shared_is_rejected() -> None:
    registry = derive_modules(_spec(CAMPUS), project_id="p")
    enrollment = _module(registry, "enrollment")
    activity_file = _module(registry, "activity").owned_paths[0]

    assert write_issues(registry, "enrollment", [activity_file])  # 写对方目录被拒
    assert write_issues(registry, "enrollment", [registry.shared_paths[0]])  # 写共享文件被拒
    assert write_issues(registry, "enrollment", enrollment.owned_paths) == []  # 写自己目录通过
    assert write_issues(registry, "ghost", ["x"]) == ["未知模块：ghost"]


def test_task_scope_out_of_bounds_blocked() -> None:
    registry = derive_modules(_spec(CAMPUS), project_id="p")

    assert task_scope_issues(["frontend/src/api/"], registry) == []
    assert task_scope_issues(["backend/src/main/java/com/agentforge/unknown/"], registry)


# --- 持久化 / 重放 ------------------------------------------------------------

def test_registry_persists_and_replays_deterministically(tmp_path: Path) -> None:
    registry = derive_modules(_spec(CAMPUS), project_id="p")
    save_registry(registry, tmp_path)
    (tmp_path / "modules.json").read_text(encoding="utf-8")

    replayed = load_registry(tmp_path)
    assert replayed == registry  # 重放一致
    assert replayed.schema_version == "0.1"
    assert derive_modules(_spec(CAMPUS), project_id="p") == registry  # 派生确定
    assert load_registry(tmp_path / "missing") is None


def test_registry_file_is_independent_of_other_artifacts() -> None:
    """独立 schema 版本；不依赖 RunTask/asset-lock（仅读取自身文件）。"""
    registry = derive_modules(_spec(TASK), project_id="p")
    payload = json.loads(registry.model_dump_json())
    assert payload["schema_version"] == "0.1"
    assert payload["shared_owner"] == SHARED_OWNER
    assert {m["owner_role"] for m in payload["modules"]} == {"module_owner"}
