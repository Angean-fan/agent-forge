"""R25：运行历史、失败详情、阶段成本与下载授权。

覆盖：项目运行历史仅归属者可见；失败原因用确定性模板、交付包一致标注（失败包“未验证”）；按 artifact_id 下载
严格校验 owner 与 artifact 归属；分层阶段成本；增量事件分页；跨用户不可查看/下载。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from agentforge_platform.asset_registry import default_registry
from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.models import ModelCall, Project, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
ALICE = {"username": "alice", "password": "alice-secret"}
BOB = {"username": "bob", "password": "bob-secret"}
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}
_FAIL_VERIFY = {"overall": "FAIL", "checks": [{"name": "backend_build", "status": "FAIL",
                                               "error_code": "BUILD_FAILED"}]}


class _Runner:
    def __init__(self, verify: dict) -> None:
        self._verify = verify

    def verify(self, artifact_dir: str) -> dict:
        return self._verify

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


def _seed(factory, *, owner_id: int = 1, key: str = "k1") -> tuple[int, int]:
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
        run = create_run(session, project, row.id, idempotency_key=key,
                         spec_hash=content_hash(spec))
        session.flush()
        return project.id, run.id


def _auth(app, creds) -> TestClient:
    client = TestClient(app)
    assert client.post("/api/auth/login", json=creds).status_code == 200
    return client


def _complete(factory, tmp_path: Path, run_id: int, verify: dict) -> None:
    context = GenerationContext(session_factory=factory, runner=_Runner(verify),
                                work_root=tmp_path / "runs", registry=default_registry())
    run_generation(context, run_id)


# --- 历史列表归属 -------------------------------------------------------------

def test_history_lists_own_runs_only(platform_app, tmp_path: Path, monkeypatch) -> None:
    app, factory = platform_app
    project_id, run_id = _seed(factory, owner_id=1)
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    _complete(factory, tmp_path, run_id, _PASS_VERIFY)

    alice = _auth(app, ALICE)
    body = alice.get(f"/api/projects/{project_id}/runs").json()["data"]
    assert [item["id"] for item in body["items"]] == [run_id]
    assert body["items"][0]["status"] == "SUCCEEDED"
    assert body["items"][0]["spec_version"] == 1

    bob = _auth(app, BOB)
    assert bob.get(f"/api/projects/{project_id}/runs").status_code == 403  # 跨用户不可见


# --- 失败详情与交付包一致标注 -------------------------------------------------

def test_failure_detail_and_unverified_package(platform_app, tmp_path: Path, monkeypatch) -> None:
    app, factory = platform_app
    _, run_id = _seed(factory, owner_id=1)
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    _complete(factory, tmp_path, run_id, _FAIL_VERIFY)  # 构建失败 → 失败包

    alice = _auth(app, ALICE)
    detail = alice.get(f"/api/runs/{run_id}").json()["data"]

    assert detail["status"] == "FAILED"
    assert detail["failure"] == "构建或验收未通过，请查看验证报告中的未通过项。"  # 确定性模板
    assert detail["package"]["available"] is True and detail["package"]["verified"] is False
    assert detail["package"]["label"] == "未验证"
    assert "未通过全部必需检查" in detail["package"]["notice"]


def test_success_package_is_verified(platform_app, tmp_path: Path, monkeypatch) -> None:
    app, factory = platform_app
    _, run_id = _seed(factory, owner_id=1)
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    _complete(factory, tmp_path, run_id, _PASS_VERIFY)

    detail = _auth(app, ALICE).get(f"/api/runs/{run_id}").json()["data"]
    assert detail["package"]["verified"] is True and detail["package"]["label"] == "已验证"
    assert detail["failure"] is None


# --- 按 artifact_id 下载：owner + 归属校验 -----------------------------------

def test_download_by_artifact_id_enforces_owner_and_run(platform_app, tmp_path: Path, monkeypatch) -> None:
    app, factory = platform_app
    _, run_id = _seed(factory, owner_id=1, key="k1")
    _, other_run_id = _seed(factory, owner_id=1, key="k2")
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    _complete(factory, tmp_path, run_id, _PASS_VERIFY)
    _complete(factory, tmp_path, other_run_id, _PASS_VERIFY)

    alice = _auth(app, ALICE)
    detail = alice.get(f"/api/runs/{run_id}").json()["data"]
    zip_id = detail["package"]["artifact_id"]

    assert alice.get(f"/api/runs/{run_id}/artifacts/{zip_id}").status_code == 200
    # artifact 属于另一个 run → 拒绝
    assert alice.get(f"/api/runs/{other_run_id}/artifacts/{zip_id}").status_code == 404
    # 跨用户 → 403
    bob = _auth(app, BOB)
    assert bob.get(f"/api/runs/{run_id}/artifacts/{zip_id}").status_code == 403


def test_cross_user_cannot_view_detail(platform_app, tmp_path: Path, monkeypatch) -> None:
    app, factory = platform_app
    _, run_id = _seed(factory, owner_id=1)
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))

    bob = _auth(app, BOB)
    assert bob.get(f"/api/runs/{run_id}").status_code == 403


# --- 阶段成本与事件分页 -------------------------------------------------------

def test_cost_endpoint_layers(platform_app, tmp_path: Path, monkeypatch) -> None:
    app, factory = platform_app
    project_id, run_id = _seed(factory, owner_id=1)
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    with session_scope(factory) as session:
        session.add(ModelCall(call_id="d1", model_role="user_interaction", provider="deepseek_official",
                              model="deepseek-flash", project_id=project_id, run_id=None,
                              input_tokens=10, output_tokens=5, cost_micro=100, status="SETTLED"))
        session.add(ModelCall(call_id="t1", model_role="coding", provider="dashscope",
                              model="qwen3-coder-flash", project_id=project_id, run_id=run_id,
                              task_id="gap:AC01", input_tokens=20, output_tokens=8, cost_micro=300,
                              status="SETTLED"))

    cost = _auth(app, ALICE).get(f"/api/runs/{run_id}/cost").json()["data"]
    assert cost["interview_phase"]["calls"] == 1
    assert set(cost["by_task"]) == {"gap:AC01"}
    assert cost["by_task"]["gap:AC01"]["cost_micro"] == 300


def test_events_incremental_pagination(platform_app, tmp_path: Path, monkeypatch) -> None:
    app, factory = platform_app
    _, run_id = _seed(factory, owner_id=1)
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    _complete(factory, tmp_path, run_id, _PASS_VERIFY)

    alice = _auth(app, ALICE)
    first = alice.get(f"/api/runs/{run_id}/events?after_sequence=0").json()["data"]
    assert first["items"] and first["latest_sequence"] >= len(first["items"])
    later = alice.get(f"/api/runs/{run_id}/events?after_sequence={first['latest_sequence']}").json()["data"]
    assert later["items"] == []  # 增量：不再返回已读事件
