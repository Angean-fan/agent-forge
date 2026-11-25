"""项目路由：创建项目、列表与详情。仅创建/读取需求草稿，不生成代码。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, StringConstraints
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from agentforge_platform.accounts import Account
from agentforge_platform.auth import get_current_account, require_csrf, require_project_owner
from agentforge_platform.db import get_db
from agentforge_platform.middleware import get_request_id
from agentforge_platform.models import (
    Artifact,
    BudgetAdjustment,
    DefectTicketRow,
    DepartmentRun,
    FactoryBlueprint,
    Project,
    Run,
    RunEvent,
    RunTask,
    SpecVersion,
)
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


class ProjectDeleteData(BaseModel):
    deleted: bool
    project_id: int


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


@router.delete("/{project_id}", response_model=ApiResponse[ProjectDeleteData])
def delete_project(
    request: Request,
    project: Project = Depends(require_project_owner),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApiResponse[ProjectDeleteData]:
    """删除当前用户拥有的项目及其规格草稿与运行记录（保留财务 model_call 审计账本）。"""
    project_id = project.id
    run_ids = list(db.scalars(select(Run.id).where(Run.project_id == project_id)).all())
    if run_ids:
        db.execute(delete(DefectTicketRow).where(DefectTicketRow.run_id.in_(run_ids)))
        db.execute(delete(DepartmentRun).where(DepartmentRun.run_id.in_(run_ids)))
        db.execute(delete(FactoryBlueprint).where(FactoryBlueprint.run_id.in_(run_ids)))
        db.execute(delete(Artifact).where(Artifact.run_id.in_(run_ids)))
        db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
        db.execute(delete(RunTask).where(RunTask.run_id.in_(run_ids)))
        db.execute(delete(Run).where(Run.id.in_(run_ids)))

    db.execute(delete(BudgetAdjustment).where(BudgetAdjustment.project_id == project_id))
    db.execute(delete(SpecVersion).where(SpecVersion.project_id == project_id))
    db.delete(project)
    db.flush()

    return ApiResponse(
        request_id=get_request_id(request),
        data=ProjectDeleteData(deleted=True, project_id=project_id),
    )
