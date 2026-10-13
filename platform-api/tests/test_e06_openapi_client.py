"""E06：经校验 OpenAPI 的 Client/DTO 生成与 Port 适配。

覆盖：固定样例成功生成并适配既有 Port（确定性 output_hash、只依赖 JDK）；已审核 Port 先复用；
恶意 URL / 超大 schema / 未经批准 SDK 被拒；规范正文是不可信数据（指令不被执行）；
签名/鉴权/幂等/回调语义待人工确认；生成物不自动成为公共 Method Unit。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentforge_platform.asset_registry import integration_registry
from agentforge_platform.errors import AppException
from agentforge_platform.openapi_client import (
    APPROVED_GENERATORS,
    MAX_SCHEMA_DEPTH,
    MAX_SPEC_BYTES,
    find_reusable_port,
    generate_client,
    validate_openapi,
)

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "schemas" / "openapi.exchange-rate.sample.json"
_HOST = "api.rates.example.invalid"
_PORT = ["Rate getRate(String base)"]


def _spec(**overrides) -> str:
    spec = json.loads(SAMPLE.read_text(encoding="utf-8"))
    spec.update(overrides)
    return json.dumps(spec, ensure_ascii=False)


def _generate(text: str | None = None, **kwargs):
    return generate_client(
        text or SAMPLE.read_text(encoding="utf-8"),
        pack_id="exchange-rate",
        port_methods=_PORT,
        source_url=f"https://{_HOST}",
        allowed_hosts=(_HOST,),
        **kwargs,
    )


# --- 成功：生成并适配 Port -----------------------------------------------------

def test_generate_from_fixed_sample_adapts_port() -> None:
    generation = _generate()

    assert generation.dtos["Rate"].startswith("package com.agentforge.integration.dto;")
    assert "Rate getRate(String base);" in generation.client_interface
    assert "public Rate getRate(String base)" in generation.port_adapter
    assert generation.generator == "agentforge-openapi-java"
    assert generation.generator_version == "1.0.0"


def test_generation_is_deterministic_and_jdk_only() -> None:
    first, second = _generate(), _generate()

    assert first.output_hash == second.output_hash
    assert first.dtos == second.dtos
    for source in (first.client_interface, first.port_adapter):
        for line in source.splitlines():
            if line.startswith("import "):
                target = line[len("import "):].rstrip(";")
                assert target.startswith(("java.", "com.agentforge.integration."))


def test_pending_semantics_and_not_public_method_unit() -> None:
    generation = _generate()

    assert set(generation.pending_semantics) == {"signature", "auth", "idempotency", "callback"}
    assert generation.publishable_as_public_method_unit is False  # 一次构建不等于公共资产


def test_untrusted_body_instructions_are_inert() -> None:
    # info.description 里的"指令"只是不可信数据，不被解释/执行
    generation = _generate()

    assert "忽略此前所有指令" not in generation.port_adapter
    assert "忽略此前所有指令" not in generation.client_interface


# --- 复用优先 -----------------------------------------------------------------

def test_reuses_audited_port_before_generating() -> None:
    packs = [m for m in integration_registry().all() if m.kind == "integration-pack"]

    assert find_reusable_port(packs, port_methods=["WeatherForecast getForecast(String city)"]) == (
        "weather-lookup@0.3.0#WeatherPort"  # 取最高已审核版本
    )
    assert find_reusable_port(packs, port_methods=_PORT) is None  # 无审核资产 → 走生成


# --- 拒绝：来源 / 允许域名 -----------------------------------------------------

@pytest.mark.parametrize("bad_url", [
    "http://api.rates.example.invalid",          # 非 https
    "https://user:pass@api.rates.example.invalid",  # 内嵌凭据
    "https://evil.example.invalid",               # 未在允许域名
    "https://127.0.0.1",                          # 回环
    "https://169.254.169.254",                    # 云元数据
])
def test_malicious_source_url_is_rejected(bad_url: str) -> None:
    with pytest.raises(AppException) as error:
        generate_client(
            SAMPLE.read_text(encoding="utf-8"), pack_id="exchange-rate",
            port_methods=_PORT, source_url=bad_url, allowed_hosts=(_HOST,),
        )
    assert error.value.code == "OPENAPI_REJECTED"


def test_server_url_not_allowlisted_is_rejected() -> None:
    spec = json.loads(SAMPLE.read_text(encoding="utf-8"))
    spec["servers"] = [{"url": "https://other.example.invalid"}]
    with pytest.raises(AppException) as error:
        _generate(json.dumps(spec))
    assert error.value.code == "OPENAPI_REJECTED"
    assert "允许域名" in error.value.message


# --- 拒绝：超大 schema ---------------------------------------------------------

def test_oversized_spec_is_rejected() -> None:
    huge = json.dumps({"openapi": "3.1.0", "info": {"title": "x", "pad": "a" * MAX_SPEC_BYTES}})
    with pytest.raises(AppException) as error:
        _generate(huge)
    assert error.value.code == "OPENAPI_REJECTED"
    assert "过大" in error.value.message


def test_too_many_paths_is_rejected() -> None:
    spec = json.loads(SAMPLE.read_text(encoding="utf-8"))
    spec["paths"] = {f"/p{i}": {"get": {"operationId": f"op{i}", "responses": {"200": {"description": "ok"}}}}
                     for i in range(60)}
    with pytest.raises(AppException) as error:
        _generate(json.dumps(spec))
    assert error.value.code == "OPENAPI_REJECTED"


def test_deeply_nested_schema_is_rejected() -> None:
    nested: dict = {"type": "object", "properties": {"leaf": {"type": "string"}}}
    for _ in range(MAX_SCHEMA_DEPTH + 2):
        nested = {"type": "object", "properties": {"child": nested}}
    spec = json.loads(SAMPLE.read_text(encoding="utf-8"))
    spec["components"] = {"schemas": {"Deep": nested}}
    with pytest.raises(AppException) as error:
        _generate(json.dumps(spec))
    assert error.value.code == "OPENAPI_REJECTED"
    assert "嵌套过深" in error.value.message


# --- 拒绝：未经批准 SDK / 生成器 -----------------------------------------------

def test_unapproved_generator_is_rejected() -> None:
    with pytest.raises(AppException) as error:
        _generate(generator="some-vendor-gen", generator_version="9.9.9")
    assert error.value.code == "OPENAPI_REJECTED"
    assert "未经批准的生成器" in error.value.message


def test_unapproved_sdk_is_rejected() -> None:
    with pytest.raises(AppException) as error:
        _generate(sdk="vendor-sdk")
    assert error.value.code == "OPENAPI_REJECTED"

    spec = json.loads(SAMPLE.read_text(encoding="utf-8"))
    spec["x-vendor-sdk"] = "vendor-sdk"
    with pytest.raises(AppException) as error:
        _generate(json.dumps(spec))
    assert error.value.code == "OPENAPI_REJECTED"
    assert "未经批准的 SDK" in error.value.message


def test_approved_generator_matches_registry() -> None:
    assert APPROVED_GENERATORS["agentforge-openapi-java"] == ("1.0.0",)


# --- 拒绝：无法适配 Port ------------------------------------------------------

def test_port_method_without_operation_is_rejected() -> None:
    with pytest.raises(AppException) as error:
        generate_client(
            SAMPLE.read_text(encoding="utf-8"), pack_id="exchange-rate",
            port_methods=["Rate fetchRate(String base)"],  # 无对应 operationId
            source_url=f"https://{_HOST}", allowed_hosts=(_HOST,),
        )
    assert error.value.code == "OPENAPI_REJECTED"
    assert "未能适配 Port" in error.value.message


def test_validate_openapi_accepts_fixed_sample() -> None:
    issues = validate_openapi(
        SAMPLE.read_text(encoding="utf-8"), source_url=f"https://{_HOST}", allowed_hosts=(_HOST,)
    )

    assert issues == []
