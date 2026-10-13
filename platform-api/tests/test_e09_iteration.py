"""E09：既有生成项目的规格增量迭代。

覆盖：仅 AgentForge 自有产物可迭代（缺 spec.json/asset-lock.json 拒绝）；新增字段/规则后**生成新版本**
（新 spec 版本 + 新 run）并**保留旧 ZIP/报告**（旧 run 目录字节不变）；契约改变时 `relock_required`
（显式重新检索/冻结新锁）；优先复用锁定的旧方法资产；回归（新 run 跑通）通过。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentforge_platform.asset_registry import default_registry
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.iteration import (
    analyze_impact,
    artifact_eligibility,
    classify_change,
    load_artifact,
    plan_iteration,
    refine_change_intent,
)
from agentforge_platform.models import Project, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.resolver import AssetLock, LockedAsset
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
ALICE = {"username": "alice", "password": "alice-secret"}
BOB = {"username": "bob", "password": "bob-secret"}
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


class _Runner:
    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


def _spec(add_field: str | None = None, add_rule: str | None = None) -> SoftwareSpecification:
    payload = json.loads(TASK.read_text(encoding="utf-8"))
    if add_field:
        payload["entities"][0]["fields"].append(
            {"name": add_field, "type": "string", "required": False}
        )
    if add_rule:
        payload["business_rules"].append({"id": add_rule, "text": "新增业务规则", "source": "user_confirmed"})
    return SoftwareSpecification.model_validate(payload)


def _lock_with(*method_ids: str) -> AssetLock:
    template = AssetLock.model_validate_json(
        (REPO / "schemas" / "asset-lock.r27.sample.json").read_text(encoding="utf-8")
    )
    units = [LockedAsset(kind="method-unit", id=mid, version="1.0.0", checksum="a" * 64,
                         path=f"methods/{mid}/1.0.0") for mid in method_ids]
    return template.model_copy(update={"method_units": units, "schema_version": "0.2"})


# --- 单元：资格 / 影响 / 计划 --------------------------------------------------

def test_eligibility_requires_agentforge_artifact(tmp_path: Path) -> None:
    empty = tmp_path / "run-1"
    empty.mkdir()
    assert artifact_eligibility(empty) != []
    with pytest.raises(AppException) as error:
        load_artifact(empty)
    assert error.value.code == "ITERATION_INELIGIBLE"

    (empty / "spec.json").write_text("{}")
    (empty / "asset-lock.json").write_text("{}")
    assert artifact_eligibility(empty) == []


def test_classify_change_detects_contract_change() -> None:
    base = _spec()
    assert classify_change(base, base).contract_changed is False

    impact = classify_change(base, _spec(add_field="priority"))
    assert impact.contract_changed is True
    assert "Task.priority" in impact.added_fields
    assert "新增字段" in impact.summary


def test_plan_marks_relock_and_reuses_locked_units() -> None:
    old_spec = _spec()
    old_lock = _lock_with("date-normalizer", "status-guard")
    new_spec = _spec(add_field="priority")
    new_spec = new_spec.model_copy(update={"method_requests": ["date-normalizer@1.0.0"]})

    plan = plan_iteration(old_spec, old_lock, new_spec)

    assert plan.relock_required is True  # 契约改变 → 显式重新检索/冻结新锁
    assert plan.reused_method_units == ("date-normalizer@1.0.0",)
    assert plan.dropped_method_units == ("status-guard@1.0.0",)
    assert any("重新检索" in note for note in plan.notes)


def test_seams_are_facts_only() -> None:
    impact = classify_change(_spec(), _spec(add_rule="BR99"))
    captured: dict[str, str] = {}

    def planner(payload: str) -> str:
        captured["payload"] = payload
        return "建议：仅新增规则，不动契约。"

    analysis = analyze_impact(impact, planner=planner)
    assert "建议" in analysis["planner_note"]
    assert set(json.loads(captured["payload"])) >= {"contract_changed", "added_rules"}
    assert "变更意图" in refine_change_intent("给任务加优先级")


# --- API：迭代生成新版本并保留旧产物 ------------------------------------------

def _seed(factory, *, key: str = "e09") -> tuple[int, int]:
    spec = _spec()
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        row = SpecVersion(project_id=project.id, version=1, body_json=canonical_json(spec),
                          hash=content_hash(spec), confirmed_at=datetime(2026, 1, 1))
        session.add(row)
        session.flush()
        project.active_spec_id = row.id
        run = create_run(session, project, row.id, idempotency_key=key, spec_hash=content_hash(spec))
        session.flush()
        return project.id, run.id


def _complete(factory, tmp_path: Path, run_id: int) -> None:
    context = GenerationContext(session_factory=factory, runner=_Runner(),
                                work_root=tmp_path / "runs", registry=default_registry())
    run_generation(context, run_id)


def _auth(app, creds) -> TestClient:
    client = TestClient(app)
    assert client.post("/api/auth/login", json=creds).status_code == 200
    return client


def _csrf(client: TestClient) -> str:
    return client.cookies.get("af_csrf")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_iteration_creates_new_version_and_keeps_old(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    _, old_run = _seed(factory)
    _complete(factory, tmp_path, old_run)

    old_dir = tmp_path / "runs" / f"run-{old_run}"
    before = {name: _digest(old_dir / name) for name in ("spec.json", "asset-lock.json")}

    alice = _auth(app, ALICE)
    new_spec = _spec(add_field="priority").model_dump(mode="json")
    response = alice.post(f"/api/runs/{old_run}/iterate",
                          json={"spec": new_spec, "idempotency_key": "iter-1"},
                          headers={"X-CSRF-Token": _csrf(alice)})
    assert response.status_code == 202
    data = response.json()["data"]
    assert data["old_run_id"] == old_run
    assert data["new_run_id"] != old_run
    assert data["spec_version"] == 2  # 生成新版本
    assert data["contract_changed"] is True and data["relock_required"] is True

    # 旧 run 产物**字节不变**（不原地修改），ZIP 仍在
    after = {name: _digest(old_dir / name) for name in ("spec.json", "asset-lock.json")}
    assert before == after
    assert list(old_dir.glob("*.zip"))

    # 回归：新 run 可跑通并产出**新版本**产物（含新字段），旧 run 仍完好
    _complete(factory, tmp_path, data["new_run_id"])
    new_dir = tmp_path / "runs" / f"run-{data['new_run_id']}"
    assert (new_dir / "asset-lock.json").is_file()
    assert "priority" in (new_dir / "spec.json").read_text(encoding="utf-8")  # 新版本含新增字段
    assert list(new_dir.glob("*.zip"))
    after2 = {name: _digest(old_dir / name) for name in ("spec.json", "asset-lock.json")}
    assert after2 == before  # 旧 run 全程未被修改


def test_iteration_requires_owner_and_artifact(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    _, run_id = _seed(factory)
    _complete(factory, tmp_path, run_id)
    alice = _auth(app, ALICE)

    bob = _auth(app, BOB)
    assert bob.post(f"/api/runs/{run_id}/iterate", json={"spec": _spec(add_field="x").model_dump(mode="json")},
                    headers={"X-CSRF-Token": _csrf(bob)}).status_code == 403  # 跨用户

    # 去掉 AgentForge 产物 → 不支持迭代
    (tmp_path / "runs" / f"run-{run_id}" / "asset-lock.json").unlink()
    rejected = alice.post(f"/api/runs/{run_id}/iterate",
                          json={"spec": _spec(add_field="y").model_dump(mode="json")},
                          headers={"X-CSRF-Token": _csrf(alice)})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "ITERATION_INELIGIBLE"


def test_iteration_requires_completed_run(platform_app, tmp_path, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    _, run_id = _seed(factory)  # 未完成
    alice = _auth(app, ALICE)

    response = alice.post(f"/api/runs/{run_id}/iterate",
                          json={"spec": _spec(add_field="z").model_dump(mode="json")},
                          headers={"X-CSRF-Token": _csrf(alice)})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_INVALID_TRANSITION"
