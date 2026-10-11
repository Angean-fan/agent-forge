"""M28 运行产物保留/配额与维护任务测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from agentforge_platform.db import session_scope
from agentforge_platform.models import Project, Run, Worker
from agentforge_platform.retention import (
    cleanup_runs,
    storage_status,
    usage_bytes,
)
from agentforge_platform.runs import worker_status

ALICE_CREDS = {"username": "alice", "password": "alice-secret"}


def _naive_utc(days_ago: int) -> datetime:
    return (datetime.now(UTC) - timedelta(days=days_ago)).replace(tzinfo=None)


def _seed_project(factory, *, owner_id: int = 1, active_spec_id: int | None = 1) -> int:
    with session_scope(factory) as session:
        project = Project(owner_id=owner_id, name="p", requirement_text="x", active_spec_id=active_spec_id)
        session.add(project)
        session.flush()
        return project.id


def _seed_run(factory, project_id: int, *, status: str, age_days: int) -> int:
    with session_scope(factory) as session:
        run = Run(
            project_id=project_id,
            spec_id=1,
            status=status,
            updated_at=_naive_utc(age_days),
        )
        session.add(run)
        session.flush()
        return run.id


def _make_run_dir(root: Path, run_id: int, size: int) -> Path:
    run_dir = root / f"run-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "source.zip").write_bytes(b"x" * size)
    return run_dir


def test_cleanup_removes_only_terminal_runs_past_retention(platform_app, tmp_path, monkeypatch) -> None:
    _app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path))
    project_id = _seed_project(factory)

    old_success = _seed_run(factory, project_id, status="SUCCEEDED", age_days=30)
    old_failed = _seed_run(factory, project_id, status="FAILED", age_days=30)
    recent_success = _seed_run(factory, project_id, status="SUCCEEDED", age_days=1)
    old_running = _seed_run(factory, project_id, status="GENERATING", age_days=30)
    for run_id in (old_success, old_failed, recent_success, old_running):
        _make_run_dir(tmp_path, run_id, 10)

    with session_scope(factory) as session:
        result = cleanup_runs(session, retention_days=14)

    assert sorted(result["removed_run_ids"]) == sorted([old_success, old_failed])
    assert not (tmp_path / f"run-{old_success}").exists()
    assert not (tmp_path / f"run-{old_failed}").exists()
    # 进行中与保留期内的产物必须保留
    assert (tmp_path / f"run-{recent_success}").exists()
    assert (tmp_path / f"run-{old_running}").exists()


def test_cleanup_dry_run_keeps_everything(platform_app, tmp_path, monkeypatch) -> None:
    _app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path))
    project_id = _seed_project(factory)
    run_id = _seed_run(factory, project_id, status="SUCCEEDED", age_days=30)
    _make_run_dir(tmp_path, run_id, 10)

    with session_scope(factory) as session:
        result = cleanup_runs(session, retention_days=14, dry_run=True)

    assert result["removed_run_ids"] == [run_id]
    assert (tmp_path / f"run-{run_id}").exists()  # 未实际删除
    assert result["freed_bytes"] == 10


def test_storage_status_counts_usage(platform_app, tmp_path, monkeypatch) -> None:
    _app, _factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTFORGE_RUN_QUOTA_BYTES", "100")
    monkeypatch.setenv("AGENTFORGE_MIN_FREE_BYTES", "0")
    _make_run_dir(tmp_path, 1, 40)
    _make_run_dir(tmp_path, 2, 70)

    assert usage_bytes() == 110
    status = storage_status()
    assert status["used_bytes"] == 110 and status["over_quota"] is True


def test_start_run_rejected_when_over_quota(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTFORGE_RUN_QUOTA_BYTES", "10")
    monkeypatch.setenv("AGENTFORGE_MIN_FREE_BYTES", "0")
    _make_run_dir(tmp_path, 1, 100)  # 已超配额

    client = TestClient(app)
    assert client.post("/api/auth/login", json=ALICE_CREDS).status_code == 200
    project_id = _seed_project(factory)

    response = client.post(
        f"/api/projects/{project_id}/runs",
        json={},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf"), "Idempotency-Key": "k-quota"},
    )

    assert response.status_code == 507
    assert response.json()["error"]["code"] == "STORAGE_QUOTA_EXCEEDED"


def test_start_run_rejected_when_low_free_space(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTFORGE_RUN_QUOTA_BYTES", "0")  # 不限总量
    monkeypatch.setenv("AGENTFORGE_MIN_FREE_BYTES", str(10**18))  # 阈值高到必然不足

    client = TestClient(app)
    assert client.post("/api/auth/login", json=ALICE_CREDS).status_code == 200
    project_id = _seed_project(factory)

    response = client.post(
        f"/api/projects/{project_id}/runs",
        json={},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf"), "Idempotency-Key": "k-free"},
    )

    assert response.status_code == 507
    assert response.json()["error"]["code"] == "STORAGE_QUOTA_EXCEEDED"


def test_worker_status_flags_stalled_heartbeat(platform_app) -> None:
    _app, factory = platform_app
    with session_scope(factory) as session:
        session.add(Worker(worker_id="fresh", hostname="h1", last_heartbeat=_naive_utc(0)))
        session.add(Worker(worker_id="stalled", hostname="h2", last_heartbeat=_naive_utc(1)))

    with session_scope(factory) as session:
        status = worker_status(session)

    assert status["unhealthy"] == ["stalled"]
    healthy = {w["worker_id"]: w["healthy"] for w in status["workers"]}
    assert healthy == {"fresh": True, "stalled": False}


def test_start_run_allowed_when_under_quota(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTFORGE_RUN_QUOTA_BYTES", "1000000")
    monkeypatch.setenv("AGENTFORGE_MIN_FREE_BYTES", "0")

    client = TestClient(app)
    assert client.post("/api/auth/login", json=ALICE_CREDS).status_code == 200
    project_id = _seed_project(factory)

    response = client.post(
        f"/api/projects/{project_id}/runs",
        json={},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf"), "Idempotency-Key": "k-ok"},
    )

    assert response.status_code == 202
