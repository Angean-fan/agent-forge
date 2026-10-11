"""M06 Software Specification schema 测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentforge_platform.spec import (
    SoftwareSpecification,
    confirmation_issues,
    is_confirmable,
)

SAMPLE_PATH = (
    Path(__file__).resolve().parents[2] / "schemas" / "task-management.spec.json"
)

CONTRACT_FIELDS = {
    "schema_version",
    "project_type",
    "goal",
    "actors",
    "entities",
    "relations",
    "permissions",
    "business_rules",
    "acceptance_cases",
    "technical_profile",
    "constraints",
    "exclusions",
    "capability_requests",
    "recipe_requests",
    "integrations",
    "external_requirements",
}


def _raw() -> dict:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


def _spec_with(**overrides) -> SoftwareSpecification:
    raw = _raw()
    raw.update(overrides)
    return SoftwareSpecification.model_validate(raw)


def test_sample_spec_is_valid_and_confirmable() -> None:
    spec = SoftwareSpecification.model_validate_json(SAMPLE_PATH.read_text(encoding="utf-8"))

    assert spec.schema_version == "0.1"
    assert len(spec.entities) == 1
    assert is_confirmable(spec), confirmation_issues(spec)


def test_sample_covers_all_contract_fields() -> None:
    assert CONTRACT_FIELDS <= set(_raw().keys())


def test_missing_entities_is_not_confirmable() -> None:
    spec = _spec_with(entities=[])

    assert not is_confirmable(spec)
    assert any("实体" in issue for issue in confirmation_issues(spec))


def test_missing_permissions_is_not_confirmable() -> None:
    spec = _spec_with(permissions=[])

    assert not is_confirmable(spec)
    assert any("权限" in issue for issue in confirmation_issues(spec))


def test_missing_business_rules_is_not_confirmable() -> None:
    spec = _spec_with(business_rules=[])

    assert not is_confirmable(spec)
    assert any("规则" in issue for issue in confirmation_issues(spec))


def test_unsupported_profile_is_not_confirmable() -> None:
    spec = _spec_with(
        technical_profile={
            "profile_id": "some-other-stack",
            "profile_version": "9.9",
            "template_version": "x",
        }
    )

    assert not is_confirmable(spec)
    assert any("Profile" in issue for issue in confirmation_issues(spec))


def test_multiple_entities_are_out_of_scope() -> None:
    entity = _raw()["entities"][0]
    spec = _spec_with(entities=[entity, {**entity, "name": "Project"}])

    assert not is_confirmable(spec)
    assert any("仅支持" in issue for issue in confirmation_issues(spec))


def test_pending_blocks_confirmation() -> None:
    spec = _spec_with(
        business_rules=[{"id": "BR01", "text": "标题必填", "source": "pending"}]
    )

    assert not is_confirmable(spec)
    assert any("pending" in issue for issue in confirmation_issues(spec))


def test_invalid_source_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec_with(business_rules=[{"id": "BR01", "text": "x", "source": "guess"}])


def test_unknown_top_level_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec_with(unexpected_field="surprise")


def test_unsupported_schema_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec_with(schema_version="9.9")


def test_missing_required_contract_field_is_rejected() -> None:
    raw = _raw()
    del raw["technical_profile"]

    with pytest.raises(ValidationError):
        SoftwareSpecification.model_validate(raw)
