"""R06A：资产契约扩展——Method Unit 类型、按 kind 分支解析、锁版本化哈希。

覆盖：MVP 基线 asset-lock 原样重放（解析/哈希校验/按 ID+version+checksum 重放）、
旧 0.1 锁新增字段不改哈希、新版本锁可解析且篡改被拒、Method Unit 样例通过与
缺签名/危险路径/未知层/非法依赖/未知 Profile/冲突/无合法来源/未注册扩展点被拒。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentforge_platform.asset_registry import (
    AssetRegistry,
    MethodUnitManifest,
    default_registry,
    known_capabilities,
    known_extension_points,
    method_unit_issues,
    require_valid_method_unit,
)
from agentforge_platform.errors import AppException
from agentforge_platform.resolver import (
    AssetLock,
    LockedAsset,
    lock_hash_of,
    resolve_assets,
    verify_lock_hash,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "asset-lock-0.1-baseline.json"
SAMPLE_LOCK = REPO / "schemas" / "asset-lock.sample.json"


def _method_unit(**overrides) -> dict:
    manifest = {
        "id": "date-normalizer",
        "version": "1.0.0",
        "kind": "method-unit",
        "compatible_profiles": ["demo-ts-mp"],
        "requires": [],
        "provides": [],
        "conflicts": [],
        "parameter_schema": {},
        "extension_points": [],
        "owned_paths": ["backend/src/main/java/com/agentforge/template/util/"],
        "checks": ["mvn test"],
        "checksum": "",
        "license": "internal",
        "symbol": "com.agentforge.template.util.DateNormalizer.normalize",
        "signature": "String normalize(String raw)",
        "layer": "util",
        "language": "java",
        "framework": "spring-boot",
        "source_provenance": "internal:agentforge/r06a-review",
    }
    manifest.update(overrides)
    return manifest


# --- 基线锁重放 / 旧锁哈希不变 -------------------------------------------------

def test_mvp_baseline_lock_replays_unchanged() -> None:
    lock = AssetLock.model_validate_json(FIXTURE.read_text(encoding="utf-8"))

    # 哈希校验（不编辑、不重算）通过
    verify_lock_hash(lock)
    assert lock.method_units == []
    assert lock.lock_hash == AssetLock.model_validate_json(
        SAMPLE_LOCK.read_text(encoding="utf-8")
    ).lock_hash

    # 按固定 ID/version/checksum 重放：注册表定位 + 内容 checksum 对账
    registry = default_registry()
    for asset in lock.assets:
        manifest, directory = registry.get(asset.kind, asset.id, asset.version)
        assert manifest.version == asset.version
        assert manifest.checksum == asset.checksum
        assert directory.is_dir()
        registry.verify_checksum(asset.kind, asset.id, asset.version)


def test_resolver_output_matches_frozen_baseline() -> None:
    frozen = AssetLock.model_validate_json(FIXTURE.read_text(encoding="utf-8"))

    assert resolve_assets(default_registry(), "demo-ts-mp").lock_hash == frozen.lock_hash


def test_new_optional_fields_do_not_change_0_1_hash() -> None:
    lock = AssetLock.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    baseline = lock.lock_hash

    extended = lock.model_copy(update={
        "method_units": [
            LockedAsset(
                kind="method-unit", id="date-normalizer", version="1.0.0",
                checksum="a" * 64, path="methods/date-normalizer/1.0.0",
            )
        ],
        "index_revision": "idx-1",
        "selection_reason": "added after the fact",
    })

    # 0.1 锁哈希只取白名单字段：新增字段（含默认值）不影响旧锁哈希
    assert lock_hash_of(extended) == baseline


# --- 新版本锁 / 篡改被拒 ------------------------------------------------------

def test_new_version_lock_hashes_method_units_and_rejects_tamper() -> None:
    unit = LockedAsset(
        kind="method-unit", id="date-normalizer", version="1.0.0",
        checksum="a" * 64, path="methods/date-normalizer/1.0.0",
    )
    template = AssetLock.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    new_lock = AssetLock(
        schema_version="0.2", profile="demo-ts-mp",
        mysql_image=template.mysql_image, assets=template.assets, lock_hash="",
        method_units=[unit], index_schema_version="0.1", index_revision="idx-1",
        selection_reason="finder:exact",
    )
    signed = new_lock.model_copy(update={"lock_hash": lock_hash_of(new_lock)})
    verify_lock_hash(signed)

    tampered = signed.model_copy(update={
        "method_units": [unit.model_copy(update={"checksum": "b" * 64})]
    })
    with pytest.raises(AppException) as error:
        verify_lock_hash(tampered)
    assert error.value.code == "ASSET_CHECKSUM_MISMATCH"


def test_tampered_0_1_lock_is_rejected() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["assets"][0]["checksum"] = "0" * 64  # 篡改已锁定资产的 checksum
    lock = AssetLock.model_validate(data)

    with pytest.raises(AppException) as error:
        verify_lock_hash(lock)
    assert error.value.code == "ASSET_CHECKSUM_MISMATCH"


# --- Method Unit 契约 ---------------------------------------------------------

def test_minimal_method_unit_passes() -> None:
    registry = default_registry()
    manifest = MethodUnitManifest.model_validate(
        _method_unit(extension_points=["frontend/src/api/"])
    )

    assert manifest.layer == "util"
    assert manifest.signature

    require_valid_method_unit(
        manifest,
        known_profiles={"demo-ts-mp"},
        available_capabilities=known_capabilities(registry, "demo-ts-mp"),
        present_capabilities=set(),
        registered_extension_points=known_extension_points(registry, "demo-ts-mp"),
    )


def test_method_unit_missing_signature_or_provenance_is_rejected() -> None:
    no_signature = _method_unit()
    del no_signature["signature"]
    with pytest.raises(ValidationError):
        MethodUnitManifest.model_validate(no_signature)

    blank_provenance = _method_unit(source_provenance="   ")
    with pytest.raises(ValidationError):
        MethodUnitManifest.model_validate(blank_provenance)


def test_method_unit_dangerous_owned_paths_are_rejected() -> None:
    for bad in ("/etc/passwd", "../../secrets/", "C:/Windows/System32/"):
        with pytest.raises(ValidationError):
            MethodUnitManifest.model_validate(_method_unit(owned_paths=[bad]))


def test_method_unit_unknown_layer_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MethodUnitManifest.model_validate(_method_unit(layer="unknown-layer"))


def test_method_unit_illegal_dependency_is_rejected() -> None:
    manifest = MethodUnitManifest.model_validate(_method_unit(requires=["template:nonexistent"]))

    issues = method_unit_issues(
        manifest, known_profiles={"demo-ts-mp"}, available_capabilities={"auth"}
    )

    assert any("非法依赖" in issue for issue in issues)
    with pytest.raises(AppException) as error:
        require_valid_method_unit(
            manifest, known_profiles={"demo-ts-mp"}, available_capabilities={"auth"}
        )
    assert error.value.code == "ASSET_CONFLICT"


def test_method_unit_unknown_profile_is_rejected() -> None:
    manifest = MethodUnitManifest.model_validate(_method_unit(compatible_profiles=["unknown-stack"]))

    issues = method_unit_issues(
        manifest, known_profiles={"demo-ts-mp"}, available_capabilities=set()
    )

    assert any("未知 Profile" in issue for issue in issues)


def test_method_unit_conflict_is_rejected() -> None:
    manifest = MethodUnitManifest.model_validate(_method_unit(conflicts=["crud"]))

    issues = method_unit_issues(
        manifest, known_profiles={"demo-ts-mp"}, available_capabilities=set(),
        present_capabilities={"crud"},
    )

    assert any("冲突" in issue for issue in issues)


def test_method_unit_without_legal_provenance_is_rejected() -> None:
    manifest = MethodUnitManifest.model_validate(_method_unit(license="unknown"))

    issues = method_unit_issues(
        manifest, known_profiles={"demo-ts-mp"}, available_capabilities=set()
    )

    assert any("来源不明" in issue for issue in issues)


def test_method_unit_unknown_extension_point_is_rejected() -> None:
    manifest = MethodUnitManifest.model_validate(
        _method_unit(extension_points=["backend/secret#inject"])
    )

    issues = method_unit_issues(
        manifest, known_profiles={"demo-ts-mp"}, available_capabilities=set(),
        registered_extension_points={"frontend/src/api/"},
    )

    assert any("未注册的扩展点" in issue for issue in issues)


# --- Registry 读接口按 kind 分支 ---------------------------------------------

def test_registry_reads_method_unit_by_kind(tmp_path: Path) -> None:
    asset_dir = tmp_path / "date-normalizer" / "1.0.0"
    asset_dir.mkdir(parents=True)
    (asset_dir / "DateNormalizer.java").write_text("class DateNormalizer {}", encoding="utf-8")
    (asset_dir / "manifest.json").write_text(
        json.dumps(_method_unit(), ensure_ascii=False), encoding="utf-8"
    )
    registry = AssetRegistry([tmp_path])

    manifest, directory = registry.get("method-unit", "date-normalizer", "1.0.0")

    assert isinstance(manifest, MethodUnitManifest)
    assert manifest.symbol.endswith("normalize")
    assert directory == asset_dir


def test_default_registry_has_no_method_units() -> None:
    kinds = {manifest.kind for manifest in default_registry().all()}

    assert kinds <= {"template", "capability-pack"}  # 方法仓库在 R06B 建立
