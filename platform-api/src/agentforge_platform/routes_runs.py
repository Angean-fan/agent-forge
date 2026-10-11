"""M20 运行 API：提交（幂等）、轮询状态、worker 心跳查询。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentforge_platform.accounts import Account
from agentforge_platform.auth import get_current_account, require_csrf, require_project_owner
from agentforge_platform.db import get_db
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.middleware import get_request_id
from agentforge_platform.models import Project, Run, Worker
from agentforge_platform.responses import ApiResponse
from agentforge_platform.runs import (
    append_event,
    create_run,
    find_run_by_idempotency,
    is_worker_healthy,
    require_run_owner,
)

router = APIRouter(prefix="/api", tags=["runs"])
_STRICT = ConfigDict(extra="forbid")


class RunCreateRequest(BaseModel):
    model_config = _STRICT

    spec_id: int | None = None


class RunView(BaseModel):
    model_config = _STRICT

    id: int
    project_id: int
    spec_id: int
    status: str
    error_code: str | None
    lease_owner: str | None
    created_at: datetime
    updated_at: datetime


class WorkerView(BaseModel):
    model_config = _STRICT

    worker_id: str
    hostname: str
    last_heartbeat: datetime
    healthy: bool


class WorkerListData(BaseModel):
    model_config = _STRICT

    items: list[WorkerView]


def _run_view(run: Run) -> RunView:
    return RunView(
        id=run.id,
        project_id=run.project_id,
        spec_id=run.spec_id,
        status=run.status,
        error_code=run.error_code,
        lease_owner=run.lease_owner,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


@router.post("/projects/{project_id}/runs", response_model=ApiResponse[RunView], status_code=202)
def start_run(
    request: Request,
    payload: RunCreateRequest | None = None,
    project: Project = Depends(require_project_owner),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ApiResponse[RunView]:
    active_spec_id = project.active_spec_id
    if active_spec_id is None:
        raise AppException(
            ErrorCode.SPEC_NOT_READY, "项目尚无已确认规格，不能运行。", status_code=409
        )
    if payload is not None and payload.spec_id is not None and payload.spec_id != active_spec_id:
        raise AppException(ErrorCode.SPEC_CONFLICT, "spec_id 与已确认版本不一致。", status_code=409)
    if not idempotency_key:
        raise AppException(ErrorCode.BAD_REQUEST, "缺少 Idempotency-Key 头。", status_code=400)

    existing = find_run_by_idempotency(db, project.id, idempotency_key)
    if existing is not None:
        return ApiResponse(request_id=get_request_id(request), data=_run_view(existing))

    run = create_run(db, project, active_spec_id, idempotency_key=idempotency_key)
    append_event(db, run, "queued", "{}")
    return ApiResponse(request_id=get_request_id(request), data=_run_view(run))


@router.get("/runs/{run_id}", response_model=ApiResponse[RunView])
def get_run(request: Request, run: Run = Depends(require_run_owner)) -> ApiResponse[RunView]:
    return ApiResponse(request_id=get_request_id(request), data=_run_view(run))


@router.get("/workers", response_model=ApiResponse[WorkerListData])
def list_workers(
    request: Request,
    _account: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> ApiResponse[WorkerListData]:
    rows = db.scalars(select(Worker).order_by(Worker.worker_id)).all()
    items = [
        WorkerView(
            worker_id=worker.worker_id,
            hostname=worker.hostname,
            last_heartbeat=worker.last_heartbeat,
            healthy=is_worker_healthy(worker.last_heartbeat),
        )
        for worker in rows
    ]
    return ApiResponse(request_id=get_request_id(request), data=WorkerListData(items=items))
