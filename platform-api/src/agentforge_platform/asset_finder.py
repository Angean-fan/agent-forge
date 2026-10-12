"""R06C：Asset Finder——受限候选层（Registry/Resolver 之前的**只读**检索与兼容筛选）。

- 从**已冻结** spec + contract 派生**规范化能力查询**（能力 ID/验收 ID/技术栈），不读原始聊天；
- 先按能力 ID/标签/技术栈**精确匹配**，再用 **FTS5 关键词召回**相近方法候选；
- 对 Template / Capability Pack / Method Unit **统一做确定性兼容筛选**（Profile/语言框架/依赖/
  冲突/扩展点/许可证），排序固定可重复，返回 ``EXACT/PARAMETERIZED/STRUCTURED_CANDIDATE/NO_MATCH``
  与逐候选**拒绝原因**；
- **关键词相似度只用于召回，不能替代语义/权限/接口证据**；不能确定即 ``NO_MATCH``（转为 gap）；
- **不调用任何模型**（严禁偷偷用 Qwen/DeepSeek 裁决）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from agentforge_platform.asset_registry import (
    _FORBIDDEN_LICENSES,
    REPO_ROOT,
    AssetRegistry,
    default_registry,
    known_capabilities,
    known_extension_points,
)
from agentforge_platform.contract import BusinessContract
from agentforge_platform.errors import AppException
from agentforge_platform.method_index import (
    MethodRecord,
    index_path,
    methods_root,
    read_meta,
    scan_methods,
    search,
)
from agentforge_platform.spec import SoftwareSpecification

MATCH_EXACT = "EXACT"
MATCH_PARAMETERIZED = "PARAMETERIZED"
MATCH_STRUCTURED = "STRUCTURED_CANDIDATE"
MATCH_NONE = "NO_MATCH"

_SERVER_MANAGED_HINT = "owner_id"


@dataclass(frozen=True)
class CapabilityQuery:
    """规范化能力查询（确定性派生，不含用户原文/第三方秘密）。"""

    profile: str
    stacks: tuple[tuple[str, str], ...]
    capability_ids: tuple[str, ...]
    tags: tuple[str, ...]
    required_acceptance_ids: tuple[str, ...]

    @property
    def query_hash(self) -> str:
        payload = {
            "profile": self.profile,
            "stacks": [list(pair) for pair in self.stacks],
            "capability_ids": list(self.capability_ids),
            "tags": list(self.tags),
            "required_acceptance_ids": list(self.required_acceptance_ids),
        }
        return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Candidate:
    kind: str
    id: str
    version: str
    provides: tuple[str, ...]
    requires: tuple[str, ...]
    conflicts: tuple[str, ...]
    extension_points: tuple[str, ...]
    compatible_profiles: tuple[str, ...]
    license: str
    parameter_schema: dict[str, str]
    language: str | None = None
    framework: str | None = None
    acceptance_ids: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        return f"{self.kind}:{self.id}@{self.version}"


@dataclass(frozen=True)
class AssetSearchResult:
    query_hash: str
    index_revision: str | None
    match_type: str
    candidate_refs: tuple[str, ...]
    selected_refs: tuple[str, ...]
    filter_reasons: dict[str, list[str]] = field(default_factory=dict)
    uncovered_acceptance_ids: tuple[str, ...] = ()


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _stack_pairs(profile_id: str, profile_version: str) -> tuple[tuple[str, str], ...]:
    """从 Profile 文件读取可接受的技术栈（后端 + 前端）；缺失时回退 java/spring-boot。"""
    path = REPO_ROOT / "profiles" / f"{profile_id}-{profile_version}.json"
    stacks: list[tuple[str, str]] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for section in ("backend", "frontend"):
            block = payload.get(section) or {}
            if block.get("language") and block.get("framework"):
                stacks.append((str(block["language"]), str(block["framework"])))
    except (OSError, ValueError):
        pass
    if not stacks:
        stacks.append(("java", "spring-boot"))
    return tuple(dict.fromkeys(stacks))


def derive_capability_query(
    spec: SoftwareSpecification, contract: BusinessContract
) -> CapabilityQuery:
    """确定性派生：能力 ID 来自 contract 结构，验收 ID 来自 spec；不读取原始需求文本。"""
    capabilities: set[str] = set()
    if contract.tables and contract.routes:
        capabilities.add("crud")
    if any(route.operation == "list" for route in contract.routes):
        capabilities |= {"pagination", "search"}
    columns = {column.name for table in contract.tables for column in table.columns}
    if _SERVER_MANAGED_HINT in columns:
        capabilities.add("data_ownership")
    if any(table.unique_constraints for table in contract.tables):
        capabilities.add("unique_constraint")

    capability_ids = tuple(sorted(capabilities))
    required_acceptance_ids = tuple(sorted(case.id for case in spec.acceptance_cases))
    stacks = _stack_pairs(
        spec.technical_profile.profile_id, spec.technical_profile.profile_version
    )
    return CapabilityQuery(
        profile=spec.technical_profile.profile_id,
        stacks=stacks,
        capability_ids=capability_ids,
        tags=capability_ids,
        required_acceptance_ids=required_acceptance_ids,
    )


def collect_candidates(
    registry: AssetRegistry, methods_root: str | Path
) -> list[Candidate]:
    """汇集 Template/Capability Pack（注册表）与 Method Unit（方法仓库；权威 manifest）。"""
    candidates: list[Candidate] = []
    for manifest in registry.all():
        if manifest.kind == "method-unit":
            continue  # 方法单元以方法仓库为准，避免两套真值
        candidates.append(
            Candidate(
                kind=manifest.kind, id=manifest.id, version=manifest.version,
                provides=tuple(manifest.provides), requires=tuple(manifest.requires),
                conflicts=tuple(manifest.conflicts),
                extension_points=tuple(manifest.extension_points),
                compatible_profiles=tuple(manifest.compatible_profiles),
                license=manifest.license, parameter_schema=dict(manifest.parameter_schema),
            )
        )
    for record in scan_methods(methods_root):
        candidates.append(_candidate_from_method(record))
    candidates.sort(key=lambda candidate: (candidate.kind, candidate.id, candidate.version))
    return candidates


def _candidate_from_method(record: MethodRecord) -> Candidate:
    return Candidate(
        kind="method-unit", id=record.id, version=record.version,
        provides=tuple(record.provides), requires=tuple(record.requires),
        conflicts=tuple(record.conflicts), extension_points=tuple(record.extension_points),
        compatible_profiles=tuple(record.profiles), license=record.license,
        parameter_schema={}, language=record.language, framework=record.framework,
        acceptance_ids=tuple(record.acceptance_ids),
    )


def _capability_name(requirement: str) -> str:
    return requirement.split(":", 1)[-1]


def _rejections(
    candidate: Candidate,
    query: CapabilityQuery,
    *,
    available_capabilities: set[str],
    present_capabilities: set[str],
    registered_extension_points: set[str],
) -> list[str]:
    reasons: list[str] = []
    if query.profile not in candidate.compatible_profiles:
        reasons.append("profile_incompatible")
    if candidate.language is not None and (
        candidate.language, candidate.framework
    ) not in query.stacks:
        reasons.append("stack_mismatch")
    for requirement in candidate.requires:
        if _capability_name(requirement) not in available_capabilities:
            reasons.append(f"unmet_requirement:{requirement}")
    if set(candidate.conflicts) & present_capabilities:
        reasons.append("conflict")
    for point in candidate.extension_points:
        if point not in registered_extension_points:
            reasons.append(f"unregistered_extension_point:{point}")
    if candidate.license.strip().lower() in _FORBIDDEN_LICENSES:
        reasons.append("license_unclear")
    return reasons


def _match_type(exact_selected: list[Candidate], selected: list[Candidate]) -> str:
    if any(not c.parameter_schema for c in exact_selected):
        return MATCH_EXACT
    if exact_selected:
        return MATCH_PARAMETERIZED
    if selected:
        return MATCH_STRUCTURED
    return MATCH_NONE


def find_assets(
    query: CapabilityQuery,
    *,
    registry: AssetRegistry | None = None,
    methods_root_path: str | Path | None = None,
    index_path_override: str | Path | None = None,
    candidates: list[Candidate] | None = None,
) -> AssetSearchResult:
    """对同一冻结查询返回稳定候选与拒绝原因；不使用关键词相似度作为覆盖证据。"""
    registry = registry or default_registry()
    root = Path(methods_root_path) if methods_root_path is not None else methods_root()
    if candidates is None:
        candidates = collect_candidates(registry, root)
    candidates = sorted(candidates, key=lambda c: (c.kind, c.id, c.version))
    by_ref = {candidate.ref: candidate for candidate in candidates}

    exact_refs = {
        candidate.ref
        for candidate in candidates
        if set(candidate.provides) & set(query.capability_ids)
    }

    recall_refs: set[str] = set()
    revision: str | None = None
    index = Path(index_path_override) if index_path_override is not None else index_path()
    if index.is_file():
        try:
            revision = read_meta(index).get("index_revision")
            for hit in search(" ".join(query.capability_ids + query.tags), path=index):
                ref = f"method-unit:{hit['asset_id']}@{hit['version']}"
                if ref in by_ref:
                    recall_refs.add(ref)
        except AppException:
            recall_refs = set()

    candidate_refs = sorted(exact_refs | recall_refs)
    available = known_capabilities(registry, query.profile)
    present = set(query.capability_ids) | available
    registered_extension_points = known_extension_points(registry, query.profile)

    filter_reasons: dict[str, list[str]] = {}
    selected: list[Candidate] = []
    for ref in candidate_refs:
        candidate = by_ref[ref]
        reasons = _rejections(
            candidate, query,
            available_capabilities=available, present_capabilities=present,
            registered_extension_points=registered_extension_points,
        )
        filter_reasons[ref] = reasons
        if not reasons:
            selected.append(candidate)

    selected.sort(key=lambda candidate: (candidate.kind, candidate.id, candidate.version))
    selected_refs = tuple(candidate.ref for candidate in selected)
    exact_selected = [candidate for candidate in selected if candidate.ref in exact_refs]
    covered = {aid for candidate in selected for aid in candidate.acceptance_ids}
    uncovered = tuple(sorted(set(query.required_acceptance_ids) - covered))

    return AssetSearchResult(
        query_hash=query.query_hash,
        index_revision=revision,
        match_type=_match_type(exact_selected, selected),
        candidate_refs=tuple(candidate_refs),
        selected_refs=selected_refs,
        filter_reasons=filter_reasons,
        uncovered_acceptance_ids=uncovered,
    )


__all__ = [
    "MATCH_EXACT",
    "MATCH_NONE",
    "MATCH_PARAMETERIZED",
    "MATCH_STRUCTURED",
    "AssetSearchResult",
    "Candidate",
    "CapabilityQuery",
    "collect_candidates",
    "derive_capability_query",
    "find_assets",
]
