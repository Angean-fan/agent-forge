"""版本化 Software Specification schema（M06）。

按技术文档第 4 节定义契约字段；MVP 仅支持 1 个业务实体与固定技术 Profile。
推断/不确定项以 ``source=pending`` 标注；冻结前关键 ``pending`` 必须为零。
契约字段不得删去；暂不使用的字段以空数组表示；不接受超范围输入后静默裁剪。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

SPEC_SCHEMA_VERSION = "0.1"
SUPPORTED_SCHEMA_VERSIONS = frozenset({SPEC_SCHEMA_VERSION})
MVP_PROFILE_ID = "demo-ts-mp"
MVP_PROFILE_VERSION = "0.1"
MVP_ENTITY_LIMIT = 1

_STRICT = ConfigDict(extra="forbid")


class Source(StrEnum):
    """需求项来源。"""

    USER_EXPLICIT = "user_explicit"
    USER_CONFIRMED = "user_confirmed"
    PENDING = "pending"


class Field(BaseModel):
    model_config = _STRICT

    name: str
    type: str
    required: bool = True


class Entity(BaseModel):
    model_config = _STRICT

    name: str
    fields: list[Field] = []
    source: Source = Source.PENDING


class Permission(BaseModel):
    model_config = _STRICT

    role: str
    actions: list[str] = []
    source: Source = Source.PENDING


class BusinessRule(BaseModel):
    model_config = _STRICT

    id: str
    text: str
    source: Source = Source.PENDING


class AcceptanceCase(BaseModel):
    model_config = _STRICT

    id: str
    scenario: str
    expected: str
    source: Source = Source.PENDING


class TechnicalProfile(BaseModel):
    model_config = _STRICT

    profile_id: str
    profile_version: str
    template_version: str


class SoftwareSpecification(BaseModel):
    model_config = _STRICT

    schema_version: str
    project_type: str
    goal: str
    actors: list[str] = []
    entities: list[Entity] = []
    relations: list[dict[str, Any]] = []
    permissions: list[Permission] = []
    business_rules: list[BusinessRule] = []
    acceptance_cases: list[AcceptanceCase] = []
    technical_profile: TechnicalProfile
    constraints: list[str] = []
    exclusions: list[str] = []
    capability_requests: list[dict[str, Any]] = []
    recipe_requests: list[dict[str, Any]] = []
    integrations: list[dict[str, Any]] = []
    external_requirements: list[dict[str, Any]] = []

    @field_validator("schema_version")
    @classmethod
    def _known_schema_version(cls, value: str) -> str:
        if value not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported schema_version: {value!r}")
        return value


def is_supported_profile(profile: TechnicalProfile) -> bool:
    return (
        profile.profile_id == MVP_PROFILE_ID
        and profile.profile_version == MVP_PROFILE_VERSION
    )


def critical_pending_fields(spec: SoftwareSpecification) -> list[str]:
    """返回关键项中处于 ``pending`` 的标注（实体/权限/业务规则/验收场景）。"""
    pending: list[str] = []
    pending += [f"entity:{e.name}" for e in spec.entities if e.source is Source.PENDING]
    pending += [
        f"permission:{p.role}" for p in spec.permissions if p.source is Source.PENDING
    ]
    pending += [
        f"business_rule:{r.id}" for r in spec.business_rules if r.source is Source.PENDING
    ]
    pending += [
        f"acceptance_case:{c.id}"
        for c in spec.acceptance_cases
        if c.source is Source.PENDING
    ]
    return pending


def confirmation_issues(spec: SoftwareSpecification) -> list[str]:
    """返回阻止确认的问题列表；为空表示可确认。"""
    issues: list[str] = []

    if len(spec.entities) == 0:
        issues.append("缺少业务实体")
    elif len(spec.entities) > MVP_ENTITY_LIMIT:
        issues.append(
            f"MVP 仅支持 {MVP_ENTITY_LIMIT} 个业务实体，收到 {len(spec.entities)} 个"
        )

    if not spec.permissions:
        issues.append("缺少角色权限定义")
    if not spec.business_rules:
        issues.append("缺少关键业务规则")
    if not is_supported_profile(spec.technical_profile):
        issues.append(
            "不支持的技术 Profile: "
            f"{spec.technical_profile.profile_id}@{spec.technical_profile.profile_version}"
        )

    pending = critical_pending_fields(spec)
    if pending:
        issues.append("存在未确认的 pending 项：" + "、".join(pending))

    return issues


def is_confirmable(spec: SoftwareSpecification) -> bool:
    return not confirmation_issues(spec)
