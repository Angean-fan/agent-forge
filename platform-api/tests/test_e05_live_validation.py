"""E05：有限 Live 沙箱验证的安全前置与脱敏结果。

覆盖：未配置/未声明允许域名端口/副作用未批准 → **阻止** Live 启动（LIVE_NOT_READY）；最小注入计划
只列变量名不含值；鉴权/网络/超时/供应商/业务分类；结果脱敏不泄漏凭据；Live 记录并入 E04 就绪。
"""

from __future__ import annotations

import json
import types

import pytest

from agentforge_platform.errors import AppException
from agentforge_platform.live_validation import (
    OUTCOME_AUTH,
    OUTCOME_BUSINESS,
    OUTCOME_NETWORK,
    OUTCOME_OK,
    OUTCOME_PROVIDER,
    OUTCOME_TIMEOUT,
    LiveConfigVar,
    LiveService,
    build_live_record,
    classify_live_outcome,
    injection_plan,
    live_preflight,
    merge_live_records,
    parse_live_services,
    redact_secrets,
    require_live_preflight,
)
from agentforge_platform.readiness import (
    READINESS_LIVE_FAILED,
    READINESS_LIVE_VERIFIED,
    compute_readiness,
    declared_services,
)

_LIVE_ENTRY = {
    "requirement_id": "weather",
    "name": "天气服务",
    "provider": "weather-service-demo",
    "mode": "live",
    "live_capable": True,
    "required_in_modes": ["live"],
    "allowed_hosts": ["api.weather.example.invalid"],
    "allowed_ports": [443],
    "config": [
        {"env_name": "WEATHER_APP_ID", "secret": False},
        {"env_name": "WEATHER_API_KEY", "secret": True},
    ],
}
_ENV = {"WEATHER_APP_ID": "app-123", "WEATHER_API_KEY": "deadbeef"}


def _service(**overrides) -> LiveService:
    base = {
        "service_id": "weather",
        "name": "天气服务",
        "provider": "weather-service-demo",
        "live_capable": True,
        "required_for_live": True,
        "config": (LiveConfigVar("WEATHER_API_KEY", secret=True),),
        "allowed_hosts": ("api.weather.example.invalid",),
        "allowed_ports": (443,),
    }
    base.update(overrides)
    return LiveService(**base)


# --- 前置：阻止 Live 启动 ------------------------------------------------------

def test_preflight_blocks_when_config_missing() -> None:
    preflight = live_preflight(parse_live_services([_LIVE_ENTRY]), env={})

    assert preflight.allowed is False
    assert any("缺少必要配置：WEATHER_API_KEY" in issue for issue in preflight.issues)
    with pytest.raises(AppException) as error:
        require_live_preflight(parse_live_services([_LIVE_ENTRY]), env={})
    assert error.value.code == "LIVE_NOT_READY"


def test_preflight_blocks_mock_only_integration() -> None:
    preflight = live_preflight([_service(live_capable=False)], env=_ENV)

    assert preflight.allowed is False
    assert any("未声明支持 Live" in issue for issue in preflight.issues)


def test_preflight_blocks_missing_or_wildcard_allowlist() -> None:
    no_host = live_preflight([_service(allowed_hosts=())], env=_ENV)
    assert any("未声明允许域名" in issue for issue in no_host.issues)

    wildcard = live_preflight([_service(allowed_hosts=("*.example.invalid",))], env=_ENV)
    assert any("不允许通配" in issue for issue in wildcard.issues)

    no_port = live_preflight([_service(allowed_ports=())], env=_ENV)
    assert any("未声明允许端口" in issue for issue in no_port.issues)


def test_preflight_blocks_side_effects_without_approval() -> None:
    unapproved = _service(side_effects=("charge",), requires_approval=True, approved=False)
    preflight = live_preflight([unapproved], env=_ENV)
    assert preflight.allowed is False
    assert any("真实副作用需人工批准：charge" in issue for issue in preflight.issues)

    approved = _service(side_effects=("charge",), requires_approval=True, approved=True)
    assert live_preflight([approved], env=_ENV).allowed is True


