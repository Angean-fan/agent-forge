"""E01：Integration Pack 只读清单、版本策略与 Resolver 锁定（Mock-only）。

覆盖：Mock-only 清单可被锁定并通过 Profile 兼容筛选；无请求时锁与 MVP 0.1 完全一致；
重复/未知/不兼容/冲突/能力重复/缺失依赖/路径覆盖/危险路径被拒；mock-only 结构约束
（拒绝 live 模式、非 mock adapter、非确定性 mock、必需的真实秘密、无来源/不明许可证）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentforge_platform.asset_registry import (
    AssetRegistry,
    IntegrationPackManifest,
    compute_checksum,
    default_registry,
    integration_registry,
)
from agentforge_platform.errors import AppException
from agentforge_platform.integration_lock import resolve_integration_lock
from agentforge_platform.resolver import AssetLock, resolve_assets, verify_lock_hash

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "schemas" / "asset-lock.integration.sample.json"
WEATHER = REPO / "integration-packs" / "weather-lookup" / "0.1.0"


def _pack(**overrides) -> dict:
    manifest = {
        "id": "sample-pack",
        "version": "0.1.0",
        "kind": "integration-pack",
        "provider": "sample-provider",
        "mode": "mock-only",
        "compatible_profiles": ["demo-ts-mp"],
        "requires": [],
        "provides": ["sample_capability"],
        "conflicts": [],
        "parameter_schema": {},
        "extension_points": [],
        "owned_paths": ["src/main/java/com/agentforge/integration/sample/"],
        "target_dir": "backend",
        "ports": [{"name": "SamplePort", "methods": ["String fetch()"]}],
        "adapters": [{"name": "MockSampleAdapter", "kind": "mock"}],
        "mock": {"deterministic": True, "scenarios": ["success"]},
        "config": [],
        "checks": ["mvn test"],
        "checksum": "",
        "license": "internal",
        "source_provenance": "test:e01",
    }
    manifest.update(overrides)
    return manifest


def _write_asset(directory: Path, **fields) -> dict:
    """写一个模板/能力包 manifest（合成注册表用）。"""
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
    return manifest


def _write_pack(root: Path, asset_id: str, version: str = "0.1.0", **overrides) -> Path:
    directory = root / asset_id / version
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "content.txt").write_text("x", encoding="utf-8")
    manifest = _pack(id=asset_id, version=version, **overrides)
    manifest["checksum"] = compute_checksum(directory)
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return directory


# --- 成功：锁定与 Profile 兼容 -------------------------------------------------

def test_mock_only_pack_locks_and_is_profile_compatible() -> None:
    registry = integration_registry()
    lock = resolve_integration_lock(registry, "demo-ts-mp", ["weather-lookup"])

    assert lock.schema_version == "0.3"
    assert [unit.id for unit in lock.integration_packs] == ["weather-lookup"]
    assert lock.integration_packs[0].kind == "integration-pack"
    assert lock.integration_packs[0].checksum
    verify_lock_hash(lock)  # 0.3 锁哈希覆盖 integration_packs，可防篡改

    manifest, directory = registry.resolve(
        "integration-pack", "weather-lookup", "0.1.0", "demo-ts-mp"
    )
    assert manifest.id == "weather-lookup"
    assert directory.is_dir()


def test_no_requests_keeps_mvp_lock_unchanged() -> None:
    base = resolve_assets(default_registry(), "demo-ts-mp")
    lock = resolve_integration_lock(integration_registry(), "demo-ts-mp", [])

    assert lock.schema_version == "0.1"
    assert lock.model_dump() == base.model_dump()
    assert lock.lock_hash == base.lock_hash


def test_lock_is_stable_and_matches_committed_sample() -> None:
    registry = integration_registry()
    first = resolve_integration_lock(registry, "demo-ts-mp", ["weather-lookup"])
    second = resolve_integration_lock(registry, "demo-ts-mp", ["weather-lookup"])

    assert first.model_dump() == second.model_dump()
    committed = AssetLock.model_validate_json(SAMPLE.read_text(encoding="utf-8"))
    assert first.lock_hash == committed.lock_hash


def test_default_registry_excludes_integration_packs() -> None:
    # 集成包不进入生成默认注册表 → MVP 冷启动的资产选择与 0.1 锁不受影响
    kinds = {manifest.kind for manifest in default_registry().all()}
    assert "integration-pack" not in kinds
    assert kinds <= {"template", "capability-pack"}


# --- 清单字段与完整性 ---------------------------------------------------------

def test_manifest_declares_ports_adapters_config_mock() -> None:
    manifest, _ = integration_registry().get("integration-pack", "weather-lookup", "0.1.0")

    assert isinstance(manifest, IntegrationPackManifest)
    assert manifest.mode == "mock-only"
    assert [port.name for port in manifest.ports] == ["WeatherPort"]
    assert [adapter.kind for adapter in manifest.adapters] == ["mock"]
    assert manifest.mock.deterministic is True
    assert manifest.config and not any(item.secret for item in manifest.config)
    assert manifest.license and manifest.source_provenance
    assert manifest.checksum


def test_checksum_verifies_and_tamper_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "integration-packs"
    directory = _write_pack(root, "sample-pack")
    registry = AssetRegistry([root])
    registry.verify_checksum("integration-pack", "sample-pack", "0.1.0")

    (directory / "content.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(AppException) as error:
        registry.verify_checksum("integration-pack", "sample-pack", "0.1.0")
    assert error.value.code == "ASSET_CHECKSUM_MISMATCH"


# --- 失败：重复 / 未知 / 不兼容 ------------------------------------------------

def test_duplicate_request_is_rejected() -> None:
    with pytest.raises(AppException) as error:
        resolve_integration_lock(
            integration_registry(), "demo-ts-mp", ["weather-lookup", "weather-lookup"]
        )
    assert error.value.code == "ASSET_CONFLICT"


def test_unknown_pack_is_rejected() -> None:
    with pytest.raises(AppException) as error:
        resolve_integration_lock(integration_registry(), "demo-ts-mp", ["does-not-exist"])
    assert error.value.code == "ASSET_NOT_FOUND"


def test_incompatible_profile_is_rejected() -> None:
    with pytest.raises(AppException) as error:
        resolve_integration_lock(integration_registry(), "demo-ts-mb", ["weather-lookup"])
    assert error.value.code == "ASSET_INCOMPATIBLE"


# --- 失败：冲突 / 能力重复 / 缺失依赖 / 路径覆盖 --------------------------------

def test_missing_dependency_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    _write_asset(root / "tpl", kind="template", id="tpl", provided_capabilities=["auth"])
    _write_pack(root, "sample-pack", compatible_profiles=["p"], requires=["template:nonexistent"], provides=["sample_capability"])

    with pytest.raises(AppException) as error:
        resolve_integration_lock(AssetRegistry([root]), "p", ["sample-pack"])
    assert error.value.code == "ASSET_CONFLICT"
    assert "缺失依赖" in error.value.message


def test_conflict_with_present_capability_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    _write_asset(root / "tpl", kind="template", id="tpl")
    _write_asset(root / "cap", id="cap", provides=["crud"])
    _write_pack(root, "sample-pack", compatible_profiles=["p"], conflicts=["crud"], provides=["sample_capability"])

    with pytest.raises(AppException) as error:
        resolve_integration_lock(AssetRegistry([root]), "p", ["sample-pack"])
    assert error.value.code == "ASSET_CONFLICT"
    assert "冲突" in error.value.message


def test_duplicate_capability_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    _write_asset(root / "tpl", kind="template", id="tpl")
    _write_asset(root / "cap", id="cap", provides=["crud"])
    _write_pack(root, "sample-pack", compatible_profiles=["p"], provides=["crud"])

    with pytest.raises(AppException) as error:
        resolve_integration_lock(AssetRegistry([root]), "p", ["sample-pack"])
    assert error.value.code == "ASSET_CONFLICT"
    assert "能力重复" in error.value.message


def test_path_overlap_with_capability_pack_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    _write_asset(root / "tpl", kind="template", id="tpl")
    _write_asset(
        root / "cap", id="cap",
        target_dir="backend", owned_paths=["src/main/java/com/agentforge/template/service/"],
    )
    _write_pack(
        root, "sample-pack", compatible_profiles=["p"],
        target_dir="backend", owned_paths=["src/main/java/com/agentforge/template/service/"],
    )

    with pytest.raises(AppException) as error:
        resolve_integration_lock(AssetRegistry([root]), "p", ["sample-pack"])
    assert error.value.code == "ASSET_CONFLICT"
    assert "路径覆盖" in error.value.message


# --- 失败：mock-only 结构约束（parse 阶段即拒） --------------------------------

def test_non_mock_only_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        IntegrationPackManifest.model_validate(_pack(mode="live"))


def test_non_mock_adapter_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        IntegrationPackManifest.model_validate(_pack(adapters=[{"name": "HttpAdapter", "kind": "http"}]))


def test_non_deterministic_mock_is_rejected() -> None:
    with pytest.raises(ValidationError):
        IntegrationPackManifest.model_validate(_pack(mock={"deterministic": False, "scenarios": []}))


def test_required_real_secret_in_mock_only_is_rejected() -> None:
    with pytest.raises(ValidationError):
        IntegrationPackManifest.model_validate(
            _pack(config=[{"env_name": "WEATHER_API_KEY", "required": True, "secret": True}])
        )


def test_missing_provenance_or_unclear_license_is_rejected() -> None:
    with pytest.raises(ValidationError):
        IntegrationPackManifest.model_validate(_pack(source_provenance="   "))
    with pytest.raises(ValidationError):
        IntegrationPackManifest.model_validate(_pack(license="unknown"))


def test_dangerous_owned_paths_are_rejected() -> None:
    for bad in ("/etc/passwd", "../../secrets/", "C:/Windows/System32/"):
        with pytest.raises(ValidationError):
            IntegrationPackManifest.model_validate(_pack(owned_paths=[bad]))
