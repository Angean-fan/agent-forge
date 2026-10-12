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
    scope_issues,
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
    "pages",
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


def _entities(*names: str) -> list[dict]:
    base = _raw()["entities"][0]
    return [{**base, "name": name} for name in names]


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


def test_two_and_three_entities_are_within_scope() -> None:
    # R01：通用范围允许 1—3 个业务实体；2 实体不再被判"超范围"。
    for names in (("Task", "Project"), ("Task", "Project", "Book")):
        spec = _spec_with(entities=_entities(*names))
        assert scope_issues(spec) == []
        assert is_confirmable(spec), confirmation_issues(spec)


def test_four_entities_are_out_of_scope() -> None:
    spec = _spec_with(entities=_entities("Task", "Project", "Book", "Event"))

    assert any("业务实体最多" in issue for issue in scope_issues(spec))
    assert not is_confirmable(spec)


def test_three_roles_are_out_of_scope() -> None:
    spec = _spec_with(
        actors=["ADMIN", "MEMBER", "GUEST"],
        permissions=[
            {"role": "ADMIN", "actions": ["create", "read"]},
            {"role": "MEMBER", "actions": ["create", "read"]},
            {"role": "GUEST", "actions": ["read"]},
        ],
    )

    assert any("业务角色最多" in issue for issue in scope_issues(spec))


def test_two_roles_are_within_scope() -> None:
    spec = _spec_with(
        actors=["ADMIN", "MEMBER"],
        permissions=[
            {"role": "ADMIN", "actions": ["create", "read"]},
            {"role": "MEMBER", "actions": ["create", "read"]},
        ],
    )

    assert scope_issues(spec) == []


def test_nine_pages_are_out_of_scope() -> None:
    spec = _spec_with(pages=[{"name": f"P{i}"} for i in range(9)])

    assert any("业务页面最多" in issue for issue in scope_issues(spec))


def test_eight_pages_are_within_scope() -> None:
    spec = _spec_with(pages=[{"name": f"P{i}"} for i in range(8)])

    assert scope_issues(spec) == []


def test_unsafe_field_name_and_type_are_rejected() -> None:
    entity = _raw()["entities"][0]
    entity["fields"] = [
        {"name": "class", "type": "string", "required": True},
        {"name": "payload", "type": "json", "required": True},
    ]
    spec = _spec_with(entities=[entity])

    issues = confirmation_issues(spec)
    assert any("不安全的字段名" in issue for issue in issues)
    assert any("不支持的字段类型" in issue for issue in issues)


def test_unsupported_permission_action_is_rejected() -> None:
    spec = _spec_with(
        permissions=[{"role": "ADMIN", "actions": ["create", "read", "publish"]}]
    )

    assert any("不支持的权限操作" in issue for issue in confirmation_issues(spec))


def test_single_non_task_entity_passes_generic_confirmation() -> None:
    # 通用校验不再因"非 Task"拒绝；生成由 R10 能力门禁拦截（见 runs API 测试）。
    spec = _spec_with(entities=_entities("Book"))

    assert scope_issues(spec) == []
    assert confirmation_issues(spec) == []
    assert is_confirmable(spec)


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


def test_superseded_profile_version_requires_reconfirm() -> None:
    # 既有已确认 spec 引用的 Profile 版本被新版本取代后，不再"可确认"，须按新版本重新确认。
    spec = _spec_with(
        technical_profile={
            "profile_id": "demo-ts-mp",
            "profile_version": "0.2",
            "template_version": "demo-0.1",
        }
    )

    assert not is_confirmable(spec)
