"""R14：能力覆盖检查与 Gap Task 生成。

覆盖：已知主线（Task/校园活动）+ 固定验收 PASS → 全覆盖、0 缺口；未通过验收/未知形状 → 缺口；
方法单元显式声明的验收算覆盖；相似但不适配的方法 → 安全 Gap；生成图产出 coverage.json 且不做 coding 调用。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from agentforge_platform.asset_finder import derive_capability_query, find_assets
from agentforge_platform.asset_registry import default_registry
from agentforge_platform.contract import compile_from_spec
from agentforge_platform.coverage import analyze_coverage, platform_mainline
from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.method_index import DEFAULT_METHODS_ROOT
from agentforge_platform.method_lock import resolve_lock
from agentforge_platform.models import ModelCall, Project, Run, RunTask, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
CAMPUS = REPO / "schemas" / "campus-activity.spec.json"
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


def _spec(path: Path) -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(path.read_text(encoding="utf-8"))


def _report_spec(**overrides) -> SoftwareSpecification:
    """未知主线形状：单实体 Report（平台无固定验收证据）。"""
    payload = {
        "schema_version": "0.1", "project_type": "crud_web", "goal": "报表管理。",
        "actors": ["ADMIN", "MEMBER"],
        "entities": [{"name": "Report", "source": "user_confirmed", "fields": [
            {"name": "id", "type": "long", "required": True},
            {"name": "title", "type": "string", "required": True},
            {"name": "status", "type": "string", "required": True},
            {"name": "owner_id", "type": "long", "required": True},
            {"name": "created_at", "type": "datetime", "required": True}]}],
        "permissions": [
            {"role": "ADMIN", "actions": ["create", "read", "update", "delete"], "source": "user_confirmed"},
            {"role": "MEMBER", "actions": ["create", "read", "update", "delete"], "source": "user_confirmed"}],
        "business_rules": [{"id": "BR01", "text": "报表标题必填", "source": "user_confirmed"}],
        "acceptance_cases": [{"id": "AC01", "scenario": "成员创建报表", "expected": "成功", "source": "user_confirmed"}],
        "technical_profile": {"profile_id": "demo-ts-mp", "profile_version": "0.3", "template_version": "demo-0.2"},
    }
    payload.update(overrides)
    return SoftwareSpecification.model_validate(payload)


def _coverage(spec, acceptance, *, lock=None):
    contract = compile_from_spec(spec)
    search = find_assets(derive_capability_query(spec, contract), registry=default_registry())
    lock = lock or resolve_lock(default_registry(), spec.technical_profile.profile_id, spec, contract)
    return analyze_coverage(spec, contract, lock, search, acceptance, methods_root=DEFAULT_METHODS_ROOT)


# --- 已知主线：固定验收 PASS → 全覆盖 ----------------------------------------

def test_known_mainlines_are_detected() -> None:
    assert platform_mainline(_spec(TASK)) == "mvp_task"
    assert platform_mainline(_spec(CAMPUS)) == "campus_activity"
    assert platform_mainline(_report_spec()) is None


def test_standard_task_run_is_fully_covered_with_no_gaps() -> None:
    report = _coverage(_spec(TASK), {"overall": "PASS"})

    assert report.mainline == "mvp_task"
    assert report.covered_acceptance_ids == ("AC01", "AC02")
    assert report.gaps == ()


def test_campus_mainline_is_fully_covered() -> None:
    report = _coverage(_spec(CAMPUS), {"overall": "PASS"})

    assert report.mainline == "campus_activity"
    assert set(report.covered_acceptance_ids) == {"AC01", "AC02", "AC03", "AC04"}
    assert report.gaps == ()


# --- 未通过验收 / 未知形状 → 缺口 --------------------------------------------

def test_failed_acceptance_is_not_coverage() -> None:
    report = _coverage(_spec(TASK), {"overall": "FAIL"})

    assert report.covered_acceptance_ids == ()
    assert {g.acceptance_id for g in report.gaps} == {"AC01", "AC02"}


def test_unknown_shape_produces_bounded_explainable_gaps() -> None:
    report = _coverage(_report_spec(), {"overall": "PASS"})

    assert report.mainline is None
    assert [g.acceptance_id for g in report.gaps] == ["AC01"]
    gap = report.gaps[0]
    # 检索到候选但均不适配该 Profile → 明确区分“找到但不适配”（而非“真正没有方法”）
    assert gap.code == "candidate_incompatible"
    assert "不宜配" in gap.reason or "不适配" in gap.reason
    assert gap.mode == "agent_patch" and gap.task_id == "gap:AC01"
    # 路径有限且安全（相对、层级受限）
    assert gap.allowed_paths and all(
        p.startswith("backend/src/main/java/com/agentforge/template/") for p in gap.allowed_paths
    )


# --- 方法单元声明的覆盖（“已有适配方法” vs “仅名字相似”） --------------------

def test_compatible_method_declaring_acceptance_covers_it() -> None:
    # field-validator 声明 acceptance_ids=[AC01]，且与 demo-ts-mp 兼容 → AC01 视为已覆盖
    spec = _report_spec(method_requests=["field-validator"])
    report = _coverage(spec, {"overall": "PASS"})

    assert "AC01" in report.covered_acceptance_ids
    assert report.gaps == ()


def test_similar_named_unmatched_method_produces_safe_gap() -> None:
    # date-normalizer 名字相近但不声明任何验收 → AC01 仍为缺口
    spec = _report_spec(method_requests=["date-normalizer"])
    report = _coverage(spec, {"overall": "PASS"})

    assert report.covered_acceptance_ids == ()
    assert [g.acceptance_id for g in report.gaps] == ["AC01"]


# --- 生成图：coverage.json 与 coding 调用为 0 ---------------------------------

class _PassRunner:
    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


def _seed(factory, spec: SoftwareSpecification) -> int:
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        row = SpecVersion(project_id=project.id, version=1, body_json=canonical_json(spec),
                          hash=content_hash(spec), confirmed_at=datetime(2026, 1, 1))
        session.add(row)
        session.flush()
        project.active_spec_id = row.id
        return create_run(session, project, row.id, idempotency_key="k1",
                          spec_hash=content_hash(spec)).id


def test_generation_emits_coverage_and_makes_no_coding_calls(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed(factory, _spec(TASK))
    context = GenerationContext(session_factory=factory, runner=_PassRunner(),
                                work_root=tmp_path / "runs", registry=default_registry())

    result = run_generation(context, run_id)

    assert result["ok"] is True, result
    coverage = json.loads((tmp_path / "runs" / f"run-{run_id}" / "coverage.json").read_text("utf-8"))
    assert coverage["mainline"] == "mvp_task" and coverage["gaps"] == []
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"
        assert session.query(ModelCall).filter(ModelCall.model_role == "coding").count() == 0
        tasks = {t.task_id for t in session.query(RunTask).all()}
        assert "check_coverage" in tasks
