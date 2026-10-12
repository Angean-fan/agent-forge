"""R11：唯一约束与状态守卫。

覆盖：关系 `unique` → 契约 `unique_constraints`；migration 渲染 `UNIQUE`；服务端状态守卫渲染；
`status_guards` 校验与哈希稳定性（为空不参与 canonical hash）；装配产物含约束测试与冲突顾问。
"""

from __future__ import annotations

import json

import pytest

from agentforge_platform.assembler import assemble
from agentforge_platform.asset_registry import default_registry
from agentforge_platform.capability_packs import build_params, render_pack_files
from agentforge_platform.contract import ContractError, compile_from_spec
from agentforge_platform.resolver import resolve_assets
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json

BACKEND = default_registry().get("capability-pack", "task-crud-backend", "0.3.0")[1]
MB_BACKEND = default_registry().get("capability-pack", "task-crud-backend-mb", "0.3.0")[1]


def _spec(**overrides) -> SoftwareSpecification:
    payload = {
        "schema_version": "0.1", "project_type": "crud_web", "goal": "校园活动报名。",
        "actors": ["ADMIN", "MEMBER"],
        "entities": [
            {"name": "Activity", "source": "user_confirmed", "fields": [
                {"name": "id", "type": "long", "required": True},
                {"name": "title", "type": "string", "required": True},
                {"name": "status", "type": "string", "required": True},
                {"name": "owner_id", "type": "long", "required": True},
                {"name": "created_at", "type": "datetime", "required": True}]},
            {"name": "Enrollment", "source": "user_confirmed", "fields": [
                {"name": "id", "type": "long", "required": True},
                {"name": "member_name", "type": "string", "required": True},
                {"name": "status", "type": "string", "required": True},
                {"name": "owner_id", "type": "long", "required": True},
                {"name": "created_at", "type": "datetime", "required": True}]},
        ],
        "relations": [{"child": "Enrollment", "parent": "Activity", "kind": "many-to-one", "unique": True}],
        "status_guards": [{"child": "Enrollment", "parent": "Activity", "blocked_status": ["CLOSED", "ENDED"]}],
        "permissions": [
            {"role": "ADMIN", "actions": ["create", "read", "update", "delete"], "source": "user_confirmed"},
            {"role": "MEMBER", "actions": ["create", "read", "update", "delete"], "source": "user_confirmed"}],
        "business_rules": [{"id": "BR01", "text": "同一成员-活动最多一条", "source": "user_confirmed"}],
        "acceptance_cases": [{"id": "AC01", "scenario": "重复报名被拒", "expected": "409", "source": "user_confirmed"}],
        "technical_profile": {"profile_id": "demo-ts-mp", "profile_version": "0.3", "template_version": "demo-0.2"},
    }
    payload.update(overrides)
    return SoftwareSpecification.model_validate(payload)


# --- 契约：唯一约束 -----------------------------------------------------------

def test_unique_relation_produces_unique_constraint() -> None:
    contract = compile_from_spec(_spec())

    enrollment = next(t for t in contract.tables if t.table == "enrollment")
    assert ["activity_id", "owner_id"] in enrollment.unique_constraints


def test_migration_renders_unique_constraint() -> None:
    spec = _spec()
    contract = compile_from_spec(spec)
    migration = dict(render_pack_files(BACKEND, contract, entity_name="Enrollment",
                                       status_guards=spec.status_guards))
    sql = migration["src/main/resources/db/migration/V3__create_enrollment.sql"]

    assert "FOREIGN KEY (activity_id) REFERENCES activity(id)" in sql
    assert "UNIQUE (activity_id, owner_id)" in sql


# --- 状态守卫渲染 -------------------------------------------------------------

