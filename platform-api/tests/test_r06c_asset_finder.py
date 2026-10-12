"""R06C：Asset Finder——能力查询派生、精确/FTS 召回、确定性兼容筛选与拒绝原因。"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from agentforge_platform.asset_finder import (
    MATCH_EXACT,
    MATCH_NONE,
    MATCH_PARAMETERIZED,
    MATCH_STRUCTURED,
    Candidate,
    derive_capability_query,
    find_assets,
)
from agentforge_platform.asset_registry import AssetRegistry, compute_checksum, default_registry
from agentforge_platform.contract import compile_from_spec
from agentforge_platform.method_index import build_index
from agentforge_platform.spec import SoftwareSpecification

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "schemas" / "task-management.spec.json"
METHODS = REPO / "methods"


def _spec() -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(SAMPLE.read_text(encoding="utf-8"))


def _query(spec: SoftwareSpecification | None = None):
    spec = spec or _spec()
    return derive_capability_query(spec, compile_from_spec(spec))


def _candidate(kind: str = "method-unit", **overrides) -> Candidate:
    base = {
        "kind": kind, "id": "candidate", "version": "1.0.0",
        "provides": (), "requires": (), "conflicts": (), "extension_points": (),
        "compatible_profiles": ("demo-ts-mp",), "license": "internal",
        "parameter_schema": {}, "language": None, "framework": None, "acceptance_ids": (),
    }
    base.update(overrides)
    return Candidate(**base)


# --- 查询派生 -----------------------------------------------------------------

def test_query_derivation_is_deterministic_and_text_free() -> None:
    first, second = _query(), _query()

    assert first.query_hash == second.query_hash
    assert {"crud", "pagination", "search", "data_ownership"} <= set(first.capability_ids)
    assert first.required_acceptance_ids == ("AC01", "AC02")
    assert ("java", "spring-boot") in first.stacks


def test_query_hash_differs_when_capabilities_differ() -> None:
    base = _query()
    changed = dataclasses.replace(base, capability_ids=("crud",))

    assert base.query_hash != changed.query_hash


# --- 全量检索（默认注册表 + 方法索引） ----------------------------------------

def test_find_on_frozen_task_spec(tmp_path: Path) -> None:
    index = tmp_path / "idx.sqlite"
    build_index(METHODS, index)

    result = find_assets(
        _query(), methods_root_path=METHODS, index_path_override=index
    )

    # 能力包提供 crud/pagination/search/data_ownership 但需参数化 → PARAMETERIZED
    assert result.match_type == MATCH_PARAMETERIZED
    assert "capability-pack:task-crud-backend@0.1.2" in result.selected_refs
    assert "template:base-template@demo-0.2" not in result.selected_refs  # 不提供业务能力
    assert result.index_revision
    assert result.query_hash == _query().query_hash
    # 未选中任何方法单元 → 冻结验收 ID 未被覆盖（诚实暴露缺口）
    assert result.uncovered_acceptance_ids == ("AC01", "AC02")


def test_find_is_stable(tmp_path: Path) -> None:
    index = tmp_path / "idx.sqlite"
    build_index(METHODS, index)
    kwargs = {"methods_root_path": METHODS, "index_path_override": index}

    first = find_assets(_query(), **kwargs)
    second = find_assets(_query(), **kwargs)

    assert first == second


# --- 区分三类情况 -------------------------------------------------------------

def test_no_candidate_is_no_match() -> None:
    query = dataclasses.replace(_query(), capability_ids=("nonexistent_capability",), tags=())

    result = find_assets(query, candidates=[_candidate(provides=("crud",))])

    assert result.match_type == MATCH_NONE
    assert result.candidate_refs == ()
    assert result.selected_refs == ()
    assert result.uncovered_acceptance_ids == ("AC01", "AC02")


def test_incompatible_profile_is_rejected_with_reason(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    directory = root / "some-pack" / "1.0.0"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps({
        "id": "some-pack", "version": "1.0.0", "kind": "capability-pack",
        "compatible_profiles": ["other-stack"], "requires": [], "provides": ["crud"],
        "conflicts": [], "parameter_schema": {"entity_name": "string"},
        "extension_points": [], "owned_paths": [], "checks": [], "checksum": "",
        "license": "internal",
    }), encoding="utf-8")

    result = find_assets(_query(), registry=AssetRegistry([root]))

    assert result.match_type == MATCH_NONE
    assert result.candidate_refs == ("capability-pack:some-pack@1.0.0",)  # 检索到
    assert "profile_incompatible" in result.filter_reasons["capability-pack:some-pack@1.0.0"]


def test_similar_name_is_not_exact_coverage() -> None:
    # 名字/描述相近但 provides 不同：不得被当作 EXACT 覆盖
    query = dataclasses.replace(_query(), capability_ids=("date_normalization",), tags=())
    similar = _candidate(
        id="date-util", provides=("date_formatting",),
        language="java", framework="spring-boot",
    )

    result = find_assets(
        query, registry=AssetRegistry([]), candidates=[similar],
        methods_root_path=Path("/nonexistent"), index_path_override=Path("/nonexistent.sqlite"),
    )

    assert result.match_type == MATCH_NONE
    assert "method-unit:date-util@1.0.0" not in result.selected_refs


def test_structurally_valid_recall_candidate_is_structured_not_exact(tmp_path: Path) -> None:
    # 方法经 FTS 标签召回（tags 命中 "crud"）但 provides 不含所需能力 ID → 非精确覆盖
    root = tmp_path / "methods"
    directory = root / "crud-tagger" / "1.0.0"
    (directory / "src").mkdir(parents=True)
    (directory / "src" / "Helper.java").write_text("class Helper {}", encoding="utf-8")
    manifest = {
        "id": "crud-tagger", "version": "1.0.0", "kind": "method-unit",
        "compatible_profiles": ["demo-ts-mp"], "requires": [], "provides": ["misc_helper"],
        "conflicts": [], "parameter_schema": {}, "extension_points": [],
        "owned_paths": ["backend/src/util/"], "checks": [], "checksum": "",
        "license": "internal", "symbol": "pkg.crud_tagger.run", "signature": "void run()",
        "layer": "util", "language": "java", "framework": "spring-boot",
        "tags": ["crud"], "source_provenance": "test:r06c",
    }
    manifest["checksum"] = compute_checksum(directory)
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    index = tmp_path / "idx.sqlite"
    build_index(root, index)

    query = dataclasses.replace(_query(), capability_ids=("crud",), tags=("crud",))
    result = find_assets(
        query, registry=AssetRegistry([]), methods_root_path=root, index_path_override=index
    )

    assert result.candidate_refs == ("method-unit:crud-tagger@1.0.0",)
    assert result.match_type == MATCH_STRUCTURED  # 通过筛选但非精确-ID 证据


# --- 确定性拒绝：依赖冲突 / 扩展点 / 栈 / 许可证 ------------------------------

def test_unmet_dependency_and_conflict_are_rejected() -> None:
    query = _query()
    unmet = _candidate(id="needs-missing", provides=("data_ownership",),
                       requires=("template:missing",))
    conflicting = _candidate(id="conflicts", provides=("data_ownership",),
                             conflicts=("crud",))

    result = find_assets(
        query, registry=default_registry(), candidates=[unmet, conflicting],
        methods_root_path=Path("/nonexistent"),
    )

    assert "unmet_requirement:template:missing" in result.filter_reasons["method-unit:needs-missing@1.0.0"]
    assert "conflict" in result.filter_reasons["method-unit:conflicts@1.0.0"]
    assert result.selected_refs == ()


def test_unregistered_extension_point_is_rejected() -> None:
    query = dataclasses.replace(_query(), capability_ids=("date_normalization",), tags=())
    candidate = _candidate(
        id="injector", provides=("date_normalization",),
        extension_points=("backend/secret#inject",),
        language="java", framework="spring-boot",
    )

    result = find_assets(query, registry=default_registry(), candidates=[candidate])

    assert result.match_type == MATCH_NONE
    reasons = result.filter_reasons["method-unit:injector@1.0.0"]
    assert any(reason.startswith("unregistered_extension_point:") for reason in reasons)


def test_stack_mismatch_is_rejected() -> None:
    query = dataclasses.replace(_query(), capability_ids=("crud",), tags=())
    candidate = _candidate(id="go-helper", provides=("crud",), language="go", framework="gin")

    result = find_assets(query, registry=AssetRegistry([]), candidates=[candidate])

    assert "stack_mismatch" in result.filter_reasons["method-unit:go-helper@1.0.0"]


def test_unclear_license_is_rejected() -> None:
    query = dataclasses.replace(_query(), capability_ids=("crud",), tags=())
    candidate = _candidate(id="unlicensed", provides=("crud",), license="unknown")

    result = find_assets(query, registry=AssetRegistry([]), candidates=[candidate])

    assert "license_unclear" in result.filter_reasons["method-unit:unlicensed@1.0.0"]


def test_exact_match_when_capability_ids_align(tmp_path: Path) -> None:
    query = dataclasses.replace(_query(), capability_ids=("crud",), tags=())
    candidate = _candidate(id="crud-helper", provides=("crud",), parameter_schema={})

    result = find_assets(query, registry=AssetRegistry([]), candidates=[candidate])

    assert result.match_type == MATCH_EXACT
    assert result.selected_refs == ("method-unit:crud-helper@1.0.0",)
