"""M27 运行详情、增量事件与产物下载 API 测试。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from agentforge_platform.db import session_scope
from agentforge_platform.models import ModelCall, Project, Run
from agentforge_platform.runs import append_event, record_artifact, record_task

ALICE_CREDS = {"username": "alice", "password": "alice-secret"}
BOB_CREDS = {"username": "bob", "password": "bob-secret"}


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


def _submit(client: TestClient, project_id: int, key: str) -> int:
    response = client.post(
        f"/api/projects/{project_id}/runs",
        json={},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf"), "Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return response.json()["data"]["id"]




def test_run_detail_reports_phase_tasks_cost_and_artifacts(platform_app) -> None:
    app, factory = platform_app
    client = _auth_client(app, ALICE_CREDS)
    run_id = _submit(client, _seed_project(factory), "k1")

    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        record_task(session, run, "load_confirmed_spec", status="DONE")
        record_task(session, run, "assemble", status="DONE")
        record_task(session, run, "verify", status="RUNNING")
        append_event(session, run, "stage", json.dumps({"stage": "assemble"}))
        record_artifact(session, run, "source.zip", "a" * 64, "zip", verified=True)
        session.add(
            ModelCall(
                call_id="c-settled", model_role="coding", provider="p", model="m",
                run_id=run_id, status="SETTLED", cost_micro=12345, reserved_micro=0,
            )
        )
        session.add(
            ModelCall(
                call_id="c-reserved", model_role="coding", provider="p", model="m",
                run_id=run_id, status="RESERVED", reserved_micro=678, cost_micro=None,
            )
        )

    data = client.get(f"/api/runs/{run_id}").json()["data"]

    assert data["status"] == "QUEUED"
    assert data["phase"] == "assemble"
    assert data["tasks"] == {"total": 3, "done": 2}
    assert data["cost"]["settled_micro"] == 12345
    assert data["cost"]["reserved_micro"] == 678
    assert data["cost"]["unknown_calls"] == 1
    assert [a["kind"] for a in data["artifacts"]] == ["zip"]
    assert data["artifacts"][0]["verified"] is True


def test_run_detail_exposes_verification_report_for_terminal_runs(
    platform_app, tmp_path, monkeypatch
) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path))
    client = _auth_client(app, ALICE_CREDS)
    run_id = _submit(client, _seed_project(factory), "k1")

    # 运行中不读报告
    assert client.get(f"/api/runs/{run_id}").json()["data"]["verification"] is None

    run_dir = tmp_path / f"run-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "verification.json").write_text(
        json.dumps(
            {
                "validation_scope": "MVP",
                "verified": False,
                "label": "未验证",
                "checks": [
                    {"name": "backend_build", "status": "FAIL"},
                    {"name": "frontend_build", "status": "NOT_RUN"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        run.status = "FAILED"
        run.error_code = "VERIFICATION_FAILED"

    data = client.get(f"/api/runs/{run_id}").json()["data"]

    assert data["status"] == "FAILED"
    assert data["error_code"] == "VERIFICATION_FAILED"
    assert data["verification"]["label"] == "未验证"
    assert [c["status"] for c in data["verification"]["checks"]] == ["FAIL", "NOT_RUN"]


def test_events_are_incremental_by_sequence(platform_app) -> None:
    app, factory = platform_app
    client = _auth_client(app, ALICE_CREDS)
    run_id = _submit(client, _seed_project(factory), "k1")

    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        append_event(session, run, "stage", json.dumps({"stage": "load_confirmed_spec"}))
        append_event(session, run, "succeeded", "{}")

    first = client.get(f"/api/runs/{run_id}/events", params={"after_sequence": 0}).json()["data"]
    assert [e["sequence"] for e in first["items"]] == [1, 2, 3]
    assert first["items"][0]["type"] == "queued"
    assert first["items"][1]["payload"] == {"stage": "load_confirmed_spec"}
    assert first["latest_sequence"] == 3

    second = client.get(f"/api/runs/{run_id}/events", params={"after_sequence": 1}).json()["data"]
    assert [e["sequence"] for e in second["items"]] == [2, 3]

    caught_up = client.get(f"/api/runs/{run_id}/events", params={"after_sequence": 3}).json()["data"]
    assert caught_up["items"] == []
    assert caught_up["latest_sequence"] == 3


def test_events_and_detail_enforce_ownership(platform_app) -> None:
    app, factory = platform_app
    alice = _auth_client(app, ALICE_CREDS)
    bob = _auth_client(app, BOB_CREDS)
    run_id = _submit(alice, _seed_project(factory), "k1")

    assert alice.get(f"/api/runs/{run_id}/events").status_code == 200
    assert bob.get(f"/api/runs/{run_id}/events").status_code == 403
    assert alice.get("/api/runs/999999/events").status_code == 404
    assert bob.get(f"/api/runs/{run_id}/artifact").status_code == 403


def test_artifact_download_serves_recorded_zip(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path))
    alice = _auth_client(app, ALICE_CREDS)
    bob = _auth_client(app, BOB_CREDS)
    run_id = _submit(alice, _seed_project(factory), "k1")

    run_dir = tmp_path / f"run-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = b"PK\x03\x04 fake-zip-bytes"
    (run_dir / "source.zip").write_bytes(payload)
    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        record_artifact(session, run, "source.zip", "b" * 64, "zip", verified=True)

    response = alice.get(f"/api/runs/{run_id}/artifact")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["x-artifact-verified"] == "true"
    assert response.content == payload
    assert bob.get(f"/api/runs/{run_id}/artifact").status_code == 403


def test_artifact_missing_is_not_found(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path))
    client = _auth_client(app, ALICE_CREDS)
    run_id = _submit(client, _seed_project(factory), "k1")

    response = client.get(f"/api/runs/{run_id}/artifact")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"


def test_artifact_path_traversal_is_rejected(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path))
    client = _auth_client(app, ALICE_CREDS)
    run_id = _submit(client, _seed_project(factory), "k1")

    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        record_artifact(session, run, "../../../../etc/passwd", "c" * 64, "zip")

    response = client.get(f"/api/runs/{run_id}/artifact")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"


def test_artifact_download_streams_verified_flag_for_unverified_failure_package(
    platform_app, tmp_path, monkeypatch
) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path))
    client = _auth_client(app, ALICE_CREDS)
    run_id = _submit(client, _seed_project(factory), "k1")

    run_dir = tmp_path / f"run-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "source.zip").write_bytes(b"failed-package")
    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        record_artifact(session, run, "source.zip", "d" * 64, "zip", verified=False)

    response = client.get(f"/api/runs/{run_id}/artifact")

    assert response.status_code == 200
    assert response.headers["x-artifact-verified"] == "false"
    assert Path(run_dir / "source.zip").is_file()
