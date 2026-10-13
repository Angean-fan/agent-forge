"""E08：P1 在线预览的生命周期、授权与隔离。

覆盖：仅已通过全部代码必需验收的产物可开启；随机 token 授权（非授权/过期/已停止被拒）；
默认 15 分钟到期自动回收；停止预览**不删除源码与 ZIP**；预览只用演示数据、不含秘密；
归属校验（跨用户 403、未登录 401）；授权网关对未知/过期/停止 token 一律 404。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentforge_platform.asset_registry import default_registry
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.live_validation import LiveConfigVar
from agentforge_platform.models import Project, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.preview import (
    ACCESS_EXPIRED,
    ACCESS_OK,
    ACCESS_REJECTED,
    PREVIEW_DEFAULT_TTL_SECONDS,
    PREVIEW_STATUS_ACTIVE,
    PREVIEW_STATUS_EXPIRED,
    PREVIEW_STATUS_STOPPED,
    build_preview_env,
    create_preview,
    is_expired,
    isolation_issues,
    preview_access,
    preview_eligibility,
    recycle,
    secret_env_names,
)
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
ALICE = {"username": "alice", "password": "alice-secret"}
BOB = {"username": "bob", "password": "bob-secret"}
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


class _Service:
    """最小服务存根（含 config，供隔离校验）。"""

    def __init__(self, config: tuple[LiveConfigVar, ...]) -> None:
        self.config = config


class _Runner:
    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


# --- 单元：资格 / 授权 / 回收 / 隔离 ------------------------------------------

def test_eligibility_requires_success_and_pass() -> None:
    assert preview_eligibility("SUCCEEDED", "PASS") == []
    assert preview_eligibility("QUEUED", "NOT_RUN")
    assert preview_eligibility("FAILED", "FAIL")
    with pytest.raises(AppException) as error:
        create_preview(1, run_status="FAILED", code_delivery_status="FAIL")
    assert error.value.code == "PREVIEW_INELIGIBLE"


def test_create_sets_token_ttl_and_demo() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    session = create_preview(7, run_status="SUCCEEDED", code_delivery_status="PASS",
                             now=now, token_factory=lambda: "tok-abc")

    assert session.token == "tok-abc"
    assert session.status == PREVIEW_STATUS_ACTIVE
    assert session.demo_data is True and session.runtime_attached is False
    assert datetime.fromisoformat(session.expires_at) - now == timedelta(
        seconds=PREVIEW_DEFAULT_TTL_SECONDS
    )


def test_access_decision_matrix() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    session = create_preview(7, run_status="SUCCEEDED", code_delivery_status="PASS",
                             now=now, token_factory=lambda: "secret-token")

    assert preview_access(session, "secret-token", now=now) == ACCESS_OK
    assert preview_access(session, "wrong-token", now=now) == ACCESS_REJECTED
    later = now + timedelta(seconds=PREVIEW_DEFAULT_TTL_SECONDS + 1)
    assert is_expired(session, now=later) is True
    assert preview_access(session, "secret-token", now=later) == ACCESS_EXPIRED
    assert preview_access(recycle(session, stopped=True), "secret-token", now=now) == ACCESS_REJECTED


def test_recycle_preserves_session_identity() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    session = create_preview(7, run_status="SUCCEEDED", code_delivery_status="PASS",
                             now=now, token_factory=lambda: "tok")

    expired = recycle(session)
    assert expired.status == PREVIEW_STATUS_EXPIRED
    assert (expired.token, expired.created_at, expired.run_id) == (session.token, session.created_at, 7)


def test_preview_env_never_contains_secrets() -> None:
    services = [_Service((LiveConfigVar("WEATHER_APP_ID", secret=False),
                          LiveConfigVar("WEATHER_API_KEY", secret=True)))]

    env = build_preview_env(services)

    assert env == {"WEATHER_APP_ID": "preview-demo"}  # 秘密不入预览
    assert secret_env_names(services) == ["WEATHER_API_KEY"]
    assert isolation_issues(env, secret_env_names(services)) == []
    assert isolation_issues({"WEATHER_API_KEY": "x"}, secret_env_names(services)) != []


# --- API：归属 / 资格 / 网关 / 停止 -------------------------------------------

def _seed(factory, *, key: str = "e08") -> tuple[int, int]:
    spec = SoftwareSpecification.model_validate_json(TASK.read_text(encoding="utf-8"))
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


def _auth(app, creds) -> TestClient:
    client = TestClient(app)
    assert client.post("/api/auth/login", json=creds).status_code == 200
    return client


def _csrf(client: TestClient) -> str:
    return client.cookies.get("af_csrf")


def test_preview_requires_owner_and_eligibility(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    _, run_id = _seed(factory)

    alice = _auth(app, ALICE)
    rejected = alice.post(f"/api/runs/{run_id}/preview", headers={"X-CSRF-Token": _csrf(alice)})
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "PREVIEW_INELIGIBLE"  # 未完成 → 拒绝

    assert TestClient(app).post(f"/api/runs/{run_id}/preview").status_code == 401  # 未登录

    _complete(factory, tmp_path, run_id)

    bob = _auth(app, BOB)  # 跨用户
    assert bob.post(f"/api/runs/{run_id}/preview", headers={"X-CSRF-Token": _csrf(bob)}).status_code == 403

    created = alice.post(f"/api/runs/{run_id}/preview", headers={"X-CSRF-Token": _csrf(alice)})
    assert created.status_code == 200
    data = created.json()["data"]
    assert data["status"] == PREVIEW_STATUS_ACTIVE and data["demo_data"] is True
    assert data["url"].startswith("/api/preview/") and data["runtime_attached"] is False


def test_gateway_authorizes_by_random_token(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    _, run_id = _seed(factory)
    _complete(factory, tmp_path, run_id)
    alice = _auth(app, ALICE)
    alice.post(f"/api/runs/{run_id}/preview", headers={"X-CSRF-Token": _csrf(alice)})

    token = json.loads((tmp_path / "runs" / f"run-{run_id}" / "preview.json").read_text())["token"]
    gateway = TestClient(app)
    assert gateway.get(f"/api/preview/{token}").status_code == 200
    assert gateway.get("/api/preview/wrong-token").status_code == 404  # 非授权拒绝

    alice.delete(f"/api/runs/{run_id}/preview", headers={"X-CSRF-Token": _csrf(alice)})
    assert gateway.get(f"/api/preview/{token}").status_code == 404  # 停止后拒绝


def test_stop_preview_keeps_zip_downloadable(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    _, run_id = _seed(factory)
    _complete(factory, tmp_path, run_id)
    alice = _auth(app, ALICE)
    alice.post(f"/api/runs/{run_id}/preview", headers={"X-CSRF-Token": _csrf(alice)})

    stopped = alice.delete(f"/api/runs/{run_id}/preview", headers={"X-CSRF-Token": _csrf(alice)})
    assert stopped.json()["data"]["status"] == PREVIEW_STATUS_STOPPED
    assert alice.get(f"/api/runs/{run_id}/artifact").status_code == 200  # 源码 ZIP 仍可下载


def test_expiry_is_reported_and_recycled(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    _, run_id = _seed(factory)
    _complete(factory, tmp_path, run_id)
    alice = _auth(app, ALICE)
    alice.post(f"/api/runs/{run_id}/preview", headers={"X-CSRF-Token": _csrf(alice)})

    # 模拟 15 分钟到期
    path = tmp_path / "runs" / f"run-{run_id}" / "preview.json"
    payload = json.loads(path.read_text())
    payload["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(payload))
    token = payload["token"]

    status = alice.get(f"/api/runs/{run_id}/preview").json()["data"]
    assert status["status"] == PREVIEW_STATUS_EXPIRED  # 读时自动回收
    assert TestClient(app).get(f"/api/preview/{token}").status_code == 404