def test_service_renders_server_side_status_guard() -> None:
    spec = _spec()
    contract = compile_from_spec(spec)
    rendered = dict(render_pack_files(BACKEND, contract, entity_name="Enrollment",
                                      status_guards=spec.status_guards))
    service = rendered["src/main/java/com/agentforge/template/service/EnrollmentService.java"]

    assert "checkStatusGuards" in service
    assert "activityMapper.selectById" in service
    assert '"CONFLICT"' in service and "409" in service
    assert '"CLOSED", "ENDED"' in service


def test_mybatis_service_renders_guard_with_find_by_id() -> None:
    spec = _spec()
    spec = SoftwareSpecification.model_validate({
        **json.loads(canonical_json(spec)),
        "technical_profile": {"profile_id": "demo-ts-mb", "profile_version": "0.1", "template_version": "demo-0.2-mb"},
    })
    contract = compile_from_spec(spec)
    rendered = dict(render_pack_files(MB_BACKEND, contract, entity_name="Enrollment",
                                      status_guards=spec.status_guards))
    service = rendered["src/main/java/com/agentforge/template/service/EnrollmentService.java"]

    assert "activityMapper.findById" in service and "checkStatusGuards" in service


def test_status_guard_validation_rejects_bad_declarations() -> None:
    # 未知父实体
    with pytest.raises(ContractError) as unknown:
        compile_from_spec(_spec(status_guards=[{"child": "Enrollment", "parent": "Missing", "blocked_status": ["X"]}]))
    assert unknown.value.code == "CONTRACT_UNSUPPORTED"

    # 子实体未与父实体建立外键
    with pytest.raises(ContractError) as no_fk:
        compile_from_spec(_spec(relations=[], status_guards=[
            {"child": "Enrollment", "parent": "Activity", "blocked_status": ["X"]},
        ]))
    assert no_fk.value.code == "CONTRACT_UNSUPPORTED"

    # 禁止状态为空
    with pytest.raises(ContractError) as empty:
        compile_from_spec(_spec(status_guards=[{"child": "Enrollment", "parent": "Activity", "blocked_status": []}]))
    assert empty.value.code == "CONTRACT_UNSUPPORTED"


# --- 哈希稳定性 ---------------------------------------------------------------

def test_status_guards_omitted_from_hash_when_empty() -> None:
    base = _spec(status_guards=[])
    plain = SoftwareSpecification.model_validate(
        {k: v for k, v in json.loads(canonical_json(base)).items()}
    )
    # 空 status_guards 不进入 canonical hash → 与不含该字段的规格同哈希
    assert canonical_json(base) == canonical_json(plain)
    assert "status_guards" not in canonical_json(base)


def test_build_params_exposes_constraint_test_context() -> None:
    spec = _spec()
    contract = compile_from_spec(spec)
    params = build_params(contract, "Enrollment", status_guards=spec.status_guards)

    assert "UNIQUE (activity_id, owner_id)" in params["unique_constraints_sql"]
    assert params["has_unique_constraint"] is True
    assert params["constraint_test"]["fk_field"] == "activity_id"
    assert params["constraint_test"]["parent_table"] == "activity"
    assert params["status_guards"][0]["blocked_first"] == "CLOSED"


# --- 装配产物 ----------------------------------------------------------------

def test_assembler_renders_constraint_test_and_advice(tmp_path) -> None:
    spec = _spec()
    contract = compile_from_spec(spec)
    lock = resolve_assets(default_registry(), "demo-ts-mp")

    assemble(contract, lock, default_registry(), tmp_path / "out", spec=spec)

    java = tmp_path / "out" / "backend" / "src" / "main" / "java/com/agentforge/template"
    assert (java / "exception/ConstraintViolationAdvice.java").is_file()
    test_dir = tmp_path / "out" / "backend" / "src" / "test/java/com/agentforge/template"
    constraint_test = (test_dir / "EnrollmentConstraintTest.java").read_text(encoding="utf-8")
    assert "duplicateParentOwnerIsRejected" in constraint_test
    assert "blockedParentStatusIsRejected" in constraint_test
