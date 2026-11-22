"""F01：模块边界识别、Module Registry 与唯一写入所有权。

在冻结 spec、R13 TaskPlan/DAG 与 R15 ``allowed_paths`` 之上，引入**可验证的业务模块责任图**：

- 每个业务模块拥有稳定 ``module_id``、职责范围、**独占写入路径**、依赖关系与生命周期；
- **规则优先**：模块候选由已冻结实体（及 ``relations`` 依赖）确定性派生，**不改变 spec 语义**，
  也不调用任何模型（模块 Agents 关闭时走旧任务调度）；
- **共享文件由模板/集成层拥有**：跨实体同路径的文件（如 ``PageResult``、全局 config/exception）
  归**唯一** ``shared_owner``，**不得**隐式分给多个模块；
- **唯一写入权**：写入生效权限为 ``task.allowed_paths ∩ module.owned_paths``；空交集、越界、
  两模块声明同一文件/对方目录、依赖环、未知模块一律**阻塞**；
- Registry 可**持久化/重放**（``modules.json``，独立 schema 版本），不触碰老 ``RunTask`` 与 ``asset-lock``。
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import content_hash
from agentforge_platform.task_plan import is_safe_task_path

MODULE_REGISTRY_VERSION = "0.1"
REGISTRY_FILE = "modules.json"

SHARED_OWNER = "template"
OWNER_MODULE = "module_owner"
OWNER_TEMPLATE = "template_owner"
EXECUTION_SERIAL = "SERIAL"  # 初期仅串行；并行（F08）后置
LIFECYCLE_READY = "READY"
_ALLOWED_LIFECYCLES = frozenset({"READY", "RUNNING", "DONE", "BLOCKED"})

_STRICT = ConfigDict(extra="forbid")

# 跨实体同路径的共享文件（模板/集成层唯一负责）：不归属任何业务模块。
SHARED_FILES: tuple[str, ...] = (
    "backend/src/main/java/com/agentforge/template/vo/PageResult.java",
    "backend/src/main/java/com/agentforge/template/config/MybatisPlusConfig.java",
    "backend/src/main/java/com/agentforge/template/exception/ConstraintViolationAdvice.java",
)

_JAVA_ROOT = "backend/src/main/java/com/agentforge/template"
_TEST_ROOT = "backend/src/test/java/com/agentforge/template"
_FE_ROOT = "frontend/src"


def _snake(name: str) -> str:
    step1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step1).lower()


class Module(BaseModel):
    """一个业务模块的责任与唯一写入范围。"""

    model_config = _STRICT

    project_id: str
    module_id: str
    name: str
    based_on_spec_hash: str
    profile_ref: str
    owner_role: str = OWNER_MODULE
    owned_paths: list[str]
    readable_contract_refs: list[str] = []
    depends_on_modules: list[str] = []
    memory_revision: int = 0
    contract_revision: int = 0
    execution_mode: str = EXECUTION_SERIAL
    lifecycle: str = LIFECYCLE_READY
    # A06：工厂并行模式可选扩展字段（保留向后兼容默认值，不破坏 F01—F08 旧单测）
    migration_range: tuple[int, int] | None = None
    test_paths: list[str] = []
    owned_package: str | None = None


class ModuleRegistry(BaseModel):
    model_config = _STRICT

    schema_version: str = MODULE_REGISTRY_VERSION
    project_id: str
    based_on_spec_hash: str
    profile_ref: str
    modules: list[Module]
    shared_owner: str = SHARED_OWNER
    shared_paths: list[str] = []


def _module_paths(entity: str, *, migration_index: int) -> list[str]:
    """按生成器命名约定，派生某实体模块**独占**的具体文件路径（规则式，无通配符）。"""
    snake = _snake(entity)
    return [
        f"{_JAVA_ROOT}/entity/{entity}.java",
        f"{_JAVA_ROOT}/mapper/{entity}Mapper.java",
        f"{_JAVA_ROOT}/service/{entity}Service.java",
        f"{_JAVA_ROOT}/controller/{entity}Controller.java",
        f"{_JAVA_ROOT}/dto/{entity}CreateRequest.java",
        f"{_JAVA_ROOT}/dto/{entity}UpdateRequest.java",
        f"{_JAVA_ROOT}/vo/{entity}View.java",
        f"backend/src/main/resources/db/migration/V{migration_index}__create_{snake}.sql",
        f"{_TEST_ROOT}/{entity}ApiTest.java",
        f"{_TEST_ROOT}/{entity}ConstraintTest.java",
        f"{_FE_ROOT}/types/{snake}.ts",
        f"{_FE_ROOT}/api/{snake}.ts",
        f"{_FE_ROOT}/views/{entity}ListView.vue",
        f"{_FE_ROOT}/router/modules/{snake}.ts",
        f"frontend/test/{snake}-api.spec.ts",
    ]


def derive_modules(spec: SoftwareSpecification, *, project_id: str) -> ModuleRegistry:
    """从冻结 spec **确定性**派生模块登记 + 无环依赖图（规则优先，不调用模型）。"""
    profile_ref = f"{spec.technical_profile.profile_id}@{spec.technical_profile.profile_version}"
    spec_hash = content_hash(spec)
    by_name = {entity.name: entity for entity in spec.entities}
    module_ids = {entity.name: _snake(entity.name) for entity in spec.entities}

    # relations：child 依赖 parent（多对一 / 唯一约束）→ child 模块依赖 parent 模块。
    deps: dict[str, set[str]] = {name: set() for name in by_name}
    for relation in spec.relations or []:
        child = str(relation.get("child") or "")
        parent = str(relation.get("parent") or "")
        if child in by_name and parent in by_name and child != parent:
            deps[child].add(parent)
    # 去环：拓扑剥离（确定性顺序）；仍在环中的边一律丢弃并记录为 issue，由校验阻塞。
    ordered = _topological(by_name.keys(), deps)
    modules: list[Module] = []
    for index, entity in enumerate(spec.entities):
        name = entity.name
        reachable_deps = sorted(
            module_ids[dep] for dep in deps.get(name, set()) if dep in ordered
        )
        modules.append(
            Module(
                project_id=project_id,
                module_id=module_ids[name],
                name=name,
                based_on_spec_hash=spec_hash,
                profile_ref=profile_ref,
                owned_paths=_module_paths(name, migration_index=index + 2),
                depends_on_modules=reachable_deps,
            )
        )
    return ModuleRegistry(
        project_id=project_id, based_on_spec_hash=spec_hash, profile_ref=profile_ref,
        modules=modules, shared_paths=list(SHARED_FILES),
    )


def _topological(names, deps: dict[str, set[str]]):
    remaining = set(names)
    ordered: list[str] = []
    while remaining:
        ready = sorted(n for n in remaining if not (deps.get(n, set()) & remaining))
        if not ready:
            break  # 存在环：剩余节点不进入拓扑序（由校验阻塞）
        ordered.extend(ready)
        remaining -= set(ready)
    return ordered


def _overlaps(left: str, right: str) -> bool:
    a, b = left.rstrip("/"), right.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _within(path: str, prefix: str) -> bool:
    a, b = path.rstrip("/"), prefix.rstrip("/")
    return a == b or a.startswith(b + "/")


def registry_issues(registry: ModuleRegistry) -> list[str]:
    """唯一写入权与其完整性校验：id 唯一、路径安全、无重叠、不染指共享文件、依赖已知且无环。"""
    issues: list[str] = []
    ids = [module.module_id for module in registry.modules]
    if len(set(ids)) != len(ids):
        issues.append("module_id 重复")
    if registry.shared_owner in ids:
        issues.append(f"业务模块不得占用共享 owner id：{registry.shared_owner}")
    id_set = set(ids)

    for module in registry.modules:
        if module.lifecycle not in _ALLOWED_LIFECYCLES:
            issues.append(f"{module.module_id} 未知生命周期：{module.lifecycle}")
        if module.execution_mode != EXECUTION_SERIAL:
            issues.append(f"{module.module_id} 未知执行模式：{module.execution_mode}")
        for path in module.owned_paths:
            if not is_safe_task_path(path):
                issues.append(f"{module.module_id} 越界 owned_paths：{path}")
            for shared in registry.shared_paths:
                if _overlaps(path, shared):
                    issues.append(f"{module.module_id} 不得声明共享文件：{path}")
        for path in module.test_paths:
            if not is_safe_task_path(path):
                issues.append(f"{module.module_id} 越界 test_paths：{path}")
            for shared in registry.shared_paths:
                if _overlaps(path, shared):
                    issues.append(f"{module.module_id} 不得在 test_paths 中声明共享文件：{path}")
        if module.migration_range is not None:
            start, end = module.migration_range
            if start < 1 or end < start:
                issues.append(
                    f"{module.module_id} 非法 SQL 号段 migration_range：({start}, {end})"
                )
        for dep in module.depends_on_modules:
            if dep not in id_set:
                issues.append(f"{module.module_id} 依赖未知模块：{dep}")
            if dep == module.module_id:
                issues.append(f"{module.module_id} 依赖自身")

    for index, left in enumerate(registry.modules):
        for right in registry.modules[index + 1:]:
            left_all = [*left.owned_paths, *left.test_paths]
            right_all = [*right.owned_paths, *right.test_paths]
            for left_path in left_all:
                for right_path in right_all:
                    if _overlaps(left_path, right_path):
                        issues.append(
                            f"唯一写入权冲突：{left.module_id} 的 {left_path} 与 "
                            f"{right.module_id} 的 {right_path}"
                        )
            if left.migration_range is not None and right.migration_range is not None:
                l_start, l_end = left.migration_range
                r_start, r_end = right.migration_range
                if max(l_start, r_start) <= min(l_end, r_end):
                    issues.append(
                        f"SQL 号段重叠：{left.module_id} ({l_start}–{l_end}) 与 "
                        f"{right.module_id} ({r_start}–{r_end})"
                    )
            if left.owned_package and right.owned_package and left.owned_package == right.owned_package:
                issues.append(
                    f"包路径冲突：{left.module_id} 与 {right.module_id} 共用包 {left.owned_package}"
                )
    if _has_cycle(registry.modules):
        issues.append("模块依赖存在环")
    return issues


def _has_cycle(modules: list[Module]) -> bool:
    remaining = {module.module_id for module in modules}
    deps = {module.module_id: set(module.depends_on_modules) for module in modules}
    while remaining:
        ready = [mid for mid in remaining if not (deps[mid] & remaining)]
        if not ready:
            return True
        for mid in ready:
            remaining.discard(mid)
    return False


def owners_of(path: str, registry: ModuleRegistry) -> list[str]:
    """某路径被哪些业务模块声明（含前缀归属，含 test_paths）；共享文件返回 ``[]``（归 shared_owner）。"""
    return [
        module.module_id for module in registry.modules
        if any(
            _within(path, owned) or _within(owned, path)
            for owned in (*module.owned_paths, *module.test_paths)
        )
    ]


def task_scope_issues(task_allowed_paths: list[str], registry: ModuleRegistry) -> list[str]:
    """任务授权路径必须落在**某个**模块内（或共享区）：否则越界阻塞。"""
    issues: list[str] = []
    shared = registry.shared_paths
    for path in task_allowed_paths:
        if any(_within(path, owned) or _within(owned, path)
               for module in registry.modules for owned in module.owned_paths):
            continue
        if any(_overlaps(path, item) for item in shared):
            continue
        issues.append(f"任务路径不属于任何模块：{path}")
    return issues


def effective_write_scope(
    task_allowed_paths: list[str], module_owned_paths: list[str]
) -> tuple[list[str], list[str]]:
    """写入生效权限 = ``task.allowed_paths ∩ module.owned_paths``；空交集阻塞。"""
    effective = sorted(
        path for path in module_owned_paths
        if any(_within(path, allowed) for allowed in task_allowed_paths)
    )
    issues: list[str] = []
    if not effective:
        issues.append("task.allowed_paths 与 module.owned_paths 交集为空：拒绝写入")
    return effective, issues


def write_issues(registry: ModuleRegistry, module_id: str, paths: list[str]) -> list[str]:
    """某模块拟写入的路径是否越权：不得写他模块目录、共享文件，也不得写未授权路径。"""
    issues: list[str] = []
    module = next((m for m in registry.modules if m.module_id == module_id), None)
    if module is None:
        return [f"未知模块：{module_id}"]
    for path in paths:
        if any(_overlaps(path, shared) for shared in registry.shared_paths):
            issues.append(f"{module_id} 不得写共享文件：{path}")
            continue
        module_writable = (*module.owned_paths, *module.test_paths)
        if not any(_within(path, owned) or _within(owned, path) for owned in module_writable):
            issues.append(f"{module_id} 越权写入（不在其 owned_paths 内）：{path}")
            continue
        others = [mid for mid in owners_of(path, registry) if mid != module_id]
        if others:
            issues.append(f"{module_id} 与他模块冲突写入：{path}（{', '.join(others)}）")
    return issues


def save_registry(registry: ModuleRegistry, run_dir: str | Path) -> Path:
    path = Path(run_dir) / REGISTRY_FILE
    path.write_text(registry.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def load_registry(run_dir: str | Path) -> ModuleRegistry | None:
    """从 run 目录重放模块登记（独立 schema 版本；不触碰老 RunTask/asset-lock）。"""
    path = Path(run_dir) / REGISTRY_FILE
    if not path.is_file():
        return None
    return ModuleRegistry.model_validate_json(path.read_text(encoding="utf-8"))


__all__ = [
    "MODULE_REGISTRY_VERSION",
    "OWNER_MODULE",
    "OWNER_TEMPLATE",
    "REGISTRY_FILE",
    "SHARED_FILES",
    "SHARED_OWNER",
    "Module",
    "ModuleRegistry",
    "derive_modules",
    "effective_write_scope",
    "load_registry",
    "owners_of",
    "registry_issues",
    "save_registry",
    "task_scope_issues",
    "write_issues",
]
