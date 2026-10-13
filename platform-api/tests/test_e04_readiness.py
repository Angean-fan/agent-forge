"""E04：代码交付状态与外部服务就绪分开（Mock/Live/缺配置）。

覆盖：5 种 external_readiness 组合；code_delivery_status 与第三方故障分离（基础设施单列 NOT_RUN）；
缺 Live 配置不使 Mock 生成/验收失败；已配置但未执行 Live 不标 LIVE_VERIFIED；运行详情 API 逐服务
呈现代码交付、Mock、Live、缺配置。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from agentforge_platform.asset_registry import default_registry
from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.models import Project, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.readiness import (
    CODE_FAIL,
    CODE_NOT_RUN,
    CODE_PASS,
    READINESS_AWAITING_CONFIG,
    READINESS_LIVE_FAILED,
    READINESS_LIVE_VERIFIED,
    READINESS_MOCK_ONLY,
    READINESS_NOT_REQUIRED,
    ExternalService,
    code_delivery_status,
    compute_readiness,
    declared_services,
    parse_declared_services,
    readiness_notice,
)
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
ALICE = {"username": "alice", "password": "alice-secret"}
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


def _service(**overrides) -> ExternalService:
    base = {
        "service_id": "svc", "name": "服务", "mode": "mock-only", "required": True,
        "required_for_live": True, "secret": True, "env_name": "SVC_KEY",
        "config_present": False,
    }
    base.update(overrides)
    return ExternalService(**base)


# --- 单元：就绪组合矩阵 --------------------------------------------------------

def test_no_services_is_not_required() -> None:
    assert compute_readiness([]) == READINESS_NOT_REQUIRED
    assert readiness_notice(READINESS_NOT_REQUIRED, []) is None


def test_mock_only_when_live_not_required_or_not_needed() -> None:
    not_live = _service(required_for_live=False)  # 不要求 Live
    assert compute_readiness([not_live]) == READINESS_MOCK_ONLY
    assert "仅 Mock" in (readiness_notice(READINESS_MOCK_ONLY, [not_live]) or "")

    configured = _service(config_present=True, live_status="NOT_RUN")
    assert compute_readiness([configured]) == READINESS_MOCK_ONLY  # 已配置但未跑 Live，不标 LIVE_VERIFIED


def test_awaiting_user_config_when_required_live_config_missing() -> None:
    missing = _service(required_for_live=True, config_present=False)
    assert compute_readiness([missing]) == READINESS_AWAITING_CONFIG
    assert "SVC_KEY" in (readiness_notice(READINESS_AWAITING_CONFIG, [missing]) or "")


def test_optional_or_non_live_service_missing_config_does_not_block() -> None:
    # 非交付必需（required=False）缺配置不影响"完整就绪"
    optional = _service(required=False, required_for_live=True, config_present=False)
    assert compute_readiness([optional]) == READINESS_MOCK_ONLY


def test_live_verified_requires_all_live_required_services_passed() -> None:
    passed = _service(config_present=True, live_status="PASS")
    assert compute_readiness([passed]) == READINESS_LIVE_VERIFIED

    one_pending = _service(service_id="a", config_present=True, live_status="PASS")
    other_pending = _service(service_id="b", config_present=True, live_status="NOT_RUN")
    assert compute_readiness([one_pending, other_pending]) == READINESS_MOCK_ONLY


def test_live_failed_has_priority_and_reports_reason() -> None:
    failed = _service(config_present=True, live_status="FAIL", reason="第三方鉴权失败")
    assert compute_readiness([failed]) == READINESS_LIVE_FAILED
    assert "Live 验证失败" in (readiness_notice(READINESS_LIVE_FAILED, [failed]) or "")


def test_code_delivery_status_separates_infrastructure_from_code() -> None:
    assert code_delivery_status("SUCCEEDED") == CODE_PASS
    assert code_delivery_status("FAILED", error_code="VERIFICATION_FAILED") == CODE_FAIL
    assert code_delivery_status("FAILED", error_code="INFRASTRUCTURE") == CODE_NOT_RUN  # 基础设施非代码失败
    assert code_delivery_status("GENERATING") == CODE_NOT_RUN


def test_parse_declared_services_reads_env_presence_without_values() -> None:
    entries = [{"requirement_id": "k", "env_name": "SVC_KEY", "required_in_modes": ["live"], "secret": True}]

    assert parse_declared_services(entries, env={})[0].config_present is False
    assert parse_declared_services(entries, env={"SVC_KEY": "x"})[0].config_present is True


# --- API：运行详情分列呈现 -----------------------------------------------------

class _Runner:
    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


def _spec_with(requirements: list[dict]) -> SoftwareSpecification:
    payload = json.loads(TASK.read_text(encoding="utf-8"))
    payload["external_requirements"] = requirements
    return SoftwareSpecification.model_validate(payload)


def _seed(factory, spec: SoftwareSpecification, *, key: str = "e04") -> tuple[int, int]:
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        row = SpecVersion(project_id=project.id, version=1, body_json=canonical_json(spec),
                          hash=content_hash(spec), confirmed_at=datetime(2026, 1, 1))
        session.add(row)
        session.flush()
        project.active_spec_id = row.id
        run = create_run(session, project, row.id, idempotency_key=key, spec_hash=content_hash(spec))
        session.flush()
        return project.id, run.id


def _complete(factory, tmp_path: Path, run_id: int) -> None:
    context = GenerationContext(session_factory=factory, runner=_Runner(),
                                work_root=tmp_path / "runs", registry=default_registry())
    run_generation(context, run_id)


def _auth(app) -> TestClient:
    client = TestClient(app)
    assert client.post("/api/auth/login", json=ALICE).status_code == 200
    return client


def _detail(app, run_id: int) -> dict:
    return _auth(app).get(f"/api/runs/{run_id}").json()["data"]


_LIVE_KEY = [{"requirement_id": "weather", "name": "天气 API Key", "env_name": "E04_WEATHER_KEY",
              "secret": True, "required": True, "required_in_modes": ["live"], "mode": "mock-only"}]


def test_detail_default_spec_is_not_required(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    _, run_id = _seed(factory, _spec_with([]))
    _complete(factory, tmp_path, run_id)

    data = _detail(app, run_id)
    assert data["status"] == "SUCCEEDED"
    assert data["code_delivery_status"] == "PASS"
    assert data["external_readiness"] == "NOT_REQUIRED"
    assert data["external_services"] == []


def test_detail_awaiting_config_then_mock_only(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    monkeypatch.delenv("E04_WEATHER_KEY", raising=False)  # 无真实密钥
    _, run_id = _seed(factory, _spec_with(_LIVE_KEY))
    _complete(factory, tmp_path, run_id)  # Mock 下仍能生成并验收

    data = _detail(app, run_id)
    assert data["status"] == "SUCCEEDED"  # 缺配置不使 Mock 构建失败
    assert data["code_delivery_status"] == "PASS"
    assert data["external_readiness"] == "AWAITING_USER_CONFIG"
    assert "E04_WEATHER_KEY" in (data["external_notice"] or "")
    service = data["external_services"][0]
    assert service["config_present"] is False and service["live_status"] == "NOT_RUN"

    # 配置存在但未执行 Live → 不标 LIVE_VERIFIED
    monkeypatch.setenv("E04_WEATHER_KEY", "present")
    assert _detail(app, run_id)["external_readiness"] == "MOCK_ONLY"


def test_detail_live_verified_and_failed_from_verification(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("E04_WEATHER_KEY", "present")
    _, run_id = _seed(factory, _spec_with(_LIVE_KEY))
    _complete(factory, tmp_path, run_id)

    report_path = tmp_path / "runs" / f"run-{run_id}" / "verification.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    report["external_services"] = [
        {"service_id": "weather", "mock_status": "PASS", "live_status": "PASS"}
    ]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    data = _detail(app, run_id)
    assert data["external_readiness"] == "LIVE_VERIFIED"
    assert data["code_delivery_status"] == "PASS"

    report["external_services"][0]["live_status"] = "FAIL"
    report["external_services"][0]["reason"] = "第三方鉴权失败"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    data = _detail(app, run_id)
    assert data["external_readiness"] == "LIVE_FAILED"
    assert data["code_delivery_status"] == "PASS"  # 代码交付结果单独保留
    assert data["external_services"][0]["reason"] == "第三方鉴权失败"


def test_declared_services_merges_verification_records() -> None:
    spec = _spec_with(_LIVE_KEY)
    services = declared_services(
        spec,
        {"external_services": [{"service_id": "weather", "live_status": "PASS", "mock_status": "PASS"}]},
        env={"E04_WEATHER_KEY": "x"},
    )

    assert services[0].service_id == "weather"
    assert services[0].live_status == "PASS" and services[0].config_present is True
