"""R24：预算暂停与经确认的恢复。

覆盖：低预算**真实暂停**（PAUSED_BUDGET，非失败）；未增加预算的恢复被拒；经确认加预算后仅**重新入队续跑**
（不重复已提交任务）；恢复前核对状态不明调用；归属/CSRF 保护；预算调整可追溯。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from agentforge_platform.asset_registry import default_registry
from agentforge_platform.budget import effective_budget_micro
from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import GenerationContext, build_graph, run_generation
from agentforge_platform.models import (
    Artifact,
    BudgetAdjustment,
    ModelCall,
    Project,
    Run,
    RunTask,
    SpecVersion,
)
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.runs import RunStatus, create_run, transition
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
ALICE = {"username": "alice", "password": "alice-secret"}
BOB = {"username": "bob", "password": "bob-secret"}
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


class _Runner:
    def __init__(self, *, verify=None, accept=None) -> None:
        self._verify = verify or (lambda _d: _PASS_VERIFY)
        self._accept = accept or (lambda _d: _PASS_ACCEPT)

    def verify(self, artifact_dir: str) -> dict:
        return self._verify(artifact_dir)

    def accept(self, artifact_dir: str) -> dict:
        return self._accept(artifact_dir)


def _boom(_artifact_dir: str):
    raise RuntimeError("crash")


def _seed(factory, *, owner_id: int = 1, status: RunStatus = RunStatus.QUEUED) -> tuple[int, int]:
    spec = SoftwareSpecification.model_validate_json(TASK.read_text(encoding="utf-8"))
    with session_scope(factory) as session:
        project = Project(owner_id=owner_id, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        row = SpecVersion(project_id=project.id, version=1, body_json=canonical_json(spec),
                          hash=content_hash(spec), confirmed_at=datetime(2026, 1, 1))
        session.add(row)
        session.flush()
        project.active_spec_id = row.id
        run = create_run(session, project, row.id, idempotency_key="k1",
                         spec_hash=content_hash(spec))
        for step in (RunStatus.PLANNING, RunStatus.GENERATING, RunStatus.VERIFYING):
            if _ORDER[step] <= _ORDER[status]:
                transition(session, run, step)
        session.flush()
        return project.id, run.id


_ORDER = {RunStatus.QUEUED: 0, RunStatus.PLANNING: 1, RunStatus.GENERATING: 2, RunStatus.VERIFYING: 3}


def _context(factory, tmp_path: Path, runner) -> GenerationContext:
    return GenerationContext(session_factory=factory, runner=runner, work_root=tmp_path / "runs",
                             registry=default_registry())


def _auth(app, creds) -> TestClient:
    client = TestClient(app)
    assert client.post("/api/auth/login", json=creds).status_code == 200
    return client


def _resume(client: TestClient, run_id: int, *, additional: int = 0, reason: str | None = None):
    return client.post(f"/api/runs/{run_id}/resume",
                       json={"additional_budget_micro": additional, "reason": reason},
                       headers={"X-CSRF-Token": client.cookies.get("af_csrf")})


# --- 低预算真实暂停 -----------------------------------------------------------

def test_low_budget_pauses_run(platform_app, tmp_path: Path, monkeypatch) -> None:
    _, factory = platform_app
    _, run_id = _seed(factory)
    monkeypatch.setenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "1")
    monkeypatch.setenv("AGENTFORGE_PROJECT_BUDGET_MICRO", "0")  # 无预算 → 规划调用被拒 → 暂停

    result = run_generation(_context(factory, tmp_path, _Runner()), run_id)

    assert result["ok"] is False and result["error_code"] == "PAUSED_BUDGET"
    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        assert run.status == "PAUSED_BUDGET"  # 非终态、非 FAILED
        assert run.error_code is None


# --- 恢复被拒：未增加预算 -----------------------------------------------------

def test_resume_rejected_without_budget_increase(platform_app, tmp_path: Path, monkeypatch) -> None:
    _, factory = platform_app
    project_id, run_id = _seed(factory)
    monkeypatch.setenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "1")
    monkeypatch.setenv("AGENTFORGE_PROJECT_BUDGET_MICRO", "0")
    run_generation(_context(factory, tmp_path, _Runner()), run_id)

    client = _auth(platform_app[0], ALICE)
    response = _resume(client, run_id, additional=0)  # 未加预算

    assert response.status_code == 402 and response.json()["error"]["code"] == "BUDGET_EXCEEDED"
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "PAUSED_BUDGET"  # 仍在暂停
        assert session.query(BudgetAdjustment).filter(BudgetAdjustment.project_id == project_id).count() == 0


# --- 恢复：经确认加预算 → 重新入队续跑，不重复已提交任务 ----------------------

def test_resume_after_topup_continues_from_checkpoint(platform_app, tmp_path: Path, monkeypatch) -> None:
    app, factory = platform_app
    _, run_id = _seed(factory, status=RunStatus.PLANNING)
    run_dir = tmp_path / "runs" / f"run-{run_id}"

    # 先装配完成、验证阶段崩溃（检查点停在装配之后），再模拟预算暂停
    try:
        build_graph(_context(factory, tmp_path, _Runner(verify=_boom))).invoke({"run_id": run_id})
    except RuntimeError:
        pass
    lock_before = (run_dir / "asset-lock.json").read_text(encoding="utf-8")
    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        transition(session, run, RunStatus.PAUSED_BUDGET)  # 模拟预算暂停

    client = _auth(app, ALICE)
    response = _resume(client, run_id, additional=2_000_000, reason="topup")

    assert response.status_code == 200 and response.json()["data"]["status"] == "QUEUED"
    with session_scope(factory) as session:
        adj = session.query(BudgetAdjustment).filter(BudgetAdjustment.run_id == run_id).one()
        assert adj.delta_micro == 2_000_000 and adj.reason == "topup" and adj.actor_id == 1  # 可追溯

    # worker 续跑：不再重做装配、复用既有产物
    result = run_generation(_context(factory, tmp_path, _Runner()), run_id)

    assert result["ok"] is True, result
    assert (run_dir / "asset-lock.json").read_text(encoding="utf-8") == lock_before
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"
        assert session.query(RunTask).filter(RunTask.task_id == "assemble").count() == 1  # 未重复
        assert session.query(Artifact).filter(Artifact.kind == "source_tree").count() == 1


# --- 恢复前核对状态不明调用 ---------------------------------------------------

def test_resume_rejected_when_unsettled_calls(platform_app, tmp_path: Path, monkeypatch) -> None:
    app, factory = platform_app
    _, run_id = _seed(factory, status=RunStatus.GENERATING)
    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        transition(session, run, RunStatus.PAUSED_BUDGET)
        session.add(ModelCall(call_id="c1", model_role="coding", provider="dashscope",
                              model="qwen3-coder-flash", project_id=1, run_id=run_id,
                              reserved_micro=100, status="UNKNOWN"))

    client = _auth(app, ALICE)
    response = _resume(client, run_id, additional=5_000_000)

    assert response.status_code == 409  # 需先对账
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "PAUSED_BUDGET"


def test_resume_only_from_paused(platform_app) -> None:
    app, factory = platform_app
    _, run_id = _seed(factory, status=RunStatus.QUEUED)

    client = _auth(app, ALICE)
    assert _resume(client, run_id, additional=1_000_000).status_code == 409  # 非暂停不可恢复


def test_resume_requires_owner_and_csrf(platform_app, tmp_path: Path, monkeypatch) -> None:
    app, factory = platform_app
    _, run_id = _seed(factory, owner_id=1, status=RunStatus.GENERATING)
    with session_scope(factory) as session:
        transition(session, session.get(Run, run_id), RunStatus.PAUSED_BUDGET)

    bob = _auth(app, BOB)
    assert _resume(bob, run_id, additional=1_000_000).status_code == 403  # 非归属

    alice = _auth(app, ALICE)
    assert alice.post(f"/api/runs/{run_id}/resume", json={"additional_budget_micro": 1}).status_code == 403


# --- 生效预算 = 基础 + 调整 ---------------------------------------------------

def test_effective_budget_sums_adjustments(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    project_id, _ = _seed(factory)
    monkeypatch.setenv("AGENTFORGE_PROJECT_BUDGET_MICRO", "1000")

    with session_scope(factory) as session:
        assert effective_budget_micro(session, project_id) == 1000
    from agentforge_platform.budget import record_adjustment

    with session_scope(factory) as session:
        record_adjustment(session, project_id=project_id, delta_micro=500, reason="x", actor_id=1)
    with session_scope(factory) as session:
        assert effective_budget_micro(session, project_id) == 1500