def test_preflight_allows_when_fully_configured() -> None:
    preflight = live_preflight(parse_live_services([_LIVE_ENTRY]), env=_ENV)

    assert preflight.allowed is True and preflight.issues == []


# --- 注入计划：只有名字，没有值 ------------------------------------------------

def test_injection_plan_lists_only_names() -> None:
    plan = injection_plan(parse_live_services([_LIVE_ENTRY]))

    assert plan.env_names == ("WEATHER_APP_ID", "WEATHER_API_KEY")
    assert plan.secret_env_names == ("WEATHER_API_KEY",)
    serialized = json.dumps(plan.__dict__)  # 计划里绝不出现任何值
    assert "app-123" not in serialized and "deadbeef" not in serialized


# --- 失败分类 -----------------------------------------------------------------

def test_classify_live_outcome_matrix() -> None:
    assert classify_live_outcome(status_code=200) == OUTCOME_OK
    assert classify_live_outcome(status_code=401) == OUTCOME_AUTH
    assert classify_live_outcome(status_code=403) == OUTCOME_AUTH
    assert classify_live_outcome(status_code=500) == OUTCOME_PROVIDER
    assert classify_live_outcome(status_code=422) == OUTCOME_BUSINESS
    assert classify_live_outcome(error="Connection refused") == OUTCOME_NETWORK
    assert classify_live_outcome(error="DNS resolution failed") == OUTCOME_NETWORK
    assert classify_live_outcome(error="request timed out") == OUTCOME_TIMEOUT


# --- 结果脱敏 ----------------------------------------------------------------

def test_build_live_record_redacts_secrets() -> None:
    service = parse_live_services([_LIVE_ENTRY])[0]
    evidence = "POST /forecast Authorization: Bearer deadbeef; WEATHER_API_KEY=deadbeef"

    record = build_live_record(
        service, OUTCOME_AUTH, environment="operator-sandbox", package_version="0.4.0",
        duration_ms=210, evidence=evidence, secrets=["deadbeef"],
    )

    assert record["live_status"] == "FAIL" and record["outcome"] == "AUTH"
    assert "deadbeef" not in json.dumps(record, ensure_ascii=False)  # 凭据不泄漏
    assert "***" in record["evidence"]


def test_redact_patterns_cover_pem_jwt_aws() -> None:
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\nMII...\n token=eyJabcdefghij.eyJklmnopqrst.sig "
        "aws=AKIAABCDEFGHIJKLMNOP"
    )
    redacted = redact_secrets(text)

    assert "AKIAABCDEFGHIJKLMNOP" not in redacted
    assert "eyJabcdefghij" not in redacted


# --- 并入 E04 就绪 -------------------------------------------------------------

def test_live_records_feed_readiness() -> None:
    spec = types.SimpleNamespace(external_requirements=[_LIVE_ENTRY])
    service = parse_live_services([_LIVE_ENTRY])[0]

    passed = build_live_record(
        service, OUTCOME_OK, environment="sandbox", package_version="0.4.0", duration_ms=90
    )
    report = merge_live_records(None, [passed])
    services = declared_services(spec, report, env=_ENV)
    assert compute_readiness(services) == READINESS_LIVE_VERIFIED

    failed = build_live_record(
        service, OUTCOME_NETWORK, environment="sandbox", package_version="0.4.0", duration_ms=5000
    )
    report = merge_live_records(report, [failed])
    services = declared_services(spec, report, env=_ENV)
    assert compute_readiness(services) == READINESS_LIVE_FAILED
    assert "网络不通" in services[0].reason


def test_parse_groups_by_service_and_defaults_live_capable_false() -> None:
    services = parse_live_services([{"requirement_id": "x", "env_name": "Y_KEY", "secret": True}])

    assert len(services) == 1
    assert services[0].live_capable is False  # 未声明即不擅自开 Live
