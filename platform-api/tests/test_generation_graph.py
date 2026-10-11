"""M23 生成图集成测试（真实装配 + 注入假 Runner；不触网、不依赖 Docker）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import (
    GenerationContext,
    run_generation,
)
from agentforge_platform.models import Artifact, Project, Run, RunEvent, RunTask, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.runs import RunStatus, create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
SAMPLE_SPEC = REPO / "schemas" / "task-management.spec.json"

_PASS_VERIFY = {
    "overall": "PASS",
    "checks": [{"name": name, "status": "PASS"} for name in VERIFY_REQUIRED],
}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


class FakeRunner:
    def __init__(self, *, verify: dict | None = None, accept: dict | None = None) -> None:
        self._verify = verify if verify is not None else _PASS_VERIFY
        self._accept = accept if accept is not None else _PASS_ACCEPT

    def verify(self, artifact_dir: str) -> dict:
        return self._verify

    def accept(self, artifact_dir: str) -> dict:
        return self._accept


def _seed_confirmed_run(factory, *, confirmed: bool = True) -> int:
    spec = SoftwareSpecification.model_validate_json(SAMPLE_SPEC.read_text(encoding="utf-8"))
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        spec_version = SpecVersion(
            project_id=project.id,
            version=1,
            body_json=canonical_json(spec),
            hash=content_hash(spec),
            confirmed_at=datetime(2026, 1, 1) if confirmed else None,
        )
        session.add(spec_version)
        session.flush()
        project.active_spec_id = spec_version.id
        run = create_run(session, project, spec_version.id, idempotency_key="k1")
        return run.id


def _context(factory, tmp_path: Path, runner) -> GenerationContext:
    from agentforge_platform.asset_registry import default_registry

    return GenerationContext(
        session_factory=factory, runner=runner, work_root=tmp_path / "runs", registry=default_registry()
    )


def test_graph_reaches_success_for_confirmed_task_spec(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed_confirmed_run(factory)

    result = run_generation(_context(factory, tmp_path, FakeRunner()), run_id)

    assert result["ok"] is True
    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        assert run.status == "SUCCEEDED"
        tasks = {t.task_id for t in session.scalars(select(RunTask)).all()}
        assert {"load_confirmed_spec", "resolve_assets", "assemble", "verify"} <= tasks
        event_types = [e.type for e in session.scalars(select(RunEvent).order_by(RunEvent.sequence)).all()]
        assert "claimed" in event_types and "succeeded" in event_types


def test_graph_fails_when_verification_fails(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed_confirmed_run(factory)
    broken = {
        "overall": "FAIL",
        "checks": [{"name": "backend_build", "status": "FAIL", "error_code": "BUILD_FAILED"}],
    }

    result = run_generation(_context(factory, tmp_path, FakeRunner(verify=broken)), run_id)

    assert result["ok"] is False
    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        assert run.status == "FAILED" and run.error_code == "VERIFICATION_FAILED"
        types = [e.type for e in session.scalars(select(RunEvent)).all()]
        assert "failed" in types


def test_graph_classifies_infrastructure_failure(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed_confirmed_run(factory)
    infra = {
        "overall": "NOT_RUN",
        "checks": [{"name": "backend_build", "status": "NOT_RUN", "error_code": "INFRASTRUCTURE"}],
    }

    run_generation(_context(factory, tmp_path, FakeRunner(verify=infra)), run_id)

    with session_scope(factory) as session:
        assert session.get(Run, run_id).error_code == "INFRASTRUCTURE"


def test_graph_requires_confirmed_spec(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed_confirmed_run(factory, confirmed=False)

    result = run_generation(_context(factory, tmp_path, FakeRunner()), run_id)

    assert result["ok"] is False
    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        assert RunStatus(run.status) is RunStatus.FAILED
        assert run.error_code == "SPEC_NOT_FOUND"


def test_graph_never_succeeds_without_all_required_checks(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed_confirmed_run(factory)
    partial = {"overall": "PASS", "checks": [{"name": "static_manifest", "status": "PASS"}]}

    run_generation(_context(factory, tmp_path, FakeRunner(verify=partial)), run_id)

    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "FAILED"
        artifact = session.scalars(select(Artifact).where(Artifact.kind == "zip")).one()
        assert artifact.verified is False
    report = json.loads(
        (tmp_path / "runs" / f"run-{run_id}" / "verification.json").read_text(encoding="utf-8")
    )
    assert report["verified"] is False


def test_graph_success_packages_source_zip(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed_confirmed_run(factory)

    run_generation(_context(factory, tmp_path, FakeRunner()), run_id)

    run_dir = tmp_path / "runs" / f"run-{run_id}"
    assert (run_dir / "source.zip").is_file()
    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        assert run.status == "SUCCEEDED"
        assert any(
            a.path == "source.zip" and a.verified for a in session.scalars(select(Artifact)).all()
        )
        assert "packaged" in [e.type for e in session.scalars(select(RunEvent)).all()]


def test_graph_failure_packages_unverified_bundle(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed_confirmed_run(factory)
    broken = {"overall": "FAIL", "checks": [{"name": "backend_build", "status": "FAIL"}]}

    run_generation(_context(factory, tmp_path, FakeRunner(verify=broken)), run_id)

    verification = json.loads(
        (tmp_path / "runs" / f"run-{run_id}" / "verification.json").read_text(encoding="utf-8")
    )
    assert verification["verified"] is False and verification["label"] == "未验证"
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "FAILED"
