"""M20 运行 API + 独立 worker 测试。"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from agentforge_platform.db import session_scope
from agentforge_platform.models import Project, Run, Worker
from agentforge_platform.runs import RunStatus, _now, claim_run, transition
from agentforge_platform.worker import run_worker

ALICE_CREDS = {"username": "alice", "password": "alice-secret"}
BOB_CREDS = {"username": "bob", "password": "bob-secret"}


def _failing_handler(factory):
    """测试用处理函数：领取后如实判失败（默认处理已改为 M23 生成图）。"""

    def handler(run_id: int) -> None:
        with session_scope(factory) as session:
            run = session.get(Run, run_id)
            transition(session, run, RunStatus.PLANNING)
            transition(session, run, RunStatus.FAILED, error_code="WORKER_TEST")

    return handler


def _seed_project(factory, *, owner_id: int = 1, active_spec_id: int | None = 1) -> int:
    with session_scope(factory) as session:
        project = Project(
            owner_id=owner_id, name="p", requirement_text="x", active_spec_id=active_spec_id
        )
        session.add(project)
        session.flush()
        return project.id


def _auth_client(app, creds: dict[str, str]) -> TestClient:
    client = TestClient(app)
    assert client.post("/api/auth/login", json=creds).status_code == 200
    return client


def _submit(client: TestClient, project_id: int, key: str, body: dict | None = None):
    return client.post(
        f"/api/projects/{project_id}/runs",
        json=body if body is not None else {},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf"), "Idempotency-Key": key},
    )


def test_start_run_requires_confirmed_spec(platform_app) -> None:
    app, factory = platform_app
    project_id = _seed_project(factory, active_spec_id=None)
    client = _auth_client(app, ALICE_CREDS)

    response = _submit(client, project_id, "k1")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SPEC_NOT_READY"


def test_start_run_creates_queued_run(platform_app) -> None:
    app, factory = platform_app
    project_id = _seed_project(factory, active_spec_id=7)
    client = _auth_client(app, ALICE_CREDS)

    response = _submit(client, project_id, "k1")

    assert response.status_code == 202
    data = response.json()["data"]
    assert data["status"] == "QUEUED"
    assert data["spec_id"] == 7


def test_idempotency_key_returns_same_run(platform_app) -> None:
    app, factory = platform_app
    project_id = _seed_project(factory)
    client = _auth_client(app, ALICE_CREDS)

    first = _submit(client, project_id, "same-key").json()["data"]
    second = _submit(client, project_id, "same-key").json()["data"]

    assert first["id"] == second["id"]


def test_missing_idempotency_key_is_rejected(platform_app) -> None:
    app, factory = platform_app
    project_id = _seed_project(factory)
    client = _auth_client(app, ALICE_CREDS)

    response = client.post(
        f"/api/projects/{project_id}/runs",
        json={},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    )

    assert response.status_code == 400


def test_spec_id_mismatch_is_rejected(platform_app) -> None:
    app, factory = platform_app
    project_id = _seed_project(factory, active_spec_id=7)
    client = _auth_client(app, ALICE_CREDS)

    response = _submit(client, project_id, "k1", {"spec_id": 999})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SPEC_CONFLICT"


def test_start_run_requires_auth_and_csrf(platform_app) -> None:
    app, factory = platform_app
    project_id = _seed_project(factory)

    assert TestClient(app).post(f"/api/projects/{project_id}/runs").status_code == 401

    client = _auth_client(app, ALICE_CREDS)
    response = client.post(f"/api/projects/{project_id}/runs", json={})
    assert response.status_code == 403  # 缺 CSRF


def test_get_run_and_cross_user(platform_app) -> None:
    app, factory = platform_app
    project_id = _seed_project(factory)
    alice = _auth_client(app, ALICE_CREDS)
    bob = _auth_client(app, BOB_CREDS)
    run_id = _submit(alice, project_id, "k1").json()["data"]["id"]

    assert alice.get(f"/api/runs/{run_id}").status_code == 200
    assert bob.get(f"/api/runs/{run_id}").status_code == 403
    assert alice.get("/api/runs/999999").status_code == 404


def test_claim_run_reserves_lease(platform_app) -> None:
    app, factory = platform_app
    project_id = _seed_project(factory)
    client = _auth_client(app, ALICE_CREDS)
    _submit(client, project_id, "k1")

    with session_scope(factory) as session:
        claimed = claim_run(session, "w1", lease_seconds=60)
        assert claimed is not None
        claimed_id = claimed.id
        assert claimed.lease_owner == "w1"

    # 已被租约占用 → 不再领取
    with session_scope(factory) as session:
        assert claim_run(session, "w2", lease_seconds=60) is None

    # 租约过期后可再领取
    with session_scope(factory) as session:
        run = session.get(Run, claimed_id)
        run.lease_until = _now() - timedelta(seconds=1)
    with session_scope(factory) as session:
        reclaimed = claim_run(session, "w2", lease_seconds=60)
        assert reclaimed is not None and reclaimed.id == claimed_id


def test_worker_claims_run_and_reports_heartbeat(platform_app) -> None:
    app, factory = platform_app
    project_id = _seed_project(factory)
    client = _auth_client(app, ALICE_CREDS)
    run_id = _submit(client, project_id, "k1").json()["data"]["id"]

    # worker 以独立循环处理（不依赖 API 进程）
    run_worker(
        session_factory=factory,
        worker_id="w-test",
        iterations=1,
        poll_interval=0.0,
        handler=_failing_handler(factory),
    )

    run = client.get(f"/api/runs/{run_id}").json()["data"]
    assert run["status"] == "FAILED"
    assert run["error_code"] == "WORKER_TEST"

    workers = client.get("/api/workers").json()["data"]["items"]
    assert any(w["worker_id"] == "w-test" and w["healthy"] for w in workers)


def test_stalled_worker_is_unhealthy(platform_app) -> None:
    app, factory = platform_app
    client = _auth_client(app, ALICE_CREDS)

    with session_scope(factory) as session:
        session.add(
            Worker(
                worker_id="stale",
                hostname="h",
                last_heartbeat=_now() - timedelta(seconds=600),
            )
        )

    workers = client.get("/api/workers").json()["data"]["items"]
    stale = next(w for w in workers if w["worker_id"] == "stale")
    assert stale["healthy"] is False


def test_worker_processes_persisted_run_without_api(platform_app) -> None:
    # 关闭 API 进程不影响已持久化任务：worker 直接据同一 DB 领取处理
    app, factory = platform_app
    project_id = _seed_project(factory)
    client = _auth_client(app, ALICE_CREDS)
    _submit(client, project_id, "k1")

    run_worker(
        session_factory=factory,
        worker_id="w2",
        iterations=1,
        poll_interval=0.0,
        handler=_failing_handler(factory),
    )

    with session_scope(factory) as session:
        run = session.query(Run).one()
        assert RunStatus(run.status) is RunStatus.FAILED
