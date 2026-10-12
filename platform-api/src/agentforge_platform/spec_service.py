"""规格草稿的持久化与确认（M07）。

- 每个项目最多一个草稿（未确认）版本；确认后不可原地修改；
- 编辑已确认规格会创建新草稿/新版本；
- hash 为规范化 JSON 的 SHA-256，内容相同则稳定一致。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.models import Project, SpecVersion
from agentforge_platform.spec import (
    SoftwareSpecification,
    confirmation_issues,
    is_legacy_task_spec,
    scope_issues,
)


def canonical_json(spec: SoftwareSpecification) -> str:
    """规范化 JSON：键排序、紧凑分隔符，保证同内容 hash 稳定。"""
    return json.dumps(
        spec.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def content_hash(spec: SoftwareSpecification) -> str:
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def load_spec(row: SpecVersion) -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(row.body_json)


def latest_spec(db: Session, project_id: int) -> SpecVersion | None:
    return db.scalars(
        select(SpecVersion)
        .where(SpecVersion.project_id == project_id)
        .order_by(SpecVersion.version.desc())
    ).first()


def spec_version(db: Session, project_id: int, version: int) -> SpecVersion | None:
    """按项目 + 版本号精确取一份规格版本（用于版本差异比对）。"""
    return db.scalars(
        select(SpecVersion).where(
            SpecVersion.project_id == project_id, SpecVersion.version == version
        )
    ).first()


def put_draft(
    db: Session, project: Project, expected_version: int, spec: SoftwareSpecification,
    *, expected_revision: int | None = None,
) -> SpecVersion:
    """创建或编辑草稿；版本不匹配返回 409；编辑已确认规格则新建版本。"""
    latest = latest_spec(db, project.id)
    current = latest.version if latest is not None else 0
    if expected_version != current:
        raise AppException(
            ErrorCode.SPEC_CONFLICT,
            f"规格版本冲突：期望 {expected_version}，当前 {current}。",
            status_code=409,
        )
    current_revision = latest.draft_revision if latest is not None else 0
    if expected_revision is not None and expected_revision != current_revision:
        raise AppException(
            ErrorCode.SPEC_CONFLICT, "草稿已被修改，请重新读取。", status_code=409
        )

    body_json = canonical_json(spec)
    spec_hash = content_hash(spec)

    if latest is None:
        row = SpecVersion(
            project_id=project.id, version=1, body_json=body_json, hash=spec_hash
        )
        db.add(row)
    elif latest.confirmed_at is None:
        result = db.execute(
            update(SpecVersion)
            .where(
                SpecVersion.id == latest.id,
                SpecVersion.draft_revision == current_revision,
                SpecVersion.confirmed_at.is_(None),
            )
            .values(
                body_json=body_json,
                hash=spec_hash,
                draft_revision=current_revision + 1,
                summary=None,
                missing_fields_json="[]",
                questions_json="[]",
            )
        )
        if result.rowcount != 1:
            raise AppException(ErrorCode.SPEC_CONFLICT, "草稿已被修改，请重新读取。", status_code=409)
        row = latest
    else:
        row = SpecVersion(
            project_id=project.id,
            version=latest.version + 1,
            body_json=body_json,
            hash=spec_hash,
        )
        db.add(row)

    db.flush()
    db.refresh(row)
    return row


def confirm_draft(
    db: Session, project: Project, expected_version: int, *, expected_revision: int | None = None
) -> SpecVersion:
    """确认并冻结草稿；无草稿 404、版本冲突 409、未就绪 422。"""
    latest = latest_spec(db, project.id)
    if latest is None or latest.confirmed_at is not None:
        raise AppException(
            ErrorCode.SPEC_NOT_FOUND, "没有待确认的规格草稿。", status_code=404
        )
    if expected_version != latest.version:
        raise AppException(
            ErrorCode.SPEC_CONFLICT,
            f"规格版本冲突：期望 {expected_version}，当前 {latest.version}。",
            status_code=409,
        )
    if expected_revision is not None and expected_revision != latest.draft_revision:
        raise AppException(ErrorCode.SPEC_CONFLICT, "草稿已被修改，请重新读取。", status_code=409)

    spec = load_spec(latest)
    scope = scope_issues(spec)
    if scope:
        raise AppException(
            ErrorCode.UNSUPPORTED_SCOPE,
            "超出 Demo 范围：" + "；".join(scope) + "。",
            status_code=422,
        )
    issues = confirmation_issues(spec)
    if not issues and is_legacy_task_spec(spec):
        # 旧 Task 单实体路径：确认前先验证契约与固定能力包能接收它。
        # 合法多实体候选可确认但不在此编译（生成入口按 R10 能力门禁拦截）。
        from agentforge_platform.capability_packs import build_params
        from agentforge_platform.contract import compile_from_spec

        try:
            build_params(compile_from_spec(spec))
        except AppException as exc:
            issues.append(exc.message)
    if issues:
        raise AppException(
            ErrorCode.SPEC_NOT_READY, "规格尚未就绪：" + "；".join(issues), status_code=422
        )

    result = db.execute(
        update(SpecVersion)
        .where(
            SpecVersion.id == latest.id,
            SpecVersion.draft_revision == latest.draft_revision,
            SpecVersion.confirmed_at.is_(None),
        )
        .values(confirmed_at=datetime.now(UTC).replace(tzinfo=None))
    )
    if result.rowcount != 1:
        raise AppException(ErrorCode.SPEC_CONFLICT, "草稿已被修改，请重新读取。", status_code=409)
    project.active_spec_id = latest.id
    db.flush()
    db.refresh(latest)
    return latest


def frozen_spec_issues(row: SpecVersion) -> list[str]:
    """R06 冻结完整性检查：重算 hash + 独立验收场景/业务规则结构完整性。

    若冻结（已确认）规格的 body 被原地篡改（含验收断言），重算 hash 将不等于冻结值，
    或结构校验失败，从而在**生成前**暴露篡改。
    """
    try:
        spec = load_spec(row)
    except Exception:  # noqa: BLE001  被改坏成非法 JSON/结构
        return ["冻结规格无法按 schema 解析（疑似被篡改）"]
    issues: list[str] = []
    if content_hash(spec) != row.hash:
        issues.append("冻结规格内容与冻结 hash 不一致（疑似被篡改）")
    seen_cases: set[str] = set()
    for case in spec.acceptance_cases:
        if not case.id.strip() or not case.scenario.strip() or not case.expected.strip():
            issues.append(f"验收场景 {case.id!r} 缺少 id/场景/期望")
        if case.id in seen_cases:
            issues.append(f"验收场景 id 重复：{case.id}")
        seen_cases.add(case.id)
    seen_rules: set[str] = set()
    for rule in spec.business_rules:
        if not rule.id.strip() or not rule.text.strip():
            issues.append(f"业务规则 {rule.id!r} 缺少 id/文本")
        if rule.id in seen_rules:
            issues.append(f"业务规则 id 重复：{rule.id}")
        seen_rules.add(rule.id)
    return issues


def require_frozen_spec(row: SpecVersion) -> SoftwareSpecification:
    """校验冻结规格未被篡改；否则抛 ``SPEC_TAMPERED``（409）并给出可追溯理由。"""
    issues = frozen_spec_issues(row)
    if issues:
        raise AppException(
            ErrorCode.SPEC_TAMPERED,
            "冻结规格完整性校验未通过：" + "；".join(issues),
            status_code=409,
        )
    return load_spec(row)


def _entity_view(spec: SoftwareSpecification) -> dict[str, dict]:
    return {
        entity.name: {
            "source": entity.source.value,
            "fields": sorted(
                (
                    {"name": field.name, "type": field.type, "required": field.required}
                    for field in entity.fields
                ),
                key=lambda item: item["name"],
            ),
        }
        for entity in spec.entities
    }


def _page_view(spec: SoftwareSpecification) -> dict[str, dict]:
    return {
        page.name: {"entity": page.entity, "path": page.path} for page in spec.pages
    }


def _permission_view(spec: SoftwareSpecification) -> dict[str, dict]:
    return {
        permission.role: {
            "source": permission.source.value,
            "actions": sorted(permission.actions),
        }
        for permission in spec.permissions
    }


def _rule_view(spec: SoftwareSpecification) -> dict[str, dict]:
    return {
        rule.id: {"text": rule.text, "source": rule.source.value}
        for rule in spec.business_rules
    }


def _case_view(spec: SoftwareSpecification) -> dict[str, dict]:
    return {
        case.id: {
            "scenario": case.scenario,
            "expected": case.expected,
            "source": case.source.value,
        }
        for case in spec.acceptance_cases
    }


def _diff_keyed(section: str, before: dict[str, object], after: dict[str, object]) -> list[dict]:
    changes: list[dict] = []
    for key in sorted(set(before) | set(after)):
        if key not in before:
            changes.append({"section": section, "key": key, "kind": "added",
                            "before": None, "after": after[key]})
        elif key not in after:
            changes.append({"section": section, "key": key, "kind": "removed",
                            "before": before[key], "after": None})
        elif before[key] != after[key]:
            changes.append({"section": section, "key": key, "kind": "changed",
                            "before": before[key], "after": after[key]})
    return changes


def _diff_scalar(section: str, before: object, after: object) -> list[dict]:
    if before == after:
        return []
    return [{"section": section, "key": section, "kind": "changed",
             "before": before, "after": after}]


def _diff_items(section: str, before: list[str], after: list[str]) -> list[dict]:
    changes: list[dict] = []
    before_set, after_set = set(before), set(after)
    for item in sorted(after_set - before_set):
        changes.append({"section": section, "key": item, "kind": "added",
                        "before": None, "after": item})
    for item in sorted(before_set - after_set):
        changes.append({"section": section, "key": item, "kind": "removed",
                        "before": item, "after": None})
    return changes


def spec_diff(from_row: SpecVersion, to_row: SpecVersion) -> dict:
    """两份规格版本的结构化差异（功能/排除项/业务规则/验收场景等）。

    每项变更以 ``section/key/kind(added|removed|changed)/before/after`` 表达，便于前端展示与追溯。
    """
    before = load_spec(from_row)
    after = load_spec(to_row)
    changes: list[dict] = []
    changes += _diff_scalar("goal", before.goal, after.goal)
    changes += _diff_items("actors", before.actors, after.actors)
    changes += _diff_keyed("entities", _entity_view(before), _entity_view(after))
    changes += _diff_keyed("pages", _page_view(before), _page_view(after))
    changes += _diff_keyed("permissions", _permission_view(before), _permission_view(after))
    changes += _diff_keyed("business_rules", _rule_view(before), _rule_view(after))
    changes += _diff_keyed("acceptance_cases", _case_view(before), _case_view(after))
    changes += _diff_items("constraints", before.constraints, after.constraints)
    changes += _diff_items("exclusions", before.exclusions, after.exclusions)
    return {
        "from": {"version": from_row.version, "hash": from_row.hash},
        "to": {"version": to_row.version, "hash": to_row.hash},
        "changes": changes,
    }


__all__ = [
    "canonical_json",
    "confirm_draft",
    "content_hash",
    "frozen_spec_issues",
    "latest_spec",
    "load_spec",
    "put_draft",
    "require_frozen_spec",
    "spec_diff",
    "spec_version",
]
