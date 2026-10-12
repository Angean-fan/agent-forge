"""R10：第 2—3 个业务实体与关系映射（确定性一对多/多对一 + 外键）。

覆盖：契约父先子后 + 外键列；逐实体渲染与共享文件去重；装配产物含全部实体与外键 migration；
生成图端到端（多实体，假 Runner）；确认拒绝不可渲染的多实体规格；关系错误（环/自关联/未知实体）被拒。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from agentforge_platform.assembler import assemble
from agentforge_platform.asset_registry import default_registry
from agentforge_platform.capability_packs import build_params, render_pack_files
from agentforge_platform.contract import ContractError, compile_from_spec
from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.models import Project, Run, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.resolver import resolve_assets
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "capability-packs" / "task-crud-backend" / "0.2.0"

_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


def _entity(name: str, search_field: str, *, owner: bool = True) -> dict:
    fields = [
        {"name": "id", "type": "long", "required": True},
        {"name": search_field, "type": "string", "required": True},
        {"name": "status", "type": "string", "required": True},
        {"name": "created_at", "type": "datetime", "required": True},
    ]
    if owner:
        fields.insert(3, {"name": "owner_id", "type": "long", "required": True})
    return {"name": name, "source": "user_confirmed", "fields": fields}


def _multi_spec(**overrides) -> SoftwareSpecification:
    payload = {
        "schema_version": "0.1", "project_type": "crud_web",
        "goal": "校园活动报名：管理员发布活动，成员报名。",
        "actors": ["ADMIN", "MEMBER"],
        "entities": [_entity("Activity", "title"), _entity("Enrollment", "member_name")],
        "relations": [{"child": "Enrollment", "parent": "Activity", "kind": "many-to-one"}],
        "pages": [],
        "permissions": [
            {"role": "ADMIN", "actions": ["create", "read", "update", "delete"], "source": "user_confirmed"},
            {"role": "MEMBER", "actions": ["create", "read", "update", "delete"], "source": "user_confirmed"},
        ],
        "business_rules": [{"id": "BR01", "text": "成员只能访问自己的报名", "source": "user_confirmed"}],
        "acceptance_cases": [{"id": "AC01", "scenario": "成员创建报名", "expected": "成功", "source": "user_confirmed"}],
        "technical_profile": {"profile_id": "demo-ts-mp", "profile_version": "0.3", "template_version": "demo-0.2"},
    }
    payload.update(overrides)
    return SoftwareSpecification.model_validate(payload)


# --- 契约：父先子后 + 外键 -----------------------------------------------------

def test_contract_orders_parent_first_and_adds_foreign_key() -> None:
    contract = compile_from_spec(_multi_spec())

    assert [table.table for table in contract.tables] == ["activity", "enrollment"]
    enrollment = contract.tables[1]
    assert any(column.name == "activity_id" for column in enrollment.columns)
    assert ["activity_id"] in enrollment.indexes


def test_build_params_per_entity_migration_version_and_fk() -> None:
    contract = compile_from_spec(_multi_spec())

    parent = build_params(contract, "Activity")
    child = build_params(contract, "Enrollment")
    assert parent["migration_version"] == 2 and parent["foreign_keys_sql"] == ""
    assert child["migration_version"] == 3
    assert "FOREIGN KEY (activity_id) REFERENCES activity(id)" in child["foreign_keys_sql"]


def test_render_per_entity_produces_distinct_targets() -> None:
    contract = compile_from_spec(_multi_spec())

    activity = dict(render_pack_files(BACKEND, contract, entity_name="Activity"))
    enrollment = dict(render_pack_files(BACKEND, contract, entity_name="Enrollment"))

    assert any(target.endswith("entity/Activity.java") for target in activity)
    assert any(target.endswith("entity/Enrollment.java") for target in enrollment)
    # 共享文件（PageResult）两端内容一致，可去重。
    assert activity["src/main/java/com/agentforge/template/vo/PageResult.java"] == \
        enrollment["src/main/java/com/agentforge/template/vo/PageResult.java"]


# --- 装配产物 ----------------------------------------------------------------

def test_assembler_generates_all_entities_and_fk_migration(tmp_path: Path) -> None:
    spec = _multi_spec()
    contract = compile_from_spec(spec)
    lock = resolve_assets(default_registry(), "demo-ts-mp")

    assemble(contract, lock, default_registry(), tmp_path / "out", spec=spec)

    backend = tmp_path / "out" / "backend" / "src" / "main"
    java = backend / "java/com/agentforge/template"
    assert (java / "controller/ActivityController.java").is_file()
    assert (java / "controller/EnrollmentController.java").is_file()
    assert (java / "vo/PageResult.java").is_file()  # 共享文件仅一份
    migrations = backend / "resources/db/migration"
    assert (migrations / "V2__create_activity.sql").is_file()
    enrollment_sql = (migrations / "V3__create_enrollment.sql").read_text(encoding="utf-8")
    assert "FOREIGN KEY (activity_id) REFERENCES activity(id)" in enrollment_sql


# --- 关系错误被拒 ------------------------------------------------------------

def test_relation_errors_are_rejected() -> None:
    # 环：Activity→Enrollment 且 Enrollment→Activity
    with pytest.raises(ContractError) as cycle:
        compile_from_spec(_multi_spec(relations=[
            {"child": "Enrollment", "parent": "Activity"},
            {"child": "Activity", "parent": "Enrollment"},
        ]))
    assert cycle.value.code == "CONTRACT_UNSUPPORTED"

    with pytest.raises(ContractError) as unknown:
        compile_from_spec(_multi_spec(relations=[{"child": "Enrollment", "parent": "Missing"}]))
    assert unknown.value.code == "CONTRACT_UNSUPPORTED"

    with pytest.raises(ContractError) as selfref:
        compile_from_spec(_multi_spec(relations=[{"child": "Activity", "parent": "Activity"}]))
    assert selfref.value.code == "CONTRACT_UNSUPPORTED"


def test_unsupported_relation_kind_is_rejected() -> None:
    with pytest.raises(ContractError) as error:
        compile_from_spec(_multi_spec(relations=[
            {"child": "Enrollment", "parent": "Activity", "kind": "many-to-many"},
        ]))
    assert error.value.code == "CONTRACT_UNSUPPORTED"


# --- 生成图端到端（多实体） ---------------------------------------------------

class _PassRunner:
    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


def test_generation_graph_multi_entity_end_to_end(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    spec = _multi_spec()
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
    run_dir = tmp_path / "runs" / f"run-{run_id}" / "backend/src/main"
    assert (run_dir / "java/com/agentforge/template/controller/EnrollmentController.java").is_file()
    assert (run_dir / "resources/db/migration/V3__create_enrollment.sql").is_file()
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"


# --- 确认：不可渲染的多实体规格被拒 ------------------------------------------

def test_confirm_rejects_unrenderable_multi_entity(platform_app) -> None:
    from fastapi.testclient import TestClient

    app, _ = platform_app
    client = TestClient(app)
    assert client.post("/api/auth/login", json={"username": "alice", "password": "alice-secret"}).status_code == 200
    project_id = client.post(
        "/api/projects",
        json={"name": "活动", "requirement": "请帮我做一个校园活动报名管理系统，管理员可以发布活动，成员可以报名活动，成员只能查看自己的报名记录。"},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    ).json()["data"]["id"]
    spec = json.loads(canonical_json(_multi_spec()))
    # 去掉 Enrollment 的归属列 → 能力包无法渲染该实体
    enrollment = spec["entities"][1]
    enrollment["fields"] = [f for f in enrollment["fields"] if f["name"] != "owner_id"]
    client.put(
        f"/api/projects/{project_id}/spec",
        json={"version": 0, "draft_revision": 0, "spec": spec},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    )
    current = client.get(f"/api/projects/{project_id}/spec").json()["data"]

    response = client.post(
        f"/api/projects/{project_id}/confirm",
        json={"version": current["version"], "draft_revision": current["draft_revision"]},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SPEC_NOT_READY"
