"""E03：External Requirement 结构化声明与配置指南生成。

覆盖：从 pack manifest 确定性生成 `.env.example` 与 `EXTERNAL_SERVICES.md`（申请什么—在哪里申请—
填写哪里—如何验证）；env_name↔config_path 1:1；同名变量冲突拒绝、语义一致去重；秘密字段留空且
秘密扫描通过；secret/required 独立、live-only 秘密可在 mock-only 清单声明；用户说明默认确定性、
可选 DeepSeek 只接收脱敏事实。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentforge_platform.asset_registry import IntegrationPackManifest, integration_registry
from agentforge_platform.errors import AppException
from agentforge_platform.external_requirements import (
    build_docs,
    render_user_explanation,
    require_clean_docs,
    scan_for_secrets,
)

REPO = Path(__file__).resolve().parents[2]
ENV_SAMPLE = REPO / "schemas" / "env.example.weather-lookup.sample"
MD_SAMPLE = REPO / "schemas" / "external-services.weather-lookup.sample.md"


def _item(**overrides) -> dict:
    item = {
        "env_name": "SERVICE_BASE_URL",
        "required": True,
        "secret": False,
        "kind": "base_url",
        "required_in_modes": ["live"],
        "name": "服务基站地址",
        "config_path": "service.base-url",
        "target_file": ".env.example",
        "description": "示例。",
        "acquisition_url": "https://console.example.invalid/app",
        "acquisition_steps": ["注册", "创建应用"],
        "validation_rule": "以 https:// 开头",
        "docs_verified_at": "2026-10-13",
    }
    item.update(overrides)
    return item


def _manifest(**overrides) -> IntegrationPackManifest:
    manifest = {
        "id": "sample-service",
        "version": "1.0.0",
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
        "config": [_item()],
        "internal_types": ["SampleView"],
        "contract_tests": ["tests/SamplePortContractTest.java"],
        "checks": ["mvn test"],
        "checksum": "",
        "license": "internal",
        "source_provenance": "test:e03",
    }
    manifest.update(overrides)
    return IntegrationPackManifest.model_validate(manifest)


def _weather() -> IntegrationPackManifest:
    manifest, _ = integration_registry().get("integration-pack", "weather-lookup", "0.3.0")
    return manifest


# --- 成功：生成指南 ------------------------------------------------------------

def test_published_pack_generates_clean_guide() -> None:
    docs = require_clean_docs(build_docs([_weather()]))

    assert {requirement.env_name for requirement in docs.requirements} == {
        "WEATHER_APP_ID", "WEATHER_API_BASE_URL", "WEATHER_API_KEY"
    }
    assert docs.binding_issues == []
    assert docs.secret_findings == []
    # 申请什么—在哪里申请—填写哪里—如何验证
    for heading in ("申请什么", "在哪里申请", "填写哪里", "如何验证"):
        assert heading in docs.external_services_md
    assert "https://console.example.invalid/weather/keys" in docs.external_services_md


def test_secret_blank_and_nonsecret_placeholder() -> None:
    docs = build_docs([_weather()])
    lines = {line.split("=", 1)[0]: line.split("=", 1)[1]
             for line in docs.env_example.splitlines()
             if "=" in line and not line.startswith("#")}

    assert lines["WEATHER_API_KEY"] == ""          # 秘密留空
    assert lines["WEATHER_API_BASE_URL"].startswith("https://example.invalid")
    assert lines["WEATHER_APP_ID"] == "example-app-id"  # AppID 非秘密，占位而非真实值
    for value in lines.values():                    # 绝不含真实值：占位串均为示例
        assert "example" in value or value in {"127.0.0.1"} or value == "CHANGE_ME" or value == ""


def test_committed_samples_match_generated() -> None:
    docs = build_docs([_weather()])

    assert docs.env_example == ENV_SAMPLE.read_text(encoding="utf-8")
    assert docs.external_services_md == MD_SAMPLE.read_text(encoding="utf-8")


def test_env_binding_is_one_to_one() -> None:
    docs = build_docs([_weather()])

    assert docs.binding_issues == []
    paths = [requirement.config_path for requirement in docs.requirements]
    assert len(paths) == len(set(paths))
    assert "" not in paths


def test_identical_requirement_is_deduped() -> None:
    first = _manifest(config=[_item()])
    second = _manifest(id="other-service", config=[_item()])  # 语义一致

    docs = build_docs([first, second])

    assert len(docs.requirements) == 1


def test_user_explanation_is_deterministic_and_facts_only() -> None:
    docs = build_docs([_weather()])
    assert render_user_explanation(docs) == render_user_explanation(docs)

    captured: dict[str, str] = {}

    def explain(payload: str) -> str:
        captured["payload"] = payload
        return "通俗说明：请先申请密钥。"

    text = render_user_explanation(docs, explain=explain)
    assert "通俗说明" in text
    facts = json.loads(captured["payload"])          # 只传脱敏结构化事实
    assert "variables" in facts and "services" in facts
    assert captured["payload"].count("example.invalid") == 0  # 不含 URL 等细节
    assert "secret" in captured["payload"]


# --- 失败：冲突与扫描 ----------------------------------------------------------

def test_same_env_name_conflict_is_rejected() -> None:
    first = _manifest(config=[_item()])
    second = _manifest(id="other-service", config=[_item(secret=True)])  # 同名但语义不同

    with pytest.raises(AppException) as error:
        build_docs([first, second])
    assert error.value.code == "ASSET_CONFLICT"
    assert "同名环境变量语义冲突" in error.value.message


def test_duplicate_config_path_is_rejected() -> None:
    from agentforge_platform.external_requirements import binding_issues, collect_requirements

    manifest = _manifest(config=[
        _item(env_name="A_VALUE", config_path="service.value"),
        _item(env_name="B_VALUE", config_path="service.value"),
    ])
    issues = binding_issues(collect_requirements([manifest]))

    assert any("配置键重复" in issue for issue in issues)


def test_secret_scan_detects_nonempty_secret_and_blocks() -> None:
    from agentforge_platform.external_requirements import (
        ExternalDocs,
        generate_external_services_md,
    )

    docs = build_docs([_weather()])
    leaked = ExternalDocs(
        requirements=docs.requirements,
        binding_issues=[],
        env_example=docs.env_example + "WEATHER_API_KEY=real-secret-value\n",
        external_services_md=generate_external_services_md(docs.requirements),
        secret_findings=scan_for_secrets(docs.env_example + "WEATHER_API_KEY=real-secret-value\n"),
    )
    assert docs.secret_findings == []
    assert any("秘密变量 WEATHER_API_KEY" in finding for finding in leaked.secret_findings)
    with pytest.raises(AppException) as error:
        require_clean_docs(leaked)
    assert error.value.code == "ASSET_CONFLICT"


def test_secret_scan_detects_pem_and_jwt() -> None:
    text = "-----BEGIN RSA PRIVATE KEY-----\nMII...\n"
    assert any("PEM" in finding for finding in scan_for_secrets(text))
    jwt = "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"
    assert any("JWT" in finding for finding in scan_for_secrets(jwt))


# --- 声明约束（parse 阶段） ----------------------------------------------------

def test_secret_and_required_are_independent() -> None:
    # 秘密但非必需
    _manifest(config=[_item(secret=True, required=False, required_in_modes=["live"])])
    # 必需但非秘密
    _manifest(config=[_item(secret=False, required=True, required_in_modes=["live"])])


def test_live_only_secret_allowed_in_mock_only_but_mock_secret_rejected() -> None:
    _manifest(config=[_item(secret=True, required=True, required_in_modes=["live"])])  # 允许
    with pytest.raises(ValidationError):
        _manifest(config=[_item(secret=True, required=True, required_in_modes=["mock"])])  # 拒绝


def test_bad_acquisition_url_is_rejected() -> None:
    for bad in ("ftp://host/x", "https://user:pass@host/x", "not-a-url"):
        with pytest.raises(ValidationError):
            _manifest(config=[_item(acquisition_url=bad)])


def test_bad_config_path_and_env_name_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _manifest(config=[_item(config_path="bad path!")])
    with pytest.raises(ValidationError):
        _manifest(config=[_item(env_name="lowercase")])
