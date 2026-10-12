"""R13：接口契约先行的多任务 DAG 规划。

覆盖：task_id 账本列与网关归属校验；TaskPlan 确定性校验（无环/越界/冲突/资产引用）；
确定性优先与规划复杂性触发；technical_planning 未启用/失败即回退并记录原因；生成图产出 task-plan.json。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from agentforge_platform.asset_finder import derive_capability_query, find_assets
from agentforge_platform.asset_registry import default_registry
from agentforge_platform.clients import Completion, Usage
from agentforge_platform.contract import compile_from_spec
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.model_gateway import reserve
from agentforge_platform.models import ModelCall, Project, Run, RunTask, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.resolver import resolve_assets
from agentforge_platform.runs import create_run, record_task
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash
from agentforge_platform.task_plan import TaskPlan, TaskSpec, validate_plan
from agentforge_platform.task_planning import (
    deterministic_plan,
    input_digest,
    needs_planning,
    plan_tasks,
)

REPO = Path(__file__).resolve().parents[2]
CAMPUS = REPO / "schemas" / "campus-activity.spec.json"
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


def _campus() -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(CAMPUS.read_text(encoding="utf-8"))


def _search(spec):
    return find_assets(derive_capability_query(spec, compile_from_spec(spec)), registry=default_registry())


def _seed_run(factory) -> int:
    spec = _campus()
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        row = SpecVersion(
            project_id=project.id, version=1, body_json=canonical_json(spec),
            hash=content_hash(spec), confirmed_at=datetime(2026, 1, 1),
        )
        session.add(row)
        session.flush()
        project.active_spec_id = row.id
        run = create_run(session, project, row.id, idempotency_key="k1", spec_hash=content_hash(spec))
        return run.id


# --- 网关：task_id 归属校验 ---------------------------------------------------

def test_gateway_records_task_id_and_rejects_bad_pairing(platform_app) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory)
    with session_scope(factory) as session:  # 登记一个属于该 run 的 DAG 任务
        record_task(session, session.get(Run, run_id), "assemble", status="DONE")

    with session_scope(factory) as session:
        row = reserve(
            session, call_id="c1", model_role="user_interaction", project_id=1,
            max_input_tokens=100, max_output_tokens=100, run_id=run_id, task_id="assemble",
        )
        assert row.task_id == "assemble" and row.run_id == run_id

    with session_scope(factory) as session:
        with pytest.raises(AppException) as wrong:
            reserve(session, call_id="c2", model_role="user_interaction", project_id=1,
                    max_input_tokens=100, max_output_tokens=100, run_id=run_id, task_id="missing")
        assert wrong.value.code == "VALIDATION_ERROR"

    with session_scope(factory) as session:
        with pytest.raises(AppException) as orphan:
            reserve(session, call_id="c3", model_role="user_interaction", project_id=1,
                    max_input_tokens=100, max_output_tokens=100, task_id="assemble")
        assert orphan.value.code == "VALIDATION_ERROR"

    with session_scope(factory) as session:
        assert session.query(ModelCall).count() == 1  # 仅合法调用落账


# --- TaskPlan 确定性校验 ------------------------------------------------------

def test_validate_plan_rejects_cycle_path_escape_conflict_and_unknown_asset() -> None:
    good = TaskPlan(execution_mode="planned", input_digest_hash="h", tasks=[
        TaskSpec(task_id="a", operation="x", allowed_paths=["backend/a/"], asset_refs=["capability-pack:p@1"]),
    ])
    assert validate_plan(good, known_asset_refs={"capability-pack:p@1"}, known_acceptance_ids=set()) == []

    cycle = TaskPlan(execution_mode="planned", input_digest_hash="h", tasks=[
        TaskSpec(task_id="a", operation="x", depends_on=["b"]),
        TaskSpec(task_id="b", operation="x", depends_on=["a"]),
    ])
    assert any("环" in i for i in validate_plan(cycle, known_asset_refs=set(), known_acceptance_ids=set()))

    escape = TaskPlan(execution_mode="planned", input_digest_hash="h", tasks=[
        TaskSpec(task_id="a", operation="x", allowed_paths=["../etc/passwd"]),
    ])
    assert any("越界" in i for i in validate_plan(escape, known_asset_refs=set(), known_acceptance_ids=set()))

    conflict = TaskPlan(execution_mode="planned", input_digest_hash="h", tasks=[
        TaskSpec(task_id="a", operation="x", allowed_paths=["backend/"]),
        TaskSpec(task_id="b", operation="x", allowed_paths=["backend/x/"]),
    ])
    assert any("同路径冲突" in i for i in validate_plan(conflict, known_asset_refs=set(), known_acceptance_ids=set()))

    bad_asset = TaskPlan(execution_mode="planned", input_digest_hash="h", tasks=[
        TaskSpec(task_id="a", operation="x", asset_refs=["capability-pack:unknown@9"]),
    ])
    assert any("未锁定资产" in i for i in validate_plan(bad_asset, known_asset_refs=set(), known_acceptance_ids=set()))


# --- 规划触发与确定性转换 -----------------------------------------------------

def test_needs_planning_and_deterministic_digest() -> None:
    spec = _campus()
    search = _search(spec)
    lock = resolve_assets(default_registry(), "demo-ts-mp")

    assert needs_planning(search, spec) is True  # 有未覆盖验收
    plan = deterministic_plan(spec, compile_from_spec(spec), search, lock)
    assert plan.execution_mode == "deterministic"
    assert [t.task_id for t in plan.tasks] == [
        "load_confirmed_spec", "resolve_assets", "assemble", "verify", "package_or_fail",
    ]
    assert plan.input_digest_hash == input_digest(spec, compile_from_spec(spec), search)


def test_plan_tasks_falls_back_when_route_disabled(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory)
    monkeypatch.delenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", raising=False)
    spec = _campus()
    search = _search(spec)

    with session_scope(factory) as session:
        plan, reasons = plan_tasks(
            session, session.get(Project, 1), spec, compile_from_spec(spec), search,
            resolve_assets(default_registry(), "demo-ts-mp"), run_id=run_id,
        )

    assert plan.execution_mode == "deterministic"
    assert reasons == ["MODEL_ROLE_DISABLED"]


class _PlanClient:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def complete(self, *, model, prompt, thinking_mode=None, json_mode=False,
                 max_output_tokens=None, reasoning_effort=None) -> Completion:
        return Completion(content=self._payload, model=model, request_id="rid",
                          usage=Usage(10, 5), finish_reason="stop")


def test_plan_tasks_uses_model_when_enabled_and_valid(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory)
    monkeypatch.setenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "1")
    spec = _campus()
    search = _search(spec)
    lock = resolve_assets(default_registry(), "demo-ts-mp")
    payload = json.dumps({"tasks": [
        {"task_id": "assemble", "operation": "assemble", "mode": "render",
         "asset_refs": [f"{a.kind}:{a.id}@{a.version}" for a in lock.assets]},
    ]})
    client = _PlanClient(payload)

    with session_scope(factory) as session:
        plan, reasons = plan_tasks(
            session, session.get(Project, 1), spec, compile_from_spec(spec), search, lock,
            run_id=run_id, client=client,
        )

    assert reasons == []
    assert plan.execution_mode == "planned"
    assert [t.task_id for t in plan.tasks] == ["assemble"]


def test_plan_tasks_falls_back_on_invalid_model_plan(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory)
    monkeypatch.setenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "1")
    spec = _campus()
    search = _search(spec)
    # 引用未锁定资产 → 校验失败 → 回退
    client = _PlanClient(json.dumps({"tasks": [
        {"task_id": "a", "operation": "x", "asset_refs": ["capability-pack:unknown@9"]},
    ]}))

    with session_scope(factory) as session:
        plan, reasons = plan_tasks(
            session, session.get(Project, 1), spec, compile_from_spec(spec), search,
            resolve_assets(default_registry(), "demo-ts-mp"), run_id=run_id, client=client,
        )

    assert plan.execution_mode == "deterministic"
    assert reasons == ["PLAN_INVALID"]


# --- 生成图产出 task-plan.json ------------------------------------------------

class _PassRunner:
    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


def test_generation_emits_task_plan(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory)
    context = GenerationContext(
        session_factory=factory, runner=_PassRunner(),
        work_root=tmp_path / "runs", registry=default_registry(),
    )

    result = run_generation(context, run_id)

    assert result["ok"] is True, result
    plan = json.loads((tmp_path / "runs" / f"run-{run_id}" / "task-plan.json").read_text("utf-8"))
    assert plan["execution_mode"] == "deterministic"
    assert plan["input_digest_hash"]
    assert set(plan["uncovered_acceptance_ids"]) == {"AC01", "AC02", "AC03", "AC04"}
    assert "technical_planning" in plan["retrieval_reasons"]
    with session_scope(factory) as session:
        tasks = {t.task_id for t in session.query(RunTask).all()}
        assert "plan_tasks" in tasks and session.get(Run, run_id).status == "SUCCEEDED"
