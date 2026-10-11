"""M13 资产注册表测试（定位 / checksum / Profile 兼容 / 结构化错误）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentforge_platform.asset_registry import (
    AssetRegistry,
    compute_checksum,
    default_registry,
)
from agentforge_platform.errors import AppException


def test_default_registry_lists_all_assets() -> None:
    registry = default_registry()

    keys = {(m.kind, m.id, m.version) for m in registry.all()}
    assert ("template", "base-template", "demo-0.1") in keys
    assert ("capability-pack", "task-crud-backend", "0.1.0") in keys
    assert ("capability-pack", "task-crud-frontend", "0.1.0") in keys


def test_template_declares_provided_capabilities() -> None:
    manifest, _ = default_registry().get("template", "base-template", "demo-0.1")

    assert "auth" in manifest.provided_capabilities
    assert "jwt-security" in manifest.provided_capabilities
    assert manifest.provides == []
    assert "demo-ts-mp" in manifest.compatible_profiles


def test_all_default_checksums_verify() -> None:
    registry = default_registry()

    for manifest in registry.all():
        registry.verify_checksum(manifest.kind, manifest.id, manifest.version)


def test_missing_asset_returns_structured_error() -> None:
    with pytest.raises(AppException) as error:
        default_registry().get("capability-pack", "does-not-exist", "0.0.0")

    assert error.value.code == "ASSET_NOT_FOUND"


def test_incompatible_profile_returns_structured_error() -> None:
    registry = default_registry()

    with pytest.raises(AppException) as error:
        registry.resolve("capability-pack", "task-crud-backend", "0.1.0", "some-other-stack")
    assert error.value.code == "ASSET_INCOMPATIBLE"

    manifest, directory = registry.resolve(
        "capability-pack", "task-crud-backend", "0.1.0", "demo-ts-mp"
    )
    assert manifest.id == "task-crud-backend"
    assert directory.is_dir()


def _write_synthetic_asset(directory: Path, content: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "content.txt").write_text(content, encoding="utf-8")
    manifest = {
        "id": "sample",
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
        "checksum": compute_checksum(directory),
        "license": "internal",
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_checksum_mismatch_is_detected(tmp_path: Path) -> None:
    asset_dir = tmp_path / "sample"
    _write_synthetic_asset(asset_dir, "hello")
    registry = AssetRegistry([tmp_path])

    registry.verify_checksum("capability-pack", "sample", "0.1.0")

    (asset_dir / "content.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(AppException) as error:
        registry.verify_checksum("capability-pack", "sample", "0.1.0")
    assert error.value.code == "ASSET_CHECKSUM_MISMATCH"


def test_checksum_ignores_manifest_and_build_dirs(tmp_path: Path) -> None:
    asset_dir = tmp_path / "a"
    (asset_dir / "src").mkdir(parents=True)
    (asset_dir / "src" / "f.txt").write_text("x", encoding="utf-8")
    (asset_dir / "node_modules").mkdir()
    (asset_dir / "node_modules" / "junk.txt").write_text("junk", encoding="utf-8")

    baseline = compute_checksum(asset_dir)

    (asset_dir / "node_modules" / "junk.txt").write_text("changed", encoding="utf-8")
    (asset_dir / "manifest.json").write_text("{}", encoding="utf-8")
    assert compute_checksum(asset_dir) == baseline
