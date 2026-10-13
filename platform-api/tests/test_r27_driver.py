"""R27 真实评估驱动的路径隔离与证据提取。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from agentforge_platform.runner_client import HttpRunnerClient

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo_p0_eval.py"
SPEC = importlib.util.spec_from_file_location("demo_p0_eval", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


def test_scoped_runner_sends_unique_path_to_shared_root(monkeypatch, tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(HttpRunnerClient, "verify", lambda self, path: seen.append(("verify", path)) or {})
    monkeypatch.setattr(HttpRunnerClient, "accept", lambda self, path: seen.append(("accept", path)) or {})

    first = driver.ScopedRunnerClient(
        base_url="http://runner", token="test", scope="r27-batch/campus-1"
    )
    second = driver.ScopedRunnerClient(
        base_url="http://runner", token="test", scope="r27-batch/task-1"
    )
    first.verify("run-1")
    second.accept("run-1")

    assert seen == [
        ("verify", "r27-batch/campus-1/run-1"),
        ("accept", "r27-batch/task-1/run-1"),
    ]


def test_r27_uses_certified_offline_profile_without_mutating_sample_specs() -> None:
    spec = driver._r27_spec(driver.SPECS["campus"])
    assert spec.technical_profile.profile_version == "0.4"
    assert json.loads(driver.SPECS["campus"].read_text())["technical_profile"]["profile_version"] == "0.3"


def test_metrics_require_real_verification_and_preserve_evidence(
    migrated_sqlite_url: str, tmp_path: Path
) -> None:
    from agentforge_platform.db import make_engine, make_session_factory, session_scope
    from agentforge_platform.models import Project, Run, SpecVersion

    engine = make_engine(migrated_sqlite_url)
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        project = Project(owner_id=1, name="r27", requirement_text="eval")
        db.add(project)
        db.flush()
        spec = SpecVersion(project_id=project.id, version=1, body_json="{}", hash="h")
        db.add(spec)
        db.flush()
        run = Run(project_id=project.id, spec_id=spec.id, status="SUCCEEDED", idempotency_key="r27")
        db.add(run)
        db.flush()
        run_id = run.id
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "asset-search.json").write_text(json.dumps({
        "index_revision": "rev-a", "candidate_refs": ["method-unit:unit@1", "capability-pack:task@1"],
        "selected_refs": ["method-unit:unit@1", "capability-pack:task@1"],
        "filter_reasons": {"other@1": ["profile mismatch"], "capability-pack:task@1": []},
        "uncovered_acceptance_ids": ["AC99"],
        "reused_method_units": [{"id": "unit", "version": "1"}],
    }), encoding="utf-8")
    (run_dir / "asset-lock.json").write_text(json.dumps({
        "assets": [{"kind": "capability-pack", "id": "task", "version": "1"}]
    }), encoding="utf-8")
    (run_dir / "verification.json").write_text(json.dumps({
        "verified": True, "runtime": {"verify_overall": "PASS", "accept_overall": "PASS"},
        "checks": [{"name": "backend_test", "status": "PASS"}],
        "acceptance_cases": [{"name": "create_task", "status": "PASS"}],
    }), encoding="utf-8")
    (run_dir / "source.zip").write_bytes(b"zip")

    result = driver._metrics(factory, run_dir, run_id, "campus", 1)

    assert result.required_pass is True
    assert result.artifact_dir == str(run_dir)
    assert result.acceptance_cases == [{"name": "create_task", "status": "PASS"}]
    assert result.locked_method_units == ["unit@1"]
    assert result.locked_capability_packs == ["task@1"]
    assert result.uncovered_acceptance_ids == ["AC99"]
    assert result.rejected_reasons == {"other@1": ["profile mismatch"]}
    assert result.retrieval_candidates == 1
    assert result.compatible_selected == 1
    assert result.pack_retrieval_candidates == 1
    assert result.pack_compatible_selected == 1
    (run_dir / "verification.json").unlink()
    assert driver._metrics(factory, run_dir, run_id, "campus", 1).required_pass is False
    engine.dispose()


def test_live_matrix_uses_nine_independent_databases_and_runner_paths(
    monkeypatch, tmp_path: Path
) -> None:
    from agentforge_platform import generation_graph
    from agentforge_platform.evaluation import RunEvaluation

    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(shared))
    monkeypatch.setenv("RUNNER_TOKEN", "test-token")
    monkeypatch.setattr(driver, "DOC", tmp_path / "report.md")
    monkeypatch.setattr(driver, "_probe_runner_access", lambda *args: None)
    monkeypatch.setattr(driver, "_ensure_method_index", lambda: "rev-test")
    contexts: list[tuple[Path, str, int]] = []
    monkeypatch.setattr(
        generation_graph, "run_generation",
        lambda context, run_id: contexts.append(
            (context.work_root, context.runner._scope, run_id)
        ),
    )
    monkeypatch.setattr(
        driver, "_metrics",
        lambda factory, run_dir, run_id, case, index: RunEvaluation(
            case=case, index=index, run_id=run_id, status="SUCCEEDED",
            required_pass=True, artifact_dir=str(run_dir), index_revision="rev-test",
        ),
    )

    assert driver._live_report("http://runner") == 0
    assert len(contexts) == 9
    assert len({work_root for work_root, _, _ in contexts}) == 9
    assert len({scope for _, scope, _ in contexts}) == 9
    assert {run_id for _, _, run_id in contexts} == {1}
    assert all(work_root == shared / scope for work_root, scope, _ in contexts)
    assert "**9 / 9**" in driver.DOC.read_text(encoding="utf-8")


def test_missing_index_snapshot_cannot_pass_r27(monkeypatch, tmp_path: Path) -> None:
    from agentforge_platform import generation_graph
    from agentforge_platform.evaluation import RunEvaluation

    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(shared))
    monkeypatch.setenv("RUNNER_TOKEN", "test-token")
    monkeypatch.setattr(driver, "DOC", tmp_path / "report.md")
    monkeypatch.setattr(driver, "_probe_runner_access", lambda *args: None)
    monkeypatch.setattr(driver, "_ensure_method_index", lambda: "rev-test")
    monkeypatch.setattr(generation_graph, "run_generation", lambda *args: None)
    monkeypatch.setattr(driver, "_metrics", lambda factory, run_dir, run_id, case, index:
                        RunEvaluation(case=case, index=index, run_id=run_id,
                                      status="SUCCEEDED", required_pass=True))

    assert driver._live_report("http://runner") == 1
    report = driver.DOC.read_text(encoding="utf-8")
    assert "INDEX_SNAPSHOT_MISSING" in report
    assert "**0 / 9**" in report
