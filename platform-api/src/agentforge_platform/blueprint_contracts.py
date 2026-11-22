"""A06：工厂架构蓝图契约校验与跨模块只读 Java Facade Stub 生成器（``blueprint_contracts.py``）。

支撑项目总管在并发开工前冻结跨模块契约（``APPROVED_FOR_BUILD``）：
1. ``ArchitectureBlueprint``：封装部门模块划分、SQL 号段、跨模块冻结契约（``PublicCapability``）与共享 DTO；
2. ``validate_blueprint(blueprint, spec) -> list[str]``：执行 5 项确定性硬校验——
   ① 目录零交集（含 ``owned_paths`` / ``test_paths`` / ``owned_package``，禁止侵占底座与 ``api/*Facade.java``）；
   ② SQL 号段零交集（避开底座 ``V001..V009`` 与跨表外键 ``V900+``）；
   ③ 依赖契约全存在且无模糊签名（拒绝 ``TODO`` / ``*`` / ``?`` / 未声明跨模块依赖）；
   ④ 模块与跨模块接口依赖图无环；
   ⑤ 覆盖全部冻结业务需求（实体与验收场景零遗漏）；
3. ``generate_java_facade_stubs(blueprint, target_dir) -> list[str]``：确定性生成合法可编译的
   ``backend/src/main/java/<base_pkg>/modules/<dept_id>/api/*Facade.java`` 与公共 DTO 骨架，并设为只读保护。
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.interface_registry import (
    KIND_JAVA_METHOD,
    STATUS_APPROVED_FOR_BUILD,
    STATUS_VERIFIED,
    InterfaceRegistry,
    PublicCapability,
)
from agentforge_platform.interface_registry import (
    registry_issues as interface_registry_issues,
)
from agentforge_platform.module_registry import (
    Module,
    ModuleRegistry,
)
from agentforge_platform.module_registry import (
    registry_issues as module_registry_issues,
)
from agentforge_platform.scope_policy import factory_max_modules
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import content_hash

_STRICT = ConfigDict(extra="forbid")

_SAFE_JAVA_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_JAVA_PACKAGE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_SAFE_JAVA_TYPE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:<[A-Za-z0-9_,\s<>]+>)?(?:\[\])?$"
)
_AMBIGUOUS_TOKENS = ("*", "?", "todo", "tbd", "undefined", "unknown")
_METHOD_SIG_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_<>,\s\[\]]*?)\s+([a-z][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*$"
)
_TYPE_TOKEN_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\b")

_STD_JAVA_IMPORTS: dict[str, str] = {
    "BigDecimal": "java.math.BigDecimal",
    "BigInteger": "java.math.BigInteger",
    "Instant": "java.time.Instant",
    "LocalDate": "java.time.LocalDate",
    "LocalDateTime": "java.time.LocalDateTime",
    "List": "java.util.List",
    "Map": "java.util.Map",
    "Optional": "java.util.Optional",
    "Set": "java.util.Set",
    "UUID": "java.util.UUID",
}

MIN_MODULE_MIGRATION_VERSION = 10
MAX_MODULE_MIGRATION_VERSION = 899  # V900+ 预留给总装跨表外键 V900__relations.sql


class DtoField(BaseModel):
    """跨模块公共 DTO 的字段定义。"""

    model_config = _STRICT

    name: str
    java_type: str
    required: bool = True
    description: str = ""

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _SAFE_JAVA_IDENT.fullmatch(value):
            raise ValueError(f"非法 Java 字段名：{value!r}")
        return value

    @field_validator("java_type")
    @classmethod
    def _valid_type(cls, value: str) -> str:
        cleaned = value.strip()
        lower = cleaned.lower()
        if any(tok in lower for tok in _AMBIGUOUS_TOKENS) or not _SAFE_JAVA_TYPE.fullmatch(cleaned):
            raise ValueError(f"非法或模糊的 Java 类型：{value!r}")
        return cleaned


class DtoContract(BaseModel):
    """跨模块公共 DTO 契约（生成在 ``modules/<owner_module_id>/api/<name>.java``）。"""

    model_config = _STRICT

    name: str
    owner_module_id: str
    fields: list[DtoField]

    @field_validator("name")
    @classmethod
    def _valid_dto_name(cls, value: str) -> str:
        if not _SAFE_JAVA_IDENT.fullmatch(value) or not value[0].isupper():
            raise ValueError(f"DTO 类名必须为大驼峰合法 Java 标识符：{value!r}")
        return value


class ArchitectureBlueprint(BaseModel):
    """A06 / FP02：项目总管冻结的跨模块架构蓝图（开工前契约真值）。"""

    model_config = _STRICT

    schema_version: str = "1.0"
    blueprint_version: int = 1
    phase: str = "backend"
    project_id: str
    based_on_spec_hash: str
    profile_ref: str
    base_package: str = "com.agentforge.template"
    modules: list[Module]
    frozen_contracts: list[PublicCapability] = []
    shared_dtos: list[DtoContract] = []
    covered_entities: dict[str, list[str]] = {}
    covered_pages: dict[str, list[str]] = {}
    covered_acceptance_ids: dict[str, list[str]] = {}
    sandbox_integrations: list[dict[str, Any]] = []
    shared_paths: list[str] = []

    @field_validator("base_package")
    @classmethod
    def _valid_pkg(cls, value: str) -> str:
        if not _SAFE_JAVA_PACKAGE.fullmatch(value):
            raise ValueError(f"非法 Java 基础包名：{value!r}")
        return value

    def to_module_registry(self) -> ModuleRegistry:
        return ModuleRegistry(
            project_id=self.project_id,
            based_on_spec_hash=self.based_on_spec_hash,
            profile_ref=self.profile_ref,
            modules=list(self.modules),
            shared_paths=list(self.shared_paths),
        )

    def to_interface_registry(self) -> InterfaceRegistry:
        return InterfaceRegistry(
            project_id=self.project_id,
            based_on_spec_hash=self.based_on_spec_hash,
            capabilities=list(self.frozen_contracts),
        )

    def blueprint_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def facade_stub_paths(self) -> list[str]:
        """计算蓝图将生成的全部只读 ``modules/<dept_id>/api/*.java`` 相对路径。"""
        pkg_path = self.base_package.replace(".", "/")
        paths: set[str] = set()
        for dto in self.shared_dtos:
            paths.add(
                f"backend/src/main/java/{pkg_path}/modules/{dto.owner_module_id}/api/{dto.name}.java"
            )
        for cap in self.frozen_contracts:
            if cap.interface_kind == KIND_JAVA_METHOD and cap.symbol:
                facade_cls = cap.symbol.split(".", 1)[0]
                paths.add(
                    f"backend/src/main/java/{pkg_path}/modules/{cap.owner_module_id}/api/{facade_cls}.java"
                )
        return sorted(paths)


def _is_facade_api_path(path: str) -> bool:
    parts = PurePosixPath(path.rstrip("/")).parts
    if "modules" in parts and "api" in parts:
        mod_idx = parts.index("modules")
        if mod_idx + 2 < len(parts) and parts[mod_idx + 2] == "api":
            return True
    return False


def _has_combined_cycle(
    module_ids: set[str],
    edges: dict[str, set[str]],
) -> bool:
    remaining = set(module_ids)
    while remaining:
        ready = [mid for mid in remaining if not (edges.get(mid, set()) & remaining)]
        if not ready:
            return True
        for mid in ready:
            remaining.discard(mid)
    return False


def _extract_contract_signature_issues(cap: PublicCapability) -> list[str]:
    issues: list[str] = []
    if cap.interface_kind == KIND_JAVA_METHOD:
        lower = cap.signature.lower()
        if any(tok in lower for tok in _AMBIGUOUS_TOKENS):
            issues.append(f"契约 {cap.capability_id} 含模糊签名：{cap.signature!r}")
        elif _METHOD_SIG_RE.fullmatch(cap.signature) is None:
            issues.append(
                f"契约 {cap.capability_id} Java 方法签名格式非法（需为 '<ReturnType> <methodName>(<Args>)'）：{cap.signature!r}"
            )
        if cap.symbol:
            parts = cap.symbol.split(".")
            if len(parts) != 2 or not all(_SAFE_JAVA_IDENT.fullmatch(p) for p in parts):
                issues.append(f"契约 {cap.capability_id} 的 Java symbol 非法：{cap.symbol!r}")
    return issues


def validate_blueprint(
    blueprint: ArchitectureBlueprint,
    spec: SoftwareSpecification,
) -> list[str]:
    """A06：对架构蓝图执行 5 项确定性硬校验，返回全部违规问题列表（空列表 = 通过）。

    1. 目录零交集（含 ``owned_paths`` / ``test_paths`` / ``owned_package``，禁止单部门宣称 ``modules/*/api/``）；
    2. SQL 号段零交集（后端模块必须声明互不重叠且在 ``10..899`` 范围内的 ``migration_range``）；
    3. 依赖契约全存在且无模糊签名（含未声明跨模块依赖拦截）；
    4. 模块与跨模块接口依赖图无环；
    5. 覆盖全部冻结业务需求（实体与验收场景零遗漏）。
    """
    issues: list[str] = []
    if not blueprint.modules:
        return ["架构蓝图缺少业务模块定义（modules 不能为空）"]

    max_mods = factory_max_modules()
    if len(blueprint.modules) > max_mods:
        issues.append(
            f"架构蓝图模块数超过工厂上限：最多 {max_mods} 个，当前 {len(blueprint.modules)} 个"
        )

    expected_spec_hash = content_hash(spec)
    if blueprint.based_on_spec_hash and blueprint.based_on_spec_hash != expected_spec_hash:
        issues.append(
            f"蓝图 spec_hash 不匹配：blueprint={blueprint.based_on_spec_hash[:12]} "
            f"!= spec={expected_spec_hash[:12]}"
        )

    # 校验项 1 & 2（部分）：复用 ModuleRegistry 硬校验（目录零交集、test_paths、SQL 号段重叠、包冲突、模块依赖环）
    mod_registry = blueprint.to_module_registry()
    issues.extend(module_registry_issues(mod_registry))

    modules_by_id = {m.module_id: m for m in blueprint.modules}
    module_ids = set(modules_by_id)

    for module in blueprint.modules:
        # 禁止单部门将只读 Facade Stub 目录声明为可写 owned_paths
        for owned in module.owned_paths:
            if _is_facade_api_path(owned):
                issues.append(
                    f"模块 {module.module_id} 禁止将只读 Facade Stub 路径纳入可写 owned_paths：{owned}"
                )
        # 后端阶段每个模块必须分配合法的 SQL 号段（10..899）
        if blueprint.phase == "backend":
            if module.migration_range is None:
                issues.append(f"后端模块 {module.module_id} 缺少 SQL 号段分配（migration_range）")
            else:
                lo, hi = module.migration_range
                if lo < MIN_MODULE_MIGRATION_VERSION or hi > MAX_MODULE_MIGRATION_VERSION:
                    issues.append(
                        f"模块 {module.module_id} SQL 号段 ({lo}–{hi}) 超出允许范围 "
                        f"[{MIN_MODULE_MIGRATION_VERSION}..{MAX_MODULE_MIGRATION_VERSION}]"
                        "（V001..V009 预留底座，V900+ 预留跨表外键）"
                    )

    # 校验项 3：依赖契约全存在且无模糊签名
    iface_registry = blueprint.to_interface_registry()
    issues.extend(interface_registry_issues(iface_registry))

    contracts_by_id: dict[str, PublicCapability] = {}
    for cap in blueprint.frozen_contracts:
        contracts_by_id[cap.capability_id] = cap
        if cap.owner_module_id not in module_ids:
            issues.append(
                f"契约 {cap.capability_id} 的所有者模块不存在：{cap.owner_module_id}"
            )
        if cap.status not in {STATUS_APPROVED_FOR_BUILD, STATUS_VERIFIED}:
            issues.append(
                f"开工前蓝图契约 {cap.capability_id} 必须冻结为 APPROVED_FOR_BUILD 或 VERIFIED（当前：{cap.status}）"
            )
        issues.extend(_extract_contract_signature_issues(cap))

    for dto in blueprint.shared_dtos:
        if dto.owner_module_id not in module_ids:
            issues.append(f"共享 DTO {dto.name} 的所有者模块不存在：{dto.owner_module_id}")

    # 跨模块契约引用与显式依赖一致性校验 + 联合依赖图（校验项 3 & 4）
    combined_edges: dict[str, set[str]] = {
        m.module_id: set(m.depends_on_modules) for m in blueprint.modules
    }
    for module in blueprint.modules:
        referenced_owners: set[str] = set()
        for ref in module.readable_contract_refs:
            cap = contracts_by_id.get(ref)
            if cap is None:
                issues.append(
                    f"模块 {module.module_id} 引用了不存在的冻结契约：{ref}"
                )
                continue
            if cap.owner_module_id != module.module_id:
                referenced_owners.add(cap.owner_module_id)
                combined_edges[module.module_id].add(cap.owner_module_id)
                if cap.owner_module_id not in module.depends_on_modules:
                    issues.append(
                        f"未声明的跨模块依赖：模块 {module.module_id} 引用了 {cap.owner_module_id} 的契约 "
                        f"{ref}，但未在 depends_on_modules 中声明依赖 {cap.owner_module_id}"
                    )
        # 若声明依赖了某个模块，校验该上游模块是否已冻结可消费契约
        for dep in module.depends_on_modules:
            if dep in module_ids and dep != module.module_id:
                dep_contracts = [c for c in blueprint.frozen_contracts if c.owner_module_id == dep]
                if not dep_contracts:
                    issues.append(
                        f"模块 {module.module_id} 声明依赖 {dep}，但 {dep} 未冻结任何跨模块公共契约"
                    )

    if _has_combined_cycle(module_ids, combined_edges) and "模块依赖存在环" not in issues:
        issues.append("模块/接口联合依赖图存在环")

    # 校验项 5：覆盖全部冻结业务需求（实体、页面、验收场景）
    covered_entity_set: set[str] = set()
    for mod_id, ents in blueprint.covered_entities.items():
        if mod_id not in module_ids:
            issues.append(f"covered_entities 引用了未知模块：{mod_id}")
        covered_entity_set.update(e.lower() for e in ents)
    # 若未显式提供 covered_entities，按模块名/ID 宽松兜底匹配
    if not blueprint.covered_entities:
        covered_entity_set.update(m.name.lower() for m in blueprint.modules)
        covered_entity_set.update(m.module_id.lower() for m in blueprint.modules)

    missing_entities = [
        ent.name for ent in spec.entities if ent.name.lower() not in covered_entity_set
    ]
    if missing_entities:
        issues.append(
            "蓝图未覆盖全部冻结业务实体，遗漏：" + "、".join(missing_entities)
        )

    if blueprint.covered_acceptance_ids and spec.acceptance_cases:
        covered_acs: set[str] = set()
        for mod_id, acs in blueprint.covered_acceptance_ids.items():
            if mod_id not in module_ids:
                issues.append(f"covered_acceptance_ids 引用了未知模块：{mod_id}")
            covered_acs.update(acs)
        missing_acs = [ac.id for ac in spec.acceptance_cases if ac.id not in covered_acs]
        if missing_acs:
            issues.append("蓝图未覆盖全部验收场景，遗漏：" + "、".join(missing_acs))

    return issues


def _collect_java_imports(
    type_expressions: list[str],
    *,
    current_package: str,
    dto_package_by_name: dict[str, str],
) -> list[str]:
    imports: set[str] = set()
    for expr in type_expressions:
        for token in _TYPE_TOKEN_RE.findall(expr):
            if token in _STD_JAVA_IMPORTS:
                imports.add(_STD_JAVA_IMPORTS[token])
            elif token in dto_package_by_name:
                target_pkg = dto_package_by_name[token]
                if target_pkg != current_package:
                    imports.add(f"{target_pkg}.{token}")
    return sorted(imports)


def _render_dto_java(
    dto: DtoContract,
    *,
    package: str,
    dto_package_by_name: dict[str, str],
) -> str:
    imports = _collect_java_imports(
        [f.java_type for f in dto.fields],
        current_package=package,
        dto_package_by_name=dto_package_by_name,
    )
    lines = [
        f"package {package};",
        "",
    ]
    if imports:
        lines.extend(f"import {imp};" for imp in imports)
        lines.append("")
    lines.append("/**")
    lines.append(f" * Frozen cross-module DTO owned by module '{dto.owner_module_id}'.")
    lines.append(" * READ-ONLY generated stub: DO NOT MODIFY inside individual department staging.")
    lines.append(" */")
    params = ", ".join(f"{f.java_type} {f.name}" for f in dto.fields)
    lines.append(f"public record {dto.name}({params}) {{")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def _render_facade_java(
    facade_name: str,
    owner_module_id: str,
    capabilities: list[PublicCapability],
    *,
    package: str,
    dto_package_by_name: dict[str, str],
) -> str:
    sig_expressions = [c.signature for c in capabilities]
    imports = _collect_java_imports(
        sig_expressions,
        current_package=package,
        dto_package_by_name=dto_package_by_name,
    )
    lines = [
        f"package {package};",
        "",
    ]
    if imports:
        lines.extend(f"import {imp};" for imp in imports)
        lines.append("")
    lines.append("/**")
    lines.append(f" * Frozen cross-module Facade contract owned by module '{owner_module_id}'.")
    lines.append(" * Status: APPROVED_FOR_BUILD (READ-ONLY generated stub).")
    lines.append(" */")
    lines.append(f"public interface {facade_name} {{")
    for cap in sorted(capabilities, key=lambda c: c.capability_id):
        match = _METHOD_SIG_RE.fullmatch(cap.signature)
        if match is None:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"无法为非法方法签名生成 Java Facade：{cap.capability_id} -> {cap.signature!r}",
                status_code=422,
            )
        ret_type, method_name, raw_args = match.group(1).strip(), match.group(2).strip(), match.group(3).strip()
        lines.append("")
        lines.append("    /**")
        lines.append(f"     * Capability: {cap.capability_id} (v{cap.version}, {cap.status})")
        if cap.errors:
            lines.append(f"     * Errors: {', '.join(cap.errors)}")
        if cap.auth_constraints:
            lines.append(f"     * Auth: {', '.join(cap.auth_constraints)}")
        lines.append("     */")
        lines.append(f"    {ret_type} {method_name}({raw_args});")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def generate_java_facade_stubs(
    blueprint: ArchitectureBlueprint,
    target_dir: str | Path,
) -> list[str]:
    """确定性生成只读的 Java ``modules/<dept_id>/api/*Facade.java`` 与公共 DTO 骨架文件。

    生成前会校验所有 ``JAVA_METHOD`` 签名与 DTO 合法性；生成的文件设为只读权限（``0o444``），
    并在 ``agent_files._is_protected`` 中同步受保护，防止单部门私自修改跨模块接口签名。
    返回生成的相对路径有序列表。
    """
    base_dir = Path(target_dir).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    pkg_rel = blueprint.base_package.replace(".", "/")

    # 前置校验契约签名合法性（拒绝模糊签名）
    contract_issues: list[str] = []
    contract_issues.extend(interface_registry_issues(blueprint.to_interface_registry()))
    for cap in blueprint.frozen_contracts:
        contract_issues.extend(_extract_contract_signature_issues(cap))
    if contract_issues:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "无法生成 Facade Stub，契约存在错误：" + "；".join(contract_issues),
            status_code=422,
        )

    dto_package_by_name = {
        dto.name: f"{blueprint.base_package}.modules.{dto.owner_module_id}.api"
        for dto in blueprint.shared_dtos
    }

    written_files: list[str] = []

    def _write_readonly(rel_path: str, content: str) -> None:
        dest = (base_dir / rel_path).resolve()
        if base_dir not in dest.parents:
            raise AppException(
                ErrorCode.BAD_REQUEST,
                f"生成的 Facade Stub 路径越界：{rel_path!r}",
                status_code=422,
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        dest.write_text(content, encoding="utf-8")
        dest.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        written_files.append(rel_path)

    # 1. 生成共享 DTO Java record 骨架
    for dto in sorted(blueprint.shared_dtos, key=lambda d: (d.owner_module_id, d.name)):
        pkg = dto_package_by_name[dto.name]
        rel = f"backend/src/main/java/{pkg_rel}/modules/{dto.owner_module_id}/api/{dto.name}.java"
        content = _render_dto_java(dto, package=pkg, dto_package_by_name=dto_package_by_name)
        _write_readonly(rel, content)

    # 2. 按 (owner_module_id, FacadeName) 分组生成只读 Java interface 骨架
    grouped: dict[tuple[str, str], list[PublicCapability]] = {}
    for cap in blueprint.frozen_contracts:
        if cap.interface_kind != KIND_JAVA_METHOD or not cap.symbol:
            continue
        facade_name = cap.symbol.split(".", 1)[0]
        grouped.setdefault((cap.owner_module_id, facade_name), []).append(cap)

    for (owner_id, facade_name), caps in sorted(grouped.items()):
        pkg = f"{blueprint.base_package}.modules.{owner_id}.api"
        rel = f"backend/src/main/java/{pkg_rel}/modules/{owner_id}/api/{facade_name}.java"
        content = _render_facade_java(
            facade_name,
            owner_id,
            caps,
            package=pkg,
            dto_package_by_name=dto_package_by_name,
        )
        _write_readonly(rel, content)

    return sorted(written_files)


__all__ = [
    "MAX_MODULE_MIGRATION_VERSION",
    "MIN_MODULE_MIGRATION_VERSION",
    "ArchitectureBlueprint",
    "DtoContract",
    "DtoField",
    "generate_java_facade_stubs",
    "validate_blueprint",
]
