"""项目路由：创建项目、列表与详情。仅创建/读取需求草稿，不生成代码。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentforge_platform.accounts import Account
from agentforge_platform.auth import get_current_account, require_csrf, require_project_owner
from agentforge_platform.db import get_db
from agentforge_platform.middleware import get_request_id
from agentforge_platform.models import Project
from agentforge_platform.responses import ApiResponse

router = APIRouter(prefix="/api/projects", tags=["projects"])

ProjectName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Requirement = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=20, max_length=5000)
]


class ProjectCreateRequest(BaseModel):
    name: ProjectName
    requirement: Requirement


class ProjectSummary(BaseModel):
    id: int
    name: str
    active_spec_id: int | None
    created_at: datetime


class ProjectDetail(ProjectSummary):
    requirement: str


class ProjectListData(BaseModel):
    items: list[ProjectSummary]


def _summary(project: Project) -> ProjectSummary:
    return ProjectSummary(
        id=project.id,
        name=project.name,
        active_spec_id=project.active_spec_id,
        created_at=project.created_at,
    )


def _detail(project: Project) -> ProjectDetail:
    return ProjectDetail(
        id=project.id,
        name=project.name,
        active_spec_id=project.active_spec_id,
        created_at=project.created_at,
        requirement=project.requirement_text,
    )


@router.post("", response_model=ApiResponse[ProjectDetail], status_code=201)
def create_project(
    payload: ProjectCreateRequest,
    request: Request,
    account: Account = Depends(get_current_account),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApiResponse[ProjectDetail]:
    project = Project(
        owner_id=account.id,
        name=payload.name,
        requirement_text=payload.requirement,
    )
    db.add(project)
    db.flush()
    db.refresh(project)
    return ApiResponse(request_id=get_request_id(request), data=_detail(project))


@router.get("", response_model=ApiResponse[ProjectListData])
def list_projects(
    request: Request,
    account: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> ApiResponse[ProjectListData]:
    rows = db.scalars(
        select(Project).where(Project.owner_id == account.id).order_by(Project.id.desc())
    ).all()
    return ApiResponse(
        request_id=get_request_id(request),
        data=ProjectListData(items=[_summary(project) for project in rows]),
    )


@router.get("/{project_id}", response_model=ApiResponse[ProjectDetail])
def get_project(
    request: Request, project: Project = Depends(require_project_owner)
) -> ApiResponse[ProjectDetail]:
    return ApiResponse(request_id=get_request_id(request), data=_detail(project))
