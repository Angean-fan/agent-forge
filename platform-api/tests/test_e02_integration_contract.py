"""E02：Integration Pack 的 Port/Adapter/Mock 契约链路校验。

覆盖：已发布 Mock-only 包通过契约校验并覆盖 success/failure/timeout；Port 只引用内部类型；
Adapter 绑定/协议实现、Mock 唯一性与场景覆盖、契约测试存在与覆盖均被确定性校验；
契约不通过会阻断 Resolver 锁定；旧版（无契约声明）仍可重放；live/SDK/越界路径在 parse 阶段即拒。
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
    integration_registry,
)
from agentforge_platform.errors import AppException
from agentforge_platform.integration_contract import (
    integration_contract_issues,
    port_contract_cases,
    require_valid_integration_contract,
)
from agentforge_platform.integration_lock import resolve_integration_lock
from agentforge_platform.resolver import verify_lock_hash

REPO = Path(__file__).resolve().parents[2]
CONTRACT_TEST_REL = "tests/SamplePortContractTest.java"
_GOOD_CONTRACT_TEST = (
    "class SamplePortContractTest { /* WeatherPort success failure timeout */ "
    "SamplePort port; }"
)


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
        "owned_paths": ["src/main/java/com/agentforge/integration/"],
        "target_dir": "backend",
        "ports": [{"name": "SamplePort", "methods": ["SampleView fetch(String key)"]}],
        "adapters": [
            {"name": "MockSampleAdapter", "kind": "mock", "implements": "SamplePort"},
            {"name": "HttpSampleAdapter", "kind": "stub", "implements": "SamplePort"},
        ],
        "mock": {"deterministic": True, "scenarios": ["success", "failure", "timeout"]},
        "config": [],
        "internal_types": ["SampleView"],
        "contract_tests": [CONTRACT_TEST_REL],
        "checks": ["mvn test"],
        "checksum": "",
        "license": "internal",
        "source_provenance": "test:e02",
    }
    manifest.update(overrides)
    return manifest


def _manifest(**overrides) -> IntegrationPackManifest:
    return IntegrationPackManifest.model_validate(_pack(**overrides))


def _write_asset(directory: Path, **fields) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": "asset", "version": "0.1.0", "kind": "capability-pack",
        "compatible_profiles": ["p"], "requires": [], "provides": [], "conflicts": [],
        "parameter_schema": {}, "extension_points": [], "owned_paths": [], "checks": [],
        "checksum": "", "license": "internal",
    }
    manifest.update(fields)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if manifest["kind"] == "template":
        (directory / "compose.yaml").write_text(
            "services:\n  mysql:\n    image: mysql:8.0.43@sha256:" + "0" * 64 + "\n",
            encoding="utf-8",
        )


def _write_pack(root: Path, asset_id: str = "sample-pack", *, with_contract_test: bool = True,
                test_text: str = _GOOD_CONTRACT_TEST, **overrides) -> Path:
    directory = root / asset_id / "0.1.0"
    directory.mkdir(parents=True, exist_ok=True)
    manifest = _pack(id=asset_id, **overrides)
    manifest["checksum"] = compute_checksum(directory)
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    if with_contract_test and manifest["contract_tests"]:
        path = directory / manifest["contract_tests"][0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(test_text, encoding="utf-8")
    return directory


# --- 成功：已发布包通过契约校验 ------------------------------------------------

def test_published_pack_passes_contract() -> None:
    registry = integration_registry()
    manifest, directory = registry.get("integration-pack", "weather-lookup", "0.2.0")

    assert integration_contract_issues(manifest, pack_dir=directory) == []
    require_valid_integration_contract(manifest, pack_dir=directory)


def test_port_contract_cases_cover_success_failure_timeout() -> None:
    manifest, _ = integration_registry().get("integration-pack", "weather-lookup", "0.2.0")

    cases = port_contract_cases(manifest)
    assert {case.scenario for case in cases} == {"success", "failure", "timeout"}
    assert {case.port for case in cases} == {"WeatherPort"}
    assert port_contract_cases(manifest) == cases  # 确定性


def test_resolver_locks_contract_pack_and_reports_old_version_still_replays() -> None:
    registry = integration_registry()
    published = [manifest.version for manifest in registry.all() if manifest.id == "weather-lookup"]
    newest = max(published, key=lambda version: tuple(int(p) for p in version.split(".")))

    latest = resolve_integration_lock(registry, "demo-ts-mp", ["weather-lookup"])
    verify_lock_hash(latest)
    assert [unit.version for unit in latest.integration_packs] == [newest]

    old = resolve_integration_lock(registry, "demo-ts-mp", ["weather-lookup@0.1.0"])
    verify_lock_hash(old)
    assert [unit.version for unit in old.integration_packs] == ["0.1.0"]


def test_manifest_declares_adapters_bound_to_port(tmp_path: Path) -> None:
    manifest, _ = integration_registry().get("integration-pack", "weather-lookup", "0.2.0")
    kinds = {adapter.kind for adapter in manifest.adapters if adapter.implements == "WeatherPort"}

    assert kinds == {"mock", "stub"}
    assert manifest.internal_types == ["WeatherForecast"]
    assert manifest.contract_tests


# --- 失败：契约链路理由 --------------------------------------------------------

def test_port_without_adapter_is_rejected() -> None:
    # 有 Adapter 但没有任何一个实现 SamplePort（此处另有一个未绑定 Port 的 Adapter）
    issues = integration_contract_issues(
        _manifest(adapters=[{"name": "OrphanAdapter", "kind": "stub", "implements": ""}])
    )

    assert any("缺少实现 Adapter" in issue for issue in issues)


def test_port_with_only_mock_is_rejected() -> None:
    issues = integration_contract_issues(_manifest(adapters=[
        {"name": "MockSampleAdapter", "kind": "mock", "implements": "SamplePort"}
    ]))

    assert any("缺少协议 Adapter" in issue for issue in issues)


def test_duplicate_mock_for_port_is_rejected() -> None:
    issues = integration_contract_issues(_manifest(adapters=[
        {"name": "MockA", "kind": "mock", "implements": "SamplePort"},
        {"name": "MockB", "kind": "mock", "implements": "SamplePort"},
        {"name": "HttpSampleAdapter", "kind": "stub", "implements": "SamplePort"},
    ]))

    assert any("Mock 不唯一" in issue for issue in issues)


def test_adapter_bound_to_undeclared_port_is_rejected() -> None:
    issues = integration_contract_issues(_manifest(adapters=[
        {"name": "MockSampleAdapter", "kind": "mock", "implements": "GhostPort"},
        {"name": "HttpSampleAdapter", "kind": "stub", "implements": "SamplePort"},
    ]))

    assert any("未声明的 Port" in issue for issue in issues)


def test_missing_mock_scenario_is_rejected() -> None:
    issues = integration_contract_issues(
        _manifest(mock={"deterministic": True, "scenarios": ["success", "failure"]})
    )

    assert any("Mock 缺少场景" in issue and "timeout" in issue for issue in issues)


def test_vendor_type_in_port_signature_is_rejected() -> None:
    issues = integration_contract_issues(_manifest(
        ports=[{"name": "SamplePort", "methods": ["VendorWeatherDto fetch(String key)"]}],
        internal_types=["SampleView"],
    ))

    assert any("非内部类型" in issue and "VendorWeatherDto" in issue for issue in issues)


def test_missing_contract_test_is_rejected() -> None:
    assert any("契约测试" in issue for issue in integration_contract_issues(_manifest(contract_tests=[])))


def test_contract_test_file_absence_is_rejected(tmp_path: Path) -> None:
    issues = integration_contract_issues(_manifest(), pack_dir=tmp_path)

    assert any("契约测试文件不存在" in issue for issue in issues)


def test_contract_test_not_covering_scenario_is_rejected(tmp_path: Path) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    issues = integration_contract_issues(
        _manifest(), pack_dir=pack_dir
    )  # 文件缺失时先报缺失
    assert any("契约测试文件不存在" in issue for issue in issues)

    (pack_dir / CONTRACT_TEST_REL).parent.mkdir(parents=True)
    (pack_dir / CONTRACT_TEST_REL).write_text("class T { SamplePort p; } /* success */", encoding="utf-8")
    issues = integration_contract_issues(_manifest(), pack_dir=pack_dir)
    assert any("未覆盖场景" in issue and "failure" in issue for issue in issues)
    assert any("未覆盖场景" in issue and "timeout" in issue for issue in issues)


# --- 失败：契约不通过会阻断 Resolver 锁定 --------------------------------------

def test_resolver_rejects_pack_with_invalid_contract(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    _write_asset(root / "tpl", kind="template", id="tpl")
    _write_pack(
        root, compatible_profiles=["p"],
        mock={"deterministic": True, "scenarios": ["success", "timeout"]},  # 缺 failure
    )

    with pytest.raises(AppException) as error:
        resolve_integration_lock(AssetRegistry([root]), "p", ["sample-pack"])
    assert error.value.code == "ASSET_CONFLICT"
    assert "Mock 缺少场景" in error.value.message


def test_pack_without_contract_declaration_is_not_contract_checked(tmp_path: Path) -> None:
    # 无 contract_tests 的旧式清单不做契约校验（0.1.0 语义），仍可锁定
    root = tmp_path / "assets"
    _write_asset(root / "tpl", kind="template", id="tpl")
    _write_pack(
        root, compatible_profiles=["p"], contract_tests=[],
        adapters=[{"name": "SoloMock", "kind": "mock", "implements": "GhostPort"}],
        with_contract_test=False,
    )

    lock = resolve_integration_lock(AssetRegistry([root]), "p", ["sample-pack"])
    assert [unit.id for unit in lock.integration_packs] == ["sample-pack"]


# --- 失败：parse 阶段的结构约束 ------------------------------------------------

def test_live_or_sdk_adapter_kind_is_rejected() -> None:
    for kind in ("live", "http", "sdk"):
        with pytest.raises(ValidationError):
            _manifest(adapters=[{"name": "A", "kind": kind, "implements": "SamplePort"}])


def test_unsafe_contract_test_path_is_rejected() -> None:
    for bad in ("../evil.java", "/etc/passwd", "a/../../b.java"):
        with pytest.raises(ValidationError):
            _manifest(contract_tests=[bad])


def test_unsafe_internal_type_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _manifest(internal_types=["Not A Type"])
