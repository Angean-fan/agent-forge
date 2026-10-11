"""M10 业务契约编译器测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentforge_platform.contract import (
    BusinessContract,
    ContractError,
    compile_from_spec,
)
from agentforge_platform.spec import SoftwareSpecification

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"
SAMPLE_SPEC = SCHEMAS_DIR / "task-management.spec.json"
COMMITTED_CONTRACT = SCHEMAS_DIR / "task-management.contract.json"


def _spec() -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(SAMPLE_SPEC.read_text(encoding="utf-8"))


def _spec_with(**overrides) -> SoftwareSpecification:
    raw = json.loads(SAMPLE_SPEC.read_text(encoding="utf-8"))
    raw.update(overrides)
    return SoftwareSpecification.model_validate(raw)


def _entity(fields: list[dict]) -> list[dict]:
    return [{"name": "Task", "source": "user_confirmed", "fields": fields}]


def test_compiles_task_contract() -> None:
    contract = compile_from_spec(_spec())

    assert contract.schema_version == "0.1"
    assert contract.profile_ref == "demo-ts-mp@0.3"
    assert [t.table for t in contract.tables] == ["task"]
    assert {c.name for c in contract.tables[0].columns} == {
        "id", "title", "description", "status", "owner_id", "created_at"
    }
    assert [r.operation for r in contract.routes] == [
        "list", "create", "get", "update", "delete"
    ]
    assert {d.name for d in contract.dtos} == {
        "TaskCreateRequest", "TaskUpdateRequest", "TaskView"
    }
    assert contract.permissions[0].entity == "Task"
    assert [r.role for r in contract.permissions[0].roles] == ["ADMIN", "MEMBER"]
    assert [e.code for e in contract.error_codes] == [
        "VALIDATION_ERROR", "UNAUTHORIZED", "FORBIDDEN", "NOT_FOUND", "CONFLICT", "INTERNAL_ERROR"
    ]
    assert contract.contract_hash


def test_create_request_excludes_server_managed_fields() -> None:
    contract = compile_from_spec(_spec())
    create = next(d for d in contract.dtos if d.name == "TaskCreateRequest")

    names = [f.name for f in create.fields]
    assert names == ["title", "description", "status"]
    assert "owner_id" not in names
    assert "id" not in names


def test_contract_hash_is_stable_for_same_spec() -> None:
    assert compile_from_spec(_spec()).contract_hash == compile_from_spec(_spec()).contract_hash


def test_committed_contract_matches_compiler_output() -> None:
    compiled = compile_from_spec(_spec())
    committed = BusinessContract.model_validate_json(
        COMMITTED_CONTRACT.read_text(encoding="utf-8")
    )

    assert compiled.model_dump() == committed.model_dump()


def test_unsupported_field_type_is_rejected() -> None:
    spec = _spec_with(
        entities=_entity(
            [{"name": "id", "type": "long", "required": True},
             {"name": "payload", "type": "uuid", "required": True}]
        )
    )

    with pytest.raises(ContractError) as error:
        compile_from_spec(spec)

    assert error.value.code == "CONTRACT_UNSUPPORTED"
    assert "uuid" in error.value.message


def test_duplicate_column_is_rejected() -> None:
    spec = _spec_with(
        entities=_entity(
            [{"name": "id", "type": "long", "required": True},
             {"name": "userName", "type": "string", "required": True},
             {"name": "user_name", "type": "string", "required": True}]
        )
    )

    with pytest.raises(ContractError) as error:
        compile_from_spec(spec)

    assert error.value.code == "CONTRACT_CONFLICT"


def test_missing_primary_key_is_rejected() -> None:
    spec = _spec_with(
        entities=_entity([{"name": "title", "type": "string", "required": True}])
    )

    with pytest.raises(ContractError) as error:
        compile_from_spec(spec)

    assert error.value.code == "CONTRACT_UNSUPPORTED"


def test_multiple_entities_are_rejected() -> None:
    first = _spec_with().entities[0].model_dump()
    spec = _spec_with(entities=[first, {**first, "name": "Project"}])

    with pytest.raises(ContractError) as error:
        compile_from_spec(spec)

    assert error.value.code == "CONTRACT_UNSUPPORTED"


def test_duplicate_role_permission_is_rejected() -> None:
    spec = _spec_with(
        permissions=[
            {"role": "ADMIN", "actions": ["read"], "source": "user_confirmed"},
            {"role": "ADMIN", "actions": ["create"], "source": "user_confirmed"},
        ]
    )

    with pytest.raises(ContractError) as error:
        compile_from_spec(spec)

    assert error.value.code == "CONTRACT_CONFLICT"
