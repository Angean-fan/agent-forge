"""M14 Asset Resolver 与锁清单测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentforge_platform.asset_registry import AssetRegistry, default_registry
from agentforge_platform.errors import AppException
from agentforge_platform.resolver import AssetLock, resolve_assets

REPO = Path(__file__).resolve().parents[2]
SAMPLE_LOCK = REPO / "schemas" / "asset-lock.r27.sample.json"


def _write_manifest(directory: Path, **fields) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
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
    manifest.update(fields)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if manifest["kind"] == "template":
        (directory / "compose.yaml").write_text(
            "services:\n  mysql:\n    image: mysql:8.0.43@sha256:" + "0" * 64 + "\n",
            encoding="utf-8",
        )


def test_resolve_task_lock() -> None:
    lock = resolve_assets(default_registry(), "demo-ts-mp")

    assert lock.profile == "demo-ts-mp"
    assert {asset.id for asset in lock.assets} == {
        "base-template", "task-crud-backend", "task-crud-frontend"
    }
    assert [asset.kind for asset in lock.assets] == ["capability-pack", "capability-pack", "template"]
    assert all(asset.checksum for asset in lock.assets)
    assert all(asset.version for asset in lock.assets)
    assert lock.lock_hash


def test_resolve_is_stable() -> None:
    first = resolve_assets(default_registry(), "demo-ts-mp")
    second = resolve_assets(default_registry(), "demo-ts-mp")

    assert first.lock_hash == second.lock_hash
    assert first.model_dump() == second.model_dump()


def test_committed_sample_matches() -> None:
    committed = AssetLock.model_validate_json(SAMPLE_LOCK.read_text(encoding="utf-8"))

    assert resolve_assets(default_registry(), "demo-ts-mp").lock_hash == committed.lock_hash


def test_duplicate_capability_is_rejected(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "tpl", kind="template", id="tpl")
    _write_manifest(tmp_path / "p1", id="p1", provides=["crud"])
    _write_manifest(tmp_path / "p2", id="p2", provides=["crud"])

    with pytest.raises(AppException) as error:
        resolve_assets(AssetRegistry([tmp_path]), "p")

    assert error.value.code == "ASSET_CONFLICT"
    assert "crud" in error.value.message


def test_path_conflict_is_rejected(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "tpl", kind="template", id="tpl")
    _write_manifest(tmp_path / "p1", id="p1", owned_paths=["src/api/"])
    _write_manifest(tmp_path / "p2", id="p2", owned_paths=["src/api/task.ts"])

    with pytest.raises(AppException) as error:
        resolve_assets(AssetRegistry([tmp_path]), "p")

    assert error.value.code == "ASSET_CONFLICT"


def test_unmet_requirement_is_rejected(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "tpl", kind="template", id="tpl", provided_capabilities=["auth"])
    _write_manifest(tmp_path / "p1", id="p1", requires=["template:nonexistent"])

    with pytest.raises(AppException) as error:
        resolve_assets(AssetRegistry([tmp_path]), "p")

    assert error.value.code == "ASSET_CONFLICT"


def test_missing_template_is_rejected(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "p1", id="p1")

    with pytest.raises(AppException) as error:
        resolve_assets(AssetRegistry([tmp_path]), "p")

    assert error.value.code == "ASSET_CONFLICT"
