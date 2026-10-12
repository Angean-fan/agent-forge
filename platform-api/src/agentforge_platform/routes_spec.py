"""规格路由：读取/编辑草稿与确认冻结。"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update
from sqlalchemy.orm import Session

from agentforge_platform.auth import require_csrf, require_project_owner
from agentforge_platform.db import get_db
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.final_summary import generate_final_summary
from agentforge_platform.middleware import get_request_id
from agentforge_platform.models import Project, SpecVersion
from agentforge_platform.requirement_extraction import extract_requirement
from agentforge_platform.responses import ApiResponse
from agentforge_platform.spec import (
    SoftwareSpecification,
    confirmation_issues,
    critical_pending_fields,
)
from agentforge_platform.spec_service import (
    confirm_draft,
    latest_spec,
    load_spec,
    put_draft,
    spec_diff,
    spec_version,
)

router = APIRouter(prefix="/api/projects", tags=["spec"])
_STRICT = ConfigDict(extra="forbid")


class SpecPutRequest(BaseModel):
    version: int = Field(ge=0)
    draft_revision: int = Field(ge=0)
    spec: SoftwareSpecification


class SpecConfirmRequest(BaseModel):
    version: int = Field(ge=0)
    draft_revision: int = Field(ge=0)


class SpecExtractRequest(BaseModel):
    version: int = Field(ge=0)


class SpecSummaryRequest(BaseModel):
    version: int = Field(ge=0)
    draft_revision: int = Field(ge=0)


class SpecSummaryView(BaseModel):
    """最终摘要 + **实际使用**的思考模式（不得谎称使用了未验证的强度）。"""

    model_config = _STRICT

    spec_id: int
    version: int
    draft_revision: int
    summary: str
    thinking_mode: str
    reasoning_effort: str | None


class SpecView(BaseModel):
    spec_id: int
    version: int
    draft_revision: int
    confirmed: bool
    confirmed_at: datetime | None
    hash: str
    spec: SoftwareSpecification
    pending: list[str]
    issues: list[str]
    summary: str | None = None
    missing_fields: list[str] = []
    questions: list[str] = []


class SpecConfirmView(BaseModel):
    spec_id: int
    version: int
    draft_revision: int
    hash: str
    confirmed_at: datetime


def _view(row: SpecVersion) -> SpecView:
    spec = load_spec(row)
    return SpecView(
        spec_id=row.id,
        version=row.version,
        draft_revision=row.draft_revision,
        confirmed=row.confirmed_at is not None,
        confirmed_at=row.confirmed_at,
        hash=row.hash,
        spec=spec,
        pending=critical_pending_fields(spec),
        issues=confirmation_issues(spec),
        summary=row.summary,
        missing_fields=json.loads(row.missing_fields_json),
        questions=json.loads(row.questions_json),
    )


@router.get("/{project_id}/spec", response_model=ApiResponse[SpecView])
def get_spec(
    request: Request,
    project: Project = Depends(require_project_owner),
    db: Session = Depends(get_db),
) -> ApiResponse[SpecView]:
    latest = latest_spec(db, project.id)
    if latest is None:
        raise AppException(ErrorCode.SPEC_NOT_FOUND, "尚未创建规格草稿。", status_code=404)
    return ApiResponse(request_id=get_request_id(request), data=_view(latest))


@router.put("/{project_id}/spec", response_model=ApiResponse[SpecView])
def put_spec(
    payload: SpecPutRequest,
    request: Request,
    project: Project = Depends(require_project_owner),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApiResponse[SpecView]:
    row = put_draft(
        db, project, payload.version, payload.spec, expected_revision=payload.draft_revision
    )
    return ApiResponse(request_id=get_request_id(request), data=_view(row))


@router.post("/{project_id}/extract", response_model=ApiResponse[SpecView])
def extract_spec(
    payload: SpecExtractRequest,
    request: Request,
    project: Project = Depends(require_project_owner),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApiResponse[SpecView]:
    row = extract_requirement(db, project, payload.version)
    return ApiResponse(request_id=get_request_id(request), data=_view(row))


@router.post("/{project_id}/confirm", response_model=ApiResponse[SpecConfirmView])
def confirm_spec(
    payload: SpecConfirmRequest,
    request: Request,
    project: Project = Depends(require_project_owner),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApiResponse[SpecConfirmView]:
    row = confirm_draft(db, project, payload.version, expected_revision=payload.draft_revision)
    return ApiResponse(
        request_id=get_request_id(request),
        data=SpecConfirmView(
            spec_id=row.id,
            version=row.version,
            draft_revision=row.draft_revision,
            hash=row.hash,
            confirmed_at=row.confirmed_at,
        ),
    )


@router.post("/{project_id}/summary", response_model=ApiResponse[SpecSummaryView])
def summarize_spec(
    payload: SpecSummaryRequest,
    request: Request,
    project: Project = Depends(require_project_owner),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApiResponse[SpecSummaryView]:
    """R06：用 DeepSeek 整理面向用户的最终需求摘要（仅对未冻结草稿，且无 pending）。

    该摘要是**起草**，不代替用户确认：不改动任何 ``source``；确认冻结仍走 ``/confirm``。
    """
    latest = latest_spec(db, project.id)
    if latest is None:
        raise AppException(ErrorCode.SPEC_NOT_FOUND, "尚未创建规格草稿。", status_code=404)
    if latest.confirmed_at is not None:
        raise AppException(
            ErrorCode.SPEC_CONFLICT, "已冻结的规格不能再生成最终摘要；请先编辑生成新草稿。",
            status_code=409,
        )
    if payload.version != latest.version:
        raise AppException(
            ErrorCode.SPEC_CONFLICT,
            f"规格版本冲突：期望 {payload.version}，当前 {latest.version}。",
            status_code=409,
        )
    if payload.draft_revision != latest.draft_revision:
        raise AppException(ErrorCode.SPEC_CONFLICT, "草稿已被修改，请重新读取。", status_code=409)

    spec = load_spec(latest)
    pending = critical_pending_fields(spec)
    if pending:
        raise AppException(
            ErrorCode.SPEC_NOT_READY,
            "存在未确认的 pending 项，无法生成最终摘要：" + "、".join(pending),
            status_code=422,
        )

    summary, thinking_mode, reasoning_effort = generate_final_summary(db, project, latest)
    result = db.execute(
        update(SpecVersion)
        .where(
            SpecVersion.id == latest.id,
            SpecVersion.draft_revision == latest.draft_revision,
            SpecVersion.confirmed_at.is_(None),
        )
        .values(summary=summary)
    )
    if result.rowcount != 1:
        raise AppException(ErrorCode.SPEC_CONFLICT, "草稿已被修改，请重新读取。", status_code=409)
    db.flush()
    db.refresh(latest)
    return ApiResponse(
        request_id=get_request_id(request),
        data=SpecSummaryView(
            spec_id=latest.id,
            version=latest.version,
            draft_revision=latest.draft_revision,
            summary=summary,
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
        ),
    )


@router.get("/{project_id}/spec/diff", response_model=ApiResponse[dict])
def diff_spec(
    request: Request,
    from_version: int = Query(ge=1),
    to_version: int = Query(ge=1),
    project: Project = Depends(require_project_owner),
    db: Session = Depends(get_db),
) -> ApiResponse[dict]:
    """R06：查看两个规格版本的功能/排除项/业务规则/验收场景差异。"""
    from_row = spec_version(db, project.id, from_version)
    to_row = spec_version(db, project.id, to_version)
    if from_row is None or to_row is None:
        raise AppException(ErrorCode.SPEC_NOT_FOUND, "指定的规格版本不存在。", status_code=404)
    return ApiResponse(request_id=get_request_id(request), data=spec_diff(from_row, to_row))
