"""M15 确定性代码装配器测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentforge_platform.assembler import (
    AssemblyLimits,
    assemble,
    assemble_from_spec,
)
from agentforge_platform.asset_registry import AssetRegistry, default_registry
from agentforge_platform.contract import compile_from_spec
from agentforge_platform.errors import AppException
from agentforge_platform.resolver import resolve_assets
from agentforge_platform.spec import SoftwareSpecification

REPO = Path(__file__).resolve().parents[2]
SAMPLE_SPEC = REPO / "schemas" / "task-management.spec.json"

_BASE_MANIFEST = {
    "id": "asset",
    "version": "0.1.0",
    "kind": "capability-pack",
    "compatible_profiles": ["p"],
    "requires": [],
    "provides": [],
    "conflicts": [],
    "parameter_schema": {},
    "extension_points": [],
    "owned_paths": [],
    "checks": [],
    "checksum": "",
    "license": "internal",
}


def _spec() -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(SAMPLE_SPEC.read_text(encoding="utf-8"))


def _contract():
    return compile_from_spec(_spec())


def _synthetic_registry(root: Path, renders: list[dict]) -> AssetRegistry:
    template = root / "tpl"
    template.mkdir(parents=True)
    (template / "manifest.json").write_text(
        json.dumps({**_BASE_MANIFEST, "kind": "template", "id": "tpl"}), encoding="utf-8"
    )
    (template / "compose.yaml").write_text(
        "services:\n  mysql:\n    image: mysql:8.0.43@sha256:" + "0" * 64 + "\n",
        encoding="utf-8",
    )
    pack = root / "pack"
    (pack / "templates").mkdir(parents=True)
    (pack / "templates" / "x.j2").write_text("hello", encoding="utf-8")
    (pack / "manifest.json").write_text(
        json.dumps({**_BASE_MANIFEST, "kind": "capability-pack", "id": "pack", "renders": renders}),
        encoding="utf-8",
    )
    return AssetRegistry([root])


def test_assemble_task_project(tmp_path: Path) -> None:
    result = assemble_from_spec(_spec(), tmp_path / "run")

    paths = {entry.path for entry in result.files}
    assert "backend/pom.xml" in paths
    assert "backend/src/main/java/com/agentforge/template/controller/TaskController.java" in paths
    assert "backend/src/main/resources/db/migration/V2__create_task.sql" in paths
    assert "frontend/src/views/TaskListView.vue" in paths
    assert "compose.yaml" in paths
    assert "README.md" in paths
    assert "asset-lock.json" in paths
    assert result.lock_hash and result.manifest_hash
    assert "mybatis-plus-jsqlparser" in (tmp_path / "run/backend/pom.xml").read_text("utf-8")


def test_assemble_is_deterministic(tmp_path: Path) -> None:
    first = assemble_from_spec(_spec(), tmp_path / "a")
    second = assemble_from_spec(_spec(), tmp_path / "b")

    assert first.manifest_hash == second.manifest_hash
    assert [(f.path, f.sha256) for f in first.files] == [
        (f.path, f.sha256) for f in second.files
    ]


def test_assembly_does_not_modify_template(tmp_path: Path) -> None:
    assemble_from_spec(_spec(), tmp_path / "run")

    registry = default_registry()
    for manifest in registry.all():
        registry.verify_checksum(manifest.kind, manifest.id, manifest.version)


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    registry = _synthetic_registry(
        tmp_path / "assets", [{"template": "x.j2", "target": "../escape.txt"}]
    )
    lock = resolve_assets(registry, "p")

    with pytest.raises(AppException) as error:
        assemble(_contract(), lock, registry, tmp_path / "out", spec=_spec())

    assert error.value.code == "ASSEMBLY_ERROR"


def test_duplicate_target_is_rejected(tmp_path: Path) -> None:
    registry = _synthetic_registry(
        tmp_path / "assets",
        [{"template": "x.j2", "target": "dup.txt"}, {"template": "x.j2", "target": "dup.txt"}],
    )
    lock = resolve_assets(registry, "p")

    with pytest.raises(AppException) as error:
        assemble(_contract(), lock, registry, tmp_path / "out", spec=_spec())

    assert error.value.code == "ASSEMBLY_ERROR"


def test_file_count_limit_is_enforced(tmp_path: Path) -> None:
    registry = _synthetic_registry(
        tmp_path / "assets",
        [{"template": "x.j2", "target": "a.txt"}, {"template": "x.j2", "target": "b.txt"}],
    )
    lock = resolve_assets(registry, "p")

    with pytest.raises(AppException) as error:
        assemble(
            _contract(),
            lock,
            registry,
            tmp_path / "out",
            spec=_spec(),
            limits=AssemblyLimits(max_files=1),
        )

    assert error.value.code == "ASSEMBLY_ERROR"
