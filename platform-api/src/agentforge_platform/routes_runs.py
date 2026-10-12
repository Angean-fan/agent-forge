"""M20/M27 运行 API：提交（幂等）、轮询状态与事件、产物下载、worker 心跳查询。"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentforge_platform.accounts import Account
from agentforge_platform.auth import get_current_account, require_csrf, require_project_owner
from agentforge_platform.db import get_db
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.middleware import get_request_id
from agentforge_platform.models import Artifact, Project, Run, RunEvent, SpecVersion, Worker
from agentforge_platform.responses import ApiResponse
from agentforge_platform.retention import ensure_run_storage_available
from agentforge_platform.runs import (
    append_event,
    create_run,
    current_phase,
    events_after,
    find_artifact,
    find_run_by_idempotency,
    is_worker_healthy,
    latest_sequence,
    list_artifacts,
    require_run_owner,
    resolve_artifact_path,
    run_cost,
    task_counts,
    verification_report,
    work_root,
)
from agentforge_platform.spec import is_legacy_task_spec, scope_issues
from agentforge_platform.spec_service import load_spec

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


class TaskSummary(BaseModel):
    model_config = _STRICT

    total: int
    done: int


class CostSummary(BaseModel):
    model_config = _STRICT

    currency_unit: str
    settled_micro: int
    reserved_micro: int
    unknown_calls: int


class ArtifactView(BaseModel):
    model_config = _STRICT

    id: int
    kind: str
    path: str
    sha256: str
    verified: bool
    created_at: datetime


class RunDetailView(BaseModel):
    """运行详情：阶段、任务计数、成本（已结算/预留分列）、产物、验证报告与失败摘要。"""

    model_config = _STRICT

    id: int
    project_id: int
    spec_id: int
    status: str
    phase: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    tasks: TaskSummary
    cost: CostSummary
    artifacts: list[ArtifactView]
    verification: dict | None = None


class RunEventView(BaseModel):
    model_config = _STRICT

    sequence: int
    type: str
    payload: dict
    created_at: datetime


class RunEventsData(BaseModel):
    model_config = _STRICT

    run_id: int
    items: list[RunEventView]
    latest_sequence: int


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


def _artifact_view(artifact: Artifact) -> ArtifactView:
    return ArtifactView(
        id=artifact.id,
        kind=artifact.kind,
        path=artifact.path,
        sha256=artifact.sha256,
        verified=artifact.verified,
        created_at=artifact.created_at,
    )


def _detail_view(db: Session, run: Run) -> RunDetailView:
    total, done = task_counts(db, run.id)
    cost = run_cost(db, run.id)
    return RunDetailView(
        id=run.id,
        project_id=run.project_id,
        spec_id=run.spec_id,
        status=run.status,
        phase=current_phase(db, run.id),
        error_code=run.error_code,
        created_at=run.created_at,
        updated_at=run.updated_at,
        tasks=TaskSummary(total=total, done=done),
        cost=CostSummary(currency_unit="micro_yuan", **cost),
        artifacts=[_artifact_view(item) for item in list_artifacts(db, run.id)],
        verification=(
            verification_report(run.id)
            if run.status in ("SUCCEEDED", "FAILED")
            else None
        ),
    )


def _event_view(event: RunEvent) -> RunEventView:
    # 事件 payload 由平台自身写入（阶段名/哈希/错误码），不含密钥；解析失败也不回显原文之外的内容。
    try:
        payload = json.loads(event.payload)
    except ValueError:
        payload = {"raw": event.payload}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return RunEventView(
        sequence=event.sequence, type=event.type, payload=payload, created_at=event.created_at
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

    # 生成入口能力门禁（R01）：合法多实体候选在 R10 能力就绪前不得生成，
    # 也不得落入旧 contract.py 单实体路径。旧 Task 单实体路径照旧放行。
    spec_row = db.get(SpecVersion, active_spec_id)
    if spec_row is not None:
        spec = load_spec(spec_row)
        scope = scope_issues(spec)
        if scope:
            raise AppException(
                ErrorCode.UNSUPPORTED_SCOPE,
                "超出 Demo 范围：" + "；".join(scope) + "。",
                status_code=422,
            )
        if not is_legacy_task_spec(spec):
            raise AppException(
                ErrorCode.SCOPE_CAPABILITY_NOT_READY,
                "多业务实体生成能力尚未就绪（计划于 R10 提供）。",
                status_code=422,
            )

    # 配额/剩余空间达标才接受新运行（超限拒绝；过期产物由维护任务清理）。
    ensure_run_storage_available()

    run = create_run(db, project, active_spec_id, idempotency_key=idempotency_key)
    append_event(db, run, "queued", "{}")
    return ApiResponse(request_id=get_request_id(request), data=_run_view(run))


@router.get("/runs/{run_id}", response_model=ApiResponse[RunDetailView])
def get_run(
    request: Request,
    run: Run = Depends(require_run_owner),
    db: Session = Depends(get_db),
) -> ApiResponse[RunDetailView]:
    return ApiResponse(request_id=get_request_id(request), data=_detail_view(db, run))


@router.get("/runs/{run_id}/events", response_model=ApiResponse[RunEventsData])
def list_run_events(
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    run: Run = Depends(require_run_owner),
    db: Session = Depends(get_db),
) -> ApiResponse[RunEventsData]:
    rows = events_after(db, run.id, after_sequence, limit)
    return ApiResponse(
        request_id=get_request_id(request),
        data=RunEventsData(
            run_id=run.id,
            items=[_event_view(event) for event in rows],
            latest_sequence=latest_sequence(db, run.id),
        ),
    )


@router.get("/runs/{run_id}/artifact")
def download_artifact(
    run: Run = Depends(require_run_owner), db: Session = Depends(get_db)
) -> FileResponse:
    """下载本 run 的源码 ZIP；只按 run_id + 已记录的产物解析路径，不接受用户传入文件名。"""
    artifact = find_artifact(db, run.id, "zip")
    if artifact is None:
        raise AppException(
            ErrorCode.ARTIFACT_NOT_FOUND, "该运行尚未产出可下载的源码包。", status_code=404
        )
    path = resolve_artifact_path(work_root(), run.id, artifact)
    if not path.is_file():
        raise AppException(ErrorCode.ARTIFACT_NOT_FOUND, "产物文件不存在。", status_code=404)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"agentforge-run-{run.id}.zip",
        headers={"X-Artifact-Verified": "true" if artifact.verified else "false"},
    )


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
