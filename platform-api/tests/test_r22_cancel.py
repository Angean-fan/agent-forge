"""R22：取消 run 与 Runner 资源回收。

覆盖：幂等 POST /api/runs/{id}/cancel（连点不重复）；QUEUED 直接取消、在途置标志由 worker 落 CANCELLED；
取消后不再派发/发新模型请求；账本保留在途/已确认额度；终态取消为 no-op；非归属不可取消。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.models import ModelCall, Project, Run, RunEvent, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.runs import (
    RunStatus,
    claim_run,
    create_run,
    request_cancel,
    transition,
)
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
ALICE = {"username": "alice", "password": "alice-secret"}
BOB = {"username": "bob", "password": "bob-secret"}
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


def _seed_project(factory, *, owner_id: int = 1) -> tuple[int, int]:
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
        return project.id, row.id


def _auth_client(app, creds: dict[str, str]) -> TestClient:
    client = TestClient(app)
    assert client.post("/api/auth/login", json=creds).status_code == 200
    return client


def _submit(client: TestClient, project_id: int, key: str):
    return client.post(f"/api/projects/{project_id}/runs", json={},
                       headers={"X-CSRF-Token": client.cookies.get("af_csrf"), "Idempotency-Key": key})


def _cancel(client: TestClient, run_id: int):
    return client.post(f"/api/runs/{run_id}/cancel",
                       headers={"X-CSRF-Token": client.cookies.get("af_csrf")})


# --- API：幂等取消 ------------------------------------------------------------

def test_cancel_queued_run_is_idempotent(platform_app) -> None:
    app, factory = platform_app
    project_id, _ = _seed_project(factory)
    client = _auth_client(app, ALICE)
    run_id = _submit(client, project_id, "k1").json()["data"]["id"]

    first = _cancel(client, run_id)
    second = _cancel(client, run_id)  # 连点取消

    assert first.status_code == 200 and second.status_code == 200
    body = second.json()["data"]
    assert body["status"] == "CANCELLED" and body["cancel_requested"] is True
    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        assert run.status == "CANCELLED" and run.cancel_requested is True
        events = session.query(RunEvent).filter(RunEvent.run_id == run_id,
                                                RunEvent.type == "cancel_requested").all()
        assert len(events) == 1  # 连点只写一次事件


def test_non_owner_cannot_cancel(platform_app) -> None:
    app, factory = platform_app
    project_id, _ = _seed_project(factory, owner_id=1)
    alice = _auth_client(app, ALICE)
    run_id = _submit(alice, project_id, "k1").json()["data"]["id"]

    bob = _auth_client(app, BOB)
    assert _cancel(bob, run_id).status_code == 403  # 非归属不可取消（归属校验）


def test_cancel_requires_csrf(platform_app) -> None:
    app, factory = platform_app
    project_id, _ = _seed_project(factory)
    client = _auth_client(app, ALICE)
    run_id = _submit(client, project_id, "k1").json()["data"]["id"]

    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 403  # 缺 CSRF


# --- 状态机与派发 -------------------------------------------------------------

def test_claim_run_skips_cancel_requested(platform_app) -> None:
    _, factory = platform_app
    project_id, spec_id = _seed_project(factory)
    with session_scope(factory) as session:
        project = session.get(Project, project_id)
        run = create_run(session, project, spec_id, idempotency_key="k1")
        run.cancel_requested = True  # 已请求但未落终态
        session.flush()
        assert claim_run(session, "w1") is None  # 不再派发


def test_cancel_terminal_run_is_noop(platform_app) -> None:
    _, factory = platform_app
    project_id, spec_id = _seed_project(factory)
    with session_scope(factory) as session:
        project = session.get(Project, project_id)
        run = create_run(session, project, spec_id, idempotency_key="k1")
        transition(session, run, RunStatus.PLANNING)
        transition(session, run, RunStatus.FAILED, error_code="X")
        request_cancel(session, run)
        assert run.status == "FAILED" and run.cancel_requested is False  # 终态不处理


# --- 生成图：取消观察与资源回收 ----------------------------------------------

class _CleanupRunner:
    def __init__(self, factory=None, cancel_on_verify: bool = False) -> None:
        self._factory = factory
        self._cancel_on_verify = cancel_on_verify
        self.cleaned: list[str] = []

    def verify(self, artifact_dir: str) -> dict:
        if self._cancel_on_verify and self._factory is not None:
            with session_scope(self._factory) as session:  # 模拟并发 API 取消
                run = session.query(Run).first()
                request_cancel(session, run)
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT

    def cleanup(self, artifact_dir: str) -> None:
        self.cleaned.append(artifact_dir)


def _queued_run(factory) -> int:
    project_id, spec_id = _seed_project(factory)
    with session_scope(factory) as session:
        project = session.get(Project, project_id)
        return create_run(session, project, spec_id, idempotency_key="k1").id


def test_cancel_before_claim_stops_dispatch(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _queued_run(factory)
    with session_scope(factory) as session:
        request_cancel(session, session.get(Run, run_id))  # QUEUED → 直接取消

    runner = _CleanupRunner()
    result = run_generation(GenerationContext(session_factory=factory, runner=runner,
                                              work_root=tmp_path / "runs", registry=_registry()), run_id)

    assert result["ok"] is False and result["error_code"] == "RUN_CANCELLED"
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "CANCELLED"


def test_cancel_mid_run_stops_and_cleans_runner(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _queued_run(factory)
    runner = _CleanupRunner(factory, cancel_on_verify=True)

    result = run_generation(GenerationContext(session_factory=factory, runner=runner,
                                              work_root=tmp_path / "runs", registry=_registry()), run_id)

    assert result["ok"] is False and result["error_code"] == "RUN_CANCELLED"
    assert runner.cleaned == [f"run-{run_id}"]  # 尽力释放受控资源
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "CANCELLED"


def test_cancel_preserves_ledger_reservation(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _queued_run(factory)
    with session_scope(factory) as session:  # 在途预留
        session.add(ModelCall(call_id="c1", model_role="coding", provider="dashscope",
                              model="qwen3-coder-flash", project_id=1, run_id=run_id,
                              task_id="t", reserved_micro=1234, status="RESERVED"))
    with session_scope(factory) as session:
        request_cancel(session, session.get(Run, run_id))
        # 取消不清账：在途/已确认额度如实保留
        row = session.get(ModelCall, "c1")
        assert row is not None and row.reserved_micro == 1234 and row.status == "RESERVED"


def _registry():
    from agentforge_platform.asset_registry import default_registry

    return default_registry()
