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
from agentforge_platform.spec import SoftwareSpecification, confirmation_issues


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
    issues = confirmation_issues(spec)
    if not issues:
        # M23 将直接消费冻结规格；确认前先验证契约与固定 Task 能力包能接收它。
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


__all__ = [
    "canonical_json",
    "confirm_draft",
    "content_hash",
    "latest_spec",
    "load_spec",
    "put_draft",
]
