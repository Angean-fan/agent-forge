"""规格路由：读取/编辑草稿与确认冻结。"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agentforge_platform.auth import require_csrf, require_project_owner
from agentforge_platform.db import get_db
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.middleware import get_request_id
from agentforge_platform.models import Project, SpecVersion
from agentforge_platform.requirement_extraction import extract_requirement
from agentforge_platform.responses import ApiResponse
from agentforge_platform.spec import (
    SoftwareSpecification,
    confirmation_issues,
    critical_pending_fields,
)
from agentforge_platform.spec_service import confirm_draft, latest_spec, load_spec, put_draft

router = APIRouter(prefix="/api/projects", tags=["spec"])


class SpecPutRequest(BaseModel):
    version: int = Field(ge=0)
    spec: SoftwareSpecification


class SpecConfirmRequest(BaseModel):
    version: int = Field(ge=0)


class SpecExtractRequest(BaseModel):
    version: int = Field(ge=0)


class SpecView(BaseModel):
    spec_id: int
    version: int
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
    hash: str
    confirmed_at: datetime


def _view(row: SpecVersion) -> SpecView:
    spec = load_spec(row)
    return SpecView(
        spec_id=row.id,
        version=row.version,
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
    row = put_draft(db, project, payload.version, payload.spec)
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
    row = confirm_draft(db, project, payload.version)
    return ApiResponse(
        request_id=get_request_id(request),
        data=SpecConfirmView(
            spec_id=row.id,
            version=row.version,
            hash=row.hash,
            confirmed_at=row.confirmed_at,
        ),
    )
