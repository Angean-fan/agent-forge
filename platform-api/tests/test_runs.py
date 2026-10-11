"""M19 运行元数据与状态机测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException
from agentforge_platform.middleware import get_request_id
from agentforge_platform.models import Project, Run
from agentforge_platform.responses import ApiResponse
from agentforge_platform.runs import (
    RunStatus,
    append_event,
    create_run,
    record_artifact,
    record_task,
    require_run_owner,
    run_directory,
    transition,
)

ALICE_CREDS = {"username": "alice", "password": "alice-secret"}
BOB_CREDS = {"username": "bob", "password": "bob-secret"}


def _seed_run(factory, *, owner_id: int = 1, spec_id: int = 11) -> int:
    with session_scope(factory) as session:
        project = Project(owner_id=owner_id, name="p")
        session.add(project)
        session.flush()
        run = create_run(session, project, spec_id)
        return run.id


def test_create_run_starts_queued(platform_app) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory)

    with session_scope(factory) as session:
        run = session.get(Run, run_id)

    assert run.status == "QUEUED"
    assert run.spec_id == 11
    assert run.error_code is None


def test_valid_transition_chain_to_succeeded(platform_app) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory)

    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        for status in (
            RunStatus.PLANNING,
            RunStatus.GENERATING,
            RunStatus.VERIFYING,
            RunStatus.SUCCEEDED,
        ):
            transition(session, run, status)

    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"


def test_illegal_transitions_are_rejected(platform_app) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory)

    with pytest.raises(AppException) as error, session_scope(factory) as session:
        transition(session, session.get(Run, run_id), RunStatus.SUCCEEDED)
    assert error.value.code == "RUN_INVALID_TRANSITION"

    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        transition(session, run, RunStatus.PLANNING)
        transition(session, run, RunStatus.FAILED, error_code="BUILD_FAILED")

    with pytest.raises(AppException), session_scope(factory) as session:
        transition(session, session.get(Run, run_id), RunStatus.PLANNING)


def test_failed_records_error_code(platform_app) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory)

    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        transition(session, run, RunStatus.PLANNING)
        transition(session, run, RunStatus.FAILED, error_code="INFRASTRUCTURE")

    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        assert run.status == "FAILED"
        assert run.error_code == "INFRASTRUCTURE"


def test_events_are_monotonic_per_run(platform_app) -> None:
    _, factory = platform_app
    first = _seed_run(factory)
    second = _seed_run(factory)

    with session_scope(factory) as session:
        run_a = session.get(Run, first)
        run_b = session.get(Run, second)
        sequences_a = [append_event(session, run_a, "stage", "{}").sequence for _ in range(3)]
        sequences_b = [append_event(session, run_b, "stage", "{}").sequence for _ in range(2)]

    assert sequences_a == [1, 2, 3]
    assert sequences_b == [1, 2]


def test_task_id_unique_within_run(platform_app) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory)

    with pytest.raises(IntegrityError), session_scope(factory) as session:
        run = session.get(Run, run_id)
        record_task(session, run, "backend_build")
        record_task(session, run, "backend_build")


def test_artifact_records_sha256(platform_app) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory)

    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        record_artifact(session, run, "asset-lock.json", "a" * 64, "lock")

    with session_scope(factory) as session:
        from agentforge_platform.models import Artifact

        artifact = session.query(Artifact).one()
        assert artifact.sha256 == "a" * 64 and artifact.verified is False


def test_run_directory_is_isolated_per_run(tmp_path: Path) -> None:
    first = run_directory(tmp_path, 1)
    second = run_directory(tmp_path, 2)

    assert first == (tmp_path / "run-1") and second == (tmp_path / "run-2")
    assert first.is_dir() and first != second


def test_run_ownership_is_enforced(platform_app) -> None:
    app, factory = platform_app

    @app.get("/api/runs/{run_id}")
    def _get_run(request: Request, run: Run = Depends(require_run_owner)) -> ApiResponse[dict]:
        return ApiResponse(
            request_id=get_request_id(request), data={"id": run.id, "status": run.status}
        )

    run_id = _seed_run(factory, owner_id=1)

    alice = TestClient(app)
    assert alice.post("/api/auth/login", json=ALICE_CREDS).status_code == 200
    bob = TestClient(app)
    assert bob.post("/api/auth/login", json=BOB_CREDS).status_code == 200
    anonymous = TestClient(app)

    assert anonymous.get(f"/api/runs/{run_id}").status_code == 401
    assert alice.get(f"/api/runs/{run_id}").status_code == 200
    assert bob.get(f"/api/runs/{run_id}").status_code == 403
    assert alice.get("/api/runs/999999").status_code == 404
