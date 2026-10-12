"""R12：校园活动主线（角色、页面与固定业务验收）+ Finder 审计入 run 报告。

覆盖：按实体权限（成员只读活动、可创建报名）；校园规格确认/装配产物角色门禁与迁移；
生成图产出 asset-search.json（候选方法/复用方法/未覆盖规则）与事件；reject 非法 entity_permissions；哈希稳定。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from agentforge_platform.assembler import assemble
from agentforge_platform.asset_registry import default_registry
from agentforge_platform.contract import ContractError, compile_from_spec, plural_of
from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.models import Project, Run, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.resolver import resolve_assets
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
CAMPUS = REPO / "schemas" / "campus-activity.spec.json"

_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


def _campus() -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(CAMPUS.read_text(encoding="utf-8"))


# --- 按实体权限 ---------------------------------------------------------------

def test_plural_of() -> None:
    assert plural_of("activity") == "activities"
    assert plural_of("task") == "tasks"
    assert plural_of("enrollment") == "enrollments"


def test_entity_permissions_gate_roles_per_entity() -> None:
    contract = compile_from_spec(_campus())

    activity = next(m for m in contract.permissions if m.entity == "Activity")
    enrollment = next(m for m in contract.permissions if m.entity == "Enrollment")
    activity_roles = {r.role: set(r.actions) for r in activity.roles}
    enrollment_roles = {r.role: set(r.actions) for r in enrollment.roles}

    assert activity_roles["MEMBER"] == {"read"}          # 成员不得发布
    assert "create" in activity_roles["ADMIN"]
    assert "create" in enrollment_roles["MEMBER"]        # 成员可报名
    assert "create" not in enrollment_roles["ADMIN"]

    create_route = next(
        r for r in contract.routes if r.path == "/api/activities" and r.operation == "create"
    )
    assert create_route.allowed_roles == ["ADMIN"]


def test_unknown_entity_permission_is_rejected() -> None:
    payload = json.loads(CAMPUS.read_text(encoding="utf-8"))
    payload["entity_permissions"].append({"entity": "Missing", "role": "ADMIN", "actions": ["read"]})

    with pytest.raises(ContractError) as error:
        compile_from_spec(SoftwareSpecification.model_validate(payload))
    assert error.value.code == "CONTRACT_UNSUPPORTED"


def test_empty_entity_permissions_are_omitted_from_hash() -> None:
    payload = json.loads(CAMPUS.read_text(encoding="utf-8"))
    payload["entity_permissions"] = []
    spec = SoftwareSpecification.model_validate(payload)

    assert "entity_permissions" not in canonical_json(spec)


# --- 装配产物 ----------------------------------------------------------------

def test_campus_assembly_has_role_gates_and_migrations(tmp_path: Path) -> None:
    spec = _campus()
    contract = compile_from_spec(spec)
    lock = resolve_assets(default_registry(), "demo-ts-mp")

    assemble(contract, lock, default_registry(), tmp_path / "out", spec=spec)

    java = tmp_path / "out" / "backend" / "src" / "main" / "java/com/agentforge/template"
    activity = (java / "controller/ActivityController.java").read_text(encoding="utf-8")
    assert '@RequestMapping("/api/activities")' in activity
    assert 'List.of("ADMIN")' in activity  # 发布仅管理员
    migration_dir = tmp_path / "out" / "backend" / "src" / "main" / "resources/db/migration"
    assert (migration_dir / "V2__create_activity.sql").is_file()
    assert (migration_dir / "V3__create_enrollment.sql").is_file()


# --- 生成图：Finder 审计入报告 ------------------------------------------------

class _PassRunner:
    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


def test_generation_emits_finder_audit(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
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
        run_id = run.id

    context = GenerationContext(
        session_factory=factory, runner=_PassRunner(),
        work_root=tmp_path / "runs", registry=default_registry(),
    )
    result = run_generation(context, run_id)

    assert result["ok"] is True
    audit = json.loads((tmp_path / "runs" / f"run-{run_id}" / "asset-search.json").read_text("utf-8"))
    assert audit["query_hash"] and audit["match_type"]
    assert "capability-pack:task-crud-backend@0.3.0" in audit["selected_refs"]
    assert set(audit["uncovered_acceptance_ids"]) == {"AC01", "AC02", "AC03", "AC04"}
    assert audit["reused_method_units"] == []
    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        assert run.status == "SUCCEEDED"
