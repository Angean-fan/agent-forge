"""C05：既有生成项目的真实增量迭代（E09）。

覆盖（**确定性/受控，不发起真实模型调用**）：

- **最小增量**：新旧 run 目录逐文件比对——`spec.json` 等受影响文件变更，模板全局文件**字节稳定**，
  无文件被删除（`untouched_stable`）；
- **网关化模型缝**：变更意图走 DeepSeek（缺凭据 → ``NOT_RUN``）；影响分析走 Qwen（路由禁用 → ``DISABLED``、
  缺凭据 → ``NOT_RUN``、注入假客户端 → 入账 `technical_planning`）；
- **契约变更加锁**：新增字段 → `relock_required`；仅改目标文本 → 不需重新锁定；
- **失败可回滚**：越界新规格被拒，旧产物字节不变、不产生新 run。

真实 DeepSeek/Qwen 调用 → NOT_RUN（缺凭据/未开启）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from agentforge_platform.asset_registry import default_registry
from agentforge_platform.clients import Completion, DeepSeekClient, Usage
from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.iteration import classify_change
from agentforge_platform.iteration_service import (
    STATUS_DISABLED,
    STATUS_NOT_RUN,
    STATUS_OK,
    analyze_impact_gateway,
    diff_artifacts,
    refine_change_intent_gateway,
    untouched_stable,
)
from agentforge_platform.models import ModelCall, Project, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
ALICE = {"username": "alice", "password": "alice-secret"}
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


class _Runner:
    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


def _spec(**overrides) -> SoftwareSpecification:
    payload = json.loads(TASK.read_text(encoding="utf-8"))
    if "add_field" in overrides:
        payload["entities"][0]["fields"].append(
            {"name": overrides["add_field"], "type": "string", "required": False})
    return SoftwareSpecification.model_validate(payload)


def _seed(factory, spec: SoftwareSpecification) -> tuple[int, int]:
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        row = SpecVersion(project_id=project.id, version=1, body_json=canonical_json(spec),
                          hash=content_hash(spec), confirmed_at=datetime(2026, 1, 1))
        session.add(row)
        session.flush()
        project.active_spec_id = row.id
        run = create_run(session, project, row.id, idempotency_key="c05", spec_hash=content_hash(spec))
        session.flush()
        return project.id, run.id


def _complete(factory, tmp_path: Path, run_id: int) -> None:
    context = GenerationContext(session_factory=factory, runner=_Runner(),
                                work_root=tmp_path / "runs", registry=default_registry())
    run_generation(context, run_id)


def _auth(app) -> TestClient:
    client = TestClient(app)
    assert client.post("/api/auth/login", json=ALICE).status_code == 200
    return client


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _project(factory) -> int:
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="c05", requirement_text="x")
        session.add(project)
        session.flush()
        return project.id


# --- 单元：差异分类 ------------------------------------------------------------

def test_diff_artifacts_classifies_changes(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    (old / "sub").mkdir(parents=True)
    (new / "sub").mkdir(parents=True)
    (old / "same.txt").write_text("x")
    (new / "same.txt").write_text("x")
    (old / "mod.txt").write_text("a")
    (new / "mod.txt").write_text("b")
    (new / "added.txt").write_text("z")
    (old / "sub" / "gone.txt").write_text("g")

    delta = diff_artifacts(old, new)

    assert delta.unchanged == ("same.txt",)
    assert delta.changed == ("mod.txt",)
    assert delta.added == ("added.txt",)
    assert delta.removed == ("sub/gone.txt",)
    assert untouched_stable(delta) is False  # 有删除 → 不稳定
    assert delta.summary()["unchanged"] == 1


# --- 集成：增量迭代的最小性与不回改 --------------------------------------------

def test_iteration_delta_keeps_untouched_modules_stable(
    platform_app, tmp_path: Path, monkeypatch
) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    _, old_run = _seed(factory, _spec())
    _complete(factory, tmp_path, old_run)
    old_dir = tmp_path / "runs" / f"run-{old_run}"
    before = {n: _digest(old_dir / n) for n in ("spec.json", "asset-lock.json")}

    alice = _auth(app)
    new_spec = _spec(add_field="priority").model_dump(mode="json")
    response = alice.post(f"/api/runs/{old_run}/iterate",
                          json={"spec": new_spec, "idempotency_key": "c05-1"},
                          headers={"X-CSRF-Token": alice.cookies.get("af_csrf")})
    assert response.status_code == 202
    new_run = response.json()["data"]["new_run_id"]
    _complete(factory, tmp_path, new_run)
    new_dir = tmp_path / "runs" / f"run-{new_run}"

    delta = diff_artifacts(old_dir, new_dir)
    assert "spec.json" in delta.changed  # 受影响文件确实变化
    assert untouched_stable(delta)  # 存在未变文件、无删除
    # 模板/全局文件保持字节稳定
    assert "compose.yaml" in delta.unchanged
    assert "backend/pom.xml" in delta.unchanged
    assert "frontend/package.json" in delta.unchanged
    # 旧 run 全程未被修改
    assert {n: _digest(old_dir / n) for n in ("spec.json", "asset-lock.json")} == before


# --- 网关化模型缝 --------------------------------------------------------------

def test_change_intent_not_run_without_key(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    project_id = _project(factory)
    with session_scope(factory) as session:
        text, status = refine_change_intent_gateway(
            session, project_id=project_id, intent="给任务加优先级", client=DeepSeekClient())
        rows = session.query(ModelCall).all()
    assert status == STATUS_NOT_RUN and text == ""
    assert all(row.status == "NOT_SENT" for row in rows)  # 请求未发出


def test_change_intent_with_injected_client(platform_app) -> None:
    _, factory = platform_app
    project_id = _project(factory)

    class _Fake:
        def complete(self, *, model, prompt, thinking_mode=None, json_mode=False,
                     max_output_tokens=None, reasoning_effort=None):
            return Completion(content="将生成新版本，旧产物不变。", model=model, request_id="r",
                              usage=Usage(4, 2), finish_reason="stop")

    with session_scope(factory) as session:
        text, status = refine_change_intent_gateway(
            session, project_id=project_id, intent="给任务加优先级", client=_Fake())
        rows = session.query(ModelCall).all()
    assert status == STATUS_OK and "新版本" in text
    assert rows and all(r.model_role == "user_interaction" for r in rows)


def test_impact_gateway_disabled_route_makes_no_call(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    monkeypatch.delenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", raising=False)
    project_id = _project(factory)
    impact = classify_change(_spec(), _spec(add_field="priority"))

    with session_scope(factory) as session:
        analysis, status = analyze_impact_gateway(
            session, project_id=project_id, impact=impact, client=DeepSeekClient())
        rows = session.query(ModelCall).all()
    assert status == STATUS_DISABLED and analysis == {}
    assert rows == []  # 路由禁用 → 零调用


def test_impact_gateway_with_injected_client(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    monkeypatch.setenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "1")
    project_id = _project(factory)
    impact = classify_change(_spec(), _spec(add_field="priority"))

    class _Fake:
        def complete(self, *, model, prompt, thinking_mode=None, json_mode=False,
                     max_output_tokens=None, reasoning_effort=None):
            return Completion(content='{"tasks":[{"task_id":"add-priority","operation":"patch"}]}',
                              model=model, request_id="r", usage=Usage(6, 4), finish_reason="stop")

    with session_scope(factory) as session:
        analysis, status = analyze_impact_gateway(
            session, project_id=project_id, impact=impact, client=_Fake())
        rows = session.query(ModelCall).all()
    assert status == STATUS_OK and analysis["tasks"][0]["task_id"] == "add-priority"
    assert rows and all(r.model_role == "technical_planning" and r.provider == "dashscope" for r in rows)


# --- 契约变更判定 --------------------------------------------------------------

def test_relock_required_links_to_contract_change() -> None:
    """E09 语义：`BusinessContract` 绑定 `spec_hash`，故规格变更即契约变更 → `relock_required=True`。"""
    from agentforge_platform.iteration import plan_iteration
    from agentforge_platform.resolver import AssetLock

    lock = AssetLock.model_validate_json(
        (REPO / "schemas" / "asset-lock.r27.sample.json").read_text(encoding="utf-8"))
    impact = classify_change(_spec(), _spec(add_field="priority"))
    plan = plan_iteration(_spec(), lock, _spec(add_field="priority"))

    assert impact.contract_changed is True
    assert plan.relock_required == plan.impact.contract_changed
    assert any("重新" in note for note in plan.notes)  # 重新冻结注记可定位


# --- 失败可回滚 ----------------------------------------------------------------

def test_failed_iteration_preserves_old_artifacts(platform_app, tmp_path: Path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    _, old_run = _seed(factory, _spec())
    _complete(factory, tmp_path, old_run)
    old_dir = tmp_path / "runs" / f"run-{old_run}"
    before = {n: _digest(old_dir / n) for n in ("spec.json", "asset-lock.json")}

    alice = _auth(app)
    bad = _spec().model_dump(mode="json")
    bad["capability_requests"] = [{"id": "out-of-scope"}]  # 超出 Demo 范围
    response = alice.post(f"/api/runs/{old_run}/iterate",
                          json={"spec": bad, "idempotency_key": "c05-bad"},
                          headers={"X-CSRF-Token": alice.cookies.get("af_csrf")})
    assert response.status_code == 422  # 拒绝，不产生新 run
    with session_scope(factory) as session:
        assert session.query(SpecVersion).filter(SpecVersion.project_id == 1).count() == 1  # 未冻结新版本
    assert {n: _digest(old_dir / n) for n in ("spec.json", "asset-lock.json")} == before
