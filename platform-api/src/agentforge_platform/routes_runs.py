"""M20/M27 运行 API：提交（幂等）、轮询状态与事件、产物下载、worker 心跳查询。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentforge_platform.accounts import Account
from agentforge_platform.auth import get_current_account, require_csrf, require_project_owner
from agentforge_platform.budget import effective_budget_micro, record_adjustment
from agentforge_platform.db import get_db
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.iteration import artifact_eligibility, load_artifact, plan_iteration
from agentforge_platform.live_validation import parse_live_services
from agentforge_platform.middleware import get_request_id
from agentforge_platform.models import Artifact, Project, Run, RunEvent, SpecVersion, Worker
from agentforge_platform.packaging import build_cost_report
from agentforge_platform.preview import (
    ACCESS_OK,
    PREVIEW_STATUS_ACTIVE,
    PreviewSession,
    authorized_url,
    build_preview_env,
    create_preview,
    is_expired,
    isolation_issues,
    preview_access,
    recycle,
    secret_env_names,
)
from agentforge_platform.readiness import (
    ExternalService,
    code_delivery_status,
    compute_readiness,
    declared_services,
    readiness_notice,
)
from agentforge_platform.responses import ApiResponse
from agentforge_platform.retention import ensure_run_storage_available
from agentforge_platform.runs import (
    RunStatus,
    append_event,
    create_run,
    current_phase,
    events_after,
    find_artifact,
    find_artifact_by_id,
    find_run_by_idempotency,
    is_worker_healthy,
    latest_sequence,
    list_artifacts,
    list_project_runs,
    request_cancel,
    require_run_owner,
    resolve_artifact_path,
    run_cost,
    run_spec_hash,
    task_counts,
    transition,
    unsettled_call_count,
    verification_report,
    work_root,
)
from agentforge_platform.spec import SoftwareSpecification, scope_issues
from agentforge_platform.spec_service import (
    confirm_draft,
    latest_spec,
    load_spec,
    put_draft,
    require_frozen_spec,
)

router = APIRouter(prefix="/api", tags=["runs"])
_STRICT = ConfigDict(extra="forbid")


class RunCreateRequest(BaseModel):
    model_config = _STRICT

    spec_id: int | None = None


class RunResumeRequest(BaseModel):
    """R24：经确认的恢复——可选新增预算（记入可追溯调整）。"""

    model_config = _STRICT

    additional_budget_micro: int = 0
    reason: str | None = None


class RunView(BaseModel):
    model_config = _STRICT

    id: int
    project_id: int
    spec_id: int
    spec_version: int | None
    spec_hash: str | None
    status: str
    error_code: str | None
    failure: str | None
    cancel_requested: bool
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


class CancellationView(BaseModel):
    """R22：取消状态与费用说明（取消不撤回 Provider 已处理调用）。"""

    model_config = _STRICT

    requested: bool
    notice: str | None = None


class PackageView(BaseModel):
    """R25：交付包状态（成功/失败包一致标注；失败包显著标“未验证”）。"""

    model_config = _STRICT

    available: bool
    artifact_id: int | None
    verified: bool
    label: str
    notice: str | None = None


class ExternalServiceView(BaseModel):
    """E04：单个外部服务的**脱敏**就绪快照（Mock/Live/配置，不含真实值）。"""

    model_config = _STRICT

    service_id: str
    name: str
    mode: str
    required: bool
    required_for_live: bool
    secret: bool
    env_name: str
    config_present: bool
    mock_status: str
    live_status: str
    reason: str | None = None


class RunDetailView(BaseModel):
    """运行详情：阶段、任务计数、成本（已结算/预留分列）、产物、验证报告与失败摘要。

    E04：``code_delivery_status``（代码交付）与 ``external_readiness``（外部服务就绪）**分开**呈现；
    逐服务 Mock/Live/配置状态用于结果页分列展示。
    """

    model_config = _STRICT

    id: int
    project_id: int
    spec_id: int
    spec_version: int | None
    spec_hash: str | None
    status: str
    phase: str | None
    error_code: str | None
    failure: str | None
    created_at: datetime
    updated_at: datetime
    tasks: TaskSummary
    cost: CostSummary
    artifacts: list[ArtifactView]
    verification: dict | None = None
    cancellation: CancellationView | None = None
    package: PackageView | None = None
    # E04：代码交付与外部就绪分开；缺配置/第三方故障分别呈现，不伪称验证。
    code_delivery_status: str = "NOT_RUN"
    external_readiness: str = "NOT_REQUIRED"
    external_notice: str | None = None
    external_services: list[ExternalServiceView] = []


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


class RunHistoryData(BaseModel):
    model_config = _STRICT

    project_id: int
    items: list[RunView]


class WorkerView(BaseModel):
    model_config = _STRICT

    worker_id: str
    hostname: str
    last_heartbeat: datetime
    healthy: bool


class WorkerListData(BaseModel):
    model_config = _STRICT

    items: list[WorkerView]


class PreviewView(BaseModel):
    """E08：在线预览会话（脱敏；随机授权地址，默认 15 分钟回收）。"""

    model_config = _STRICT

    run_id: int
    url: str
    status: str
    demo_data: bool
    runtime_attached: bool
    created_at: str
    expires_at: str
    notice: str | None = None


class IterationRequest(BaseModel):
    """E09：既有项目的规格增量迭代——提交**已确认**的变更后规格与幂等键。"""

    model_config = _STRICT

    spec: dict
    idempotency_key: str | None = None


class IterationView(BaseModel):
    """E09：迭代结果——新版本 spec/run 与最小变更计划（旧 run 不变）。"""

    model_config = _STRICT

    old_run_id: int
    new_run_id: int
    spec_id: int
    spec_version: int
    contract_changed: bool
    relock_required: bool
    reused_method_units: list[str]
    notes: list[str]


# R25：失败原因用**确定性模板**（不调用模型；需要用户解释时才另走 DeepSeek user_interaction 并计账）。
_FAILURE_MESSAGES = {
    "VERIFICATION_FAILED": "构建或验收未通过，请查看验证报告中的未通过项。",
    "INFRASTRUCTURE": "受控执行环境故障（基础设施），非代码问题。",
    "RUN_TIMEOUT": "运行超过总时限被终止。",
    "NEEDS_RECONCILIATION": "存在状态不明的模型调用，需先完成账单对账再重跑。",
    "SPEC_TAMPERED": "冻结规格疑似被篡改，已拒绝继续。",
    "SPEC_NOT_FOUND": "缺少已确认的冻结规格。",
}


def _failure_message(error_code: str | None) -> str | None:
    if not error_code:
        return None
    return _FAILURE_MESSAGES.get(error_code, f"运行失败（{error_code}）。")


def _spec_version(db: Session, run: Run) -> int | None:
    row = db.get(SpecVersion, run.spec_id)
    return row.version if row is not None else None


def _run_view(db: Session, run: Run) -> RunView:
    return RunView(
        id=run.id,
        project_id=run.project_id,
        spec_id=run.spec_id,
        spec_version=_spec_version(db, run),
        spec_hash=run_spec_hash(db, run),
        status=run.status,
        error_code=run.error_code,
        failure=_failure_message(run.error_code),
        cancel_requested=run.cancel_requested,
        lease_owner=run.lease_owner,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


_CANCEL_NOTICE = "取消不会撤回模型 Provider 已处理的调用；已花费与在途预留将如实保留在成本中。"


def _cancellation_view(run: Run) -> CancellationView:
    show = run.cancel_requested or run.status == "CANCELLED"
    return CancellationView(requested=run.cancel_requested, notice=_CANCEL_NOTICE if show else None)


def _artifact_view(artifact: Artifact) -> ArtifactView:
    return ArtifactView(
        id=artifact.id,
        kind=artifact.kind,
        path=artifact.path,
        sha256=artifact.sha256,
        verified=artifact.verified,
        created_at=artifact.created_at,
    )


def _package_view(db: Session, run: Run) -> PackageView:
    artifact = find_artifact(db, run.id, "zip")
    if artifact is None:
        return PackageView(available=False, artifact_id=None, verified=False, label="无产物")
    report = verification_report(run.id) or {}
    verified = bool(report.get("verified", artifact.verified))
    return PackageView(
        available=True, artifact_id=artifact.id, verified=verified,
        label="已验证" if verified else "未验证",
        notice=None if verified else "该交付包未通过全部必需检查，仅供排查，请以验证报告为准。",
    )


def _run_spec(db: Session, run: Run):
    """读取 run 冻结规格（用于解析声明的外部服务）；缺失/不可解析返回 None（不新增失败模式）。"""
    row = db.get(SpecVersion, run.spec_id)
    if row is None:
        return None
    try:
        return load_spec(row)
    except ValueError:
        return None


_PREVIEW_NOTICE = "预览运行时（隔离实例）未接入本环境：本接口只做授权与生命周期管理，不公开运行未知代码。"


def _preview_path(run_id: int) -> Path:
    return work_root().resolve() / f"run-{run_id}" / "preview.json"


def _read_preview(run_id: int) -> PreviewSession | None:
    try:
        payload = json.loads(_preview_path(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return PreviewSession(**payload)
    except TypeError:
        return None


def _write_preview(session: PreviewSession) -> None:
    path = _preview_path(session.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session.__dict__, ensure_ascii=False), encoding="utf-8")


def _preview_view(session: PreviewSession) -> PreviewView:
    return PreviewView(
        run_id=session.run_id, url=authorized_url(session.token), status=session.status,
        demo_data=session.demo_data, runtime_attached=session.runtime_attached,
        created_at=session.created_at, expires_at=session.expires_at, notice=_PREVIEW_NOTICE,
    )


def _preview_services(db: Session, run: Run) -> list:
    spec = _run_spec(db, run)
    return parse_live_services(getattr(spec, "external_requirements", None)) if spec else []


def _service_view(service: ExternalService) -> ExternalServiceView:
    return ExternalServiceView(
        service_id=service.service_id,
        name=service.name,
        mode=service.mode,
        required=service.required,
        required_for_live=service.required_for_live,
        secret=service.secret,
        env_name=service.env_name,
        config_present=service.config_present,
        mock_status=service.mock_status,
        live_status=service.live_status,
        reason=service.reason,
    )


def _detail_view(db: Session, run: Run) -> RunDetailView:
    total, done = task_counts(db, run.id)
    cost = run_cost(db, run.id)
    report = verification_report(run.id)
    spec = _run_spec(db, run)
    services = declared_services(spec, report) if spec is not None else []
    # C03：优先读取**已持久化**的就绪快照（权威）；旧数据该列为 NULL → 读时派生，行为不变。
    readiness = run.external_readiness or compute_readiness(services)
    return RunDetailView(
        id=run.id,
        project_id=run.project_id,
        spec_id=run.spec_id,
        spec_version=_spec_version(db, run),
        spec_hash=run_spec_hash(db, run),
        status=run.status,
        phase=current_phase(db, run.id),
        error_code=run.error_code,
        failure=_failure_message(run.error_code),
        created_at=run.created_at,
        updated_at=run.updated_at,
        tasks=TaskSummary(total=total, done=done),
        cost=CostSummary(currency_unit="micro_yuan", **cost),
        artifacts=[_artifact_view(item) for item in list_artifacts(db, run.id)],
        verification=report if run.status in ("SUCCEEDED", "FAILED") else None,
        cancellation=_cancellation_view(run),
        package=_package_view(db, run),
        code_delivery_status=code_delivery_status(run.status, error_code=run.error_code),
        external_readiness=readiness,
        external_notice=readiness_notice(readiness, services),
        external_services=[_service_view(service) for service in services],
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
        return ApiResponse(request_id=get_request_id(request), data=_run_view(db, existing))

    # R06：运行只接收**未被篡改**的冻结规格；其冻结 hash 记入 run 并随生成校验。
    # R10：多实体生成能力已就绪，门禁解除；仅保留通用业务范围校验。
    spec_hash: str | None = None
    spec_row = db.get(SpecVersion, active_spec_id)
    if spec_row is not None:
        spec = require_frozen_spec(spec_row)
        spec_hash = spec_row.hash
        scope = scope_issues(spec)
        if scope:
            raise AppException(
                ErrorCode.UNSUPPORTED_SCOPE,
                "超出 Demo 范围：" + "；".join(scope) + "。",
                status_code=422,
            )

    # 配额/剩余空间达标才接受新运行（超限拒绝；过期产物由维护任务清理）。
    ensure_run_storage_available()

    run = create_run(
        db, project, active_spec_id, idempotency_key=idempotency_key, spec_hash=spec_hash
    )
    append_event(db, run, "queued", "{}")
    return ApiResponse(request_id=get_request_id(request), data=_run_view(db, run))


@router.get("/projects/{project_id}/runs", response_model=ApiResponse[RunHistoryData])
def list_runs(
    request: Request,
    project: Project = Depends(require_project_owner),
    db: Session = Depends(get_db),
) -> ApiResponse[RunHistoryData]:
    """R25：项目运行历史（仅归属者可见；含状态/失败原因/规格版本）。"""
    return ApiResponse(
        request_id=get_request_id(request),
        data=RunHistoryData(
            project_id=project.id, items=[_run_view(db, run) for run in list_project_runs(db, project.id)]
        ),
    )


@router.get("/runs/{run_id}/cost", response_model=ApiResponse[dict])
def get_run_cost(
    request: Request,
    run: Run = Depends(require_run_owner),
    db: Session = Depends(get_db),
) -> ApiResponse[dict]:
    """R25：分层阶段成本（访谈期 / run 级 / 各 task / 按模型）；只读、归属校验。"""
    return ApiResponse(
        request_id=get_request_id(request),
        data=build_cost_report(db, project_id=run.project_id, run_id=run.id),
    )


@router.get("/runs/{run_id}/artifacts/{artifact_id}")
def download_artifact_by_id(
    artifact_id: int,
    run: Run = Depends(require_run_owner),
    db: Session = Depends(get_db),
) -> FileResponse:
    """R25：按 artifact_id 下载——严格校验归属（run 归属）与 artifact 属于该 run。"""
    artifact = find_artifact_by_id(db, run.id, artifact_id)
    if artifact is None:
        raise AppException(ErrorCode.ARTIFACT_NOT_FOUND, "产物不存在或不属于该运行。", status_code=404)
    path = resolve_artifact_path(work_root(), run.id, artifact)
    if not path.is_file():
        raise AppException(ErrorCode.ARTIFACT_NOT_FOUND, "产物文件不存在。", status_code=404)
    headers = {"X-Artifact-Verified": "true" if artifact.verified else "false"}
    if artifact.path.endswith(".zip"):
        return FileResponse(path, media_type="application/zip",
                            filename=f"agentforge-run-{run.id}.zip", headers=headers)
    return FileResponse(path, filename=artifact.path, headers=headers)


@router.get("/runs/{run_id}", response_model=ApiResponse[RunDetailView])
def get_run(
    request: Request,
    run: Run = Depends(require_run_owner),
    db: Session = Depends(get_db),
) -> ApiResponse[RunDetailView]:
    return ApiResponse(request_id=get_request_id(request), data=_detail_view(db, run))


@router.post("/runs/{run_id}/cancel", response_model=ApiResponse[RunView])
def cancel_run(
    request: Request,
    run: Run = Depends(require_run_owner),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApiResponse[RunView]:
    """R22：幂等取消。QUEUED 直接取消；在途置标志由 worker 在节点边界落 CANCELLED，阻止新派发。"""
    request_cancel(db, run)
    return ApiResponse(request_id=get_request_id(request), data=_run_view(db, run))


@router.post("/runs/{run_id}/resume", response_model=ApiResponse[RunView])
def resume_run(
    request: Request,
    payload: RunResumeRequest | None = None,
    run: Run = Depends(require_run_owner),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_account),
) -> ApiResponse[RunView]:
    """R24：经确认的预算恢复。仅 PAUSED_BUDGET 可恢复；需预算确实增加；只重新入队续跑，不重复已提交任务。"""
    if run.status != RunStatus.PAUSED_BUDGET:
        raise AppException(ErrorCode.RUN_INVALID_TRANSITION, "仅预算暂停的运行可恢复。", status_code=409)
    additional = payload.additional_budget_micro if payload is not None else 0
    if additional < 0:
        raise AppException(ErrorCode.BAD_REQUEST, "预算调整不能为负。", status_code=422)
    if additional > 0:  # 可追溯预算调整
        record_adjustment(
            db, project_id=run.project_id, run_id=run.id, delta_micro=additional,
            reason=(payload.reason if payload and payload.reason else "resume_topup"),
            actor_id=account.id,
        )
    if unsettled_call_count(db, run.id) > 0:  # 恢复前核对：状态不明调用须先对账
        raise AppException(
            ErrorCode.RUN_INVALID_TRANSITION,
            "存在状态不明的模型调用，需先完成账单对账再恢复。", status_code=409,
        )
    cost = run_cost(db, run.id)
    available = effective_budget_micro(db, run.project_id) - cost["settled_micro"] - cost["reserved_micro"]
    if available <= 0:  # 未增加预算 → 恢复被拒
        raise AppException(ErrorCode.BUDGET_EXCEEDED, "预算仍不足，恢复被拒；请先增加预算。", status_code=402)
    transition(db, run, RunStatus.QUEUED)  # 重新入队，由 worker 从安全检查点续跑
    run.lease_owner = None
    run.lease_until = None
    append_event(db, run, "resumed", json.dumps({"additional_budget_micro": additional}))
    db.flush()
    return ApiResponse(request_id=get_request_id(request), data=_run_view(db, run))


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


@router.post("/runs/{run_id}/preview", response_model=ApiResponse[PreviewView])
def start_preview(
    request: Request,
    run: Run = Depends(require_run_owner),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApiResponse[PreviewView]:
    """E08：为**已通过全部代码必需验收**的产物开启短时预览（随机地址；默认 15 分钟回收）。

    预览只用**演示数据**，绝不注入秘密/正式凭据；未验证产物拒绝（``PREVIEW_INELIGIBLE``）。
    """
    session = create_preview(
        run.id, run_status=run.status,
        code_delivery_status=code_delivery_status(run.status, error_code=run.error_code),
    )
    services = _preview_services(db, run)
    env = build_preview_env(services)
    issues = isolation_issues(env, secret_env_names(services))
    if issues:
        raise AppException(ErrorCode.PREVIEW_INELIGIBLE, "预览隔离校验未通过：" + "；".join(issues), status_code=409)
    _write_preview(session)
    return ApiResponse(request_id=get_request_id(request), data=_preview_view(session))


@router.get("/runs/{run_id}/preview", response_model=ApiResponse[PreviewView])
def get_preview(
    request: Request,
    run: Run = Depends(require_run_owner),
) -> ApiResponse[PreviewView]:
    """查询预览会话（仅归属者）；已到期则**自动回收**并如实标注 EXPIRED。"""
    session = _read_preview(run.id)
    if session is None:
        raise AppException(ErrorCode.NOT_FOUND, "该运行没有在线预览。", status_code=404)
    if session.status == PREVIEW_STATUS_ACTIVE and is_expired(session):
        session = recycle(session)
        _write_preview(session)
    return ApiResponse(request_id=get_request_id(request), data=_preview_view(session))


@router.delete("/runs/{run_id}/preview", response_model=ApiResponse[PreviewView])
def stop_preview(
    request: Request,
    run: Run = Depends(require_run_owner),
    _csrf: None = Depends(require_csrf),
) -> ApiResponse[PreviewView]:
    """E08：停止预览——**只**回收预览实例/数据；源码与 ZIP 不受影响，仍可下载。"""
    session = _read_preview(run.id)
    if session is None:
        raise AppException(ErrorCode.NOT_FOUND, "该运行没有在线预览。", status_code=404)
    stopped = recycle(session, stopped=True)
    _write_preview(stopped)
    return ApiResponse(request_id=get_request_id(request), data=_preview_view(stopped))


@router.get("/preview/{token}")
def open_preview(token: str) -> dict:
    """E08 授权网关：按**随机 token** 授权访问；未知/过期/已停止一律 404（不泄露存在性）。

    本环境**不接入**真实隔离实例，仅返回会话描述；真实预览运行时需开发者确认（NOT_RUN）。
    """
    for run_dir in sorted(work_root().resolve().glob("run-*")):
        try:
            run_id = int(run_dir.name.removeprefix("run-"))
        except ValueError:
            continue
        session = _read_preview(run_id)
        if session is None:
            continue
        if preview_access(session, token) == ACCESS_OK:
            return {"run_id": session.run_id, "status": session.status,
                    "runtime_attached": False, "notice": _PREVIEW_NOTICE}
    raise AppException(ErrorCode.NOT_FOUND, "预览地址无效或已过期。", status_code=404)


@router.post("/runs/{run_id}/iterate", response_model=ApiResponse[IterationView], status_code=202)
def iterate_run(
    request: Request,
    payload: IterationRequest,
    run: Run = Depends(require_run_owner),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApiResponse[IterationView]:
    """E09：为**已完成**的 AgentForge 产物做规格增量迭代——冻结新规格版本 + 新建 run。

    旧 run / ZIP / 锁**不予原地修改**；契约改变 → ``relock_required``（显式重新检索/冻结新锁）。
    """
    if run.status != "SUCCEEDED":
        raise AppException(ErrorCode.RUN_INVALID_TRANSITION, "仅已完成的运行可迭代。", status_code=409)
    run_dir = work_root().resolve() / f"run-{run.id}"
    issues = artifact_eligibility(run_dir)
    if issues:
        raise AppException(
            ErrorCode.ITERATION_INELIGIBLE,
            "不支持迭代：" + "；".join(issues) + "（起步仅支持 AgentForge 自有产物）",
            status_code=422,
        )

    old_row = db.get(SpecVersion, run.spec_id)
    old_spec = require_frozen_spec(old_row) if old_row is not None else None
    if old_spec is None:
        raise AppException(ErrorCode.SPEC_NOT_FOUND, "缺少源运行的冻结规格。", status_code=409)
    _, old_lock = load_artifact(run_dir)

    new_spec = SoftwareSpecification.model_validate(payload.spec)
    scope = scope_issues(new_spec)
    if scope:
        raise AppException(ErrorCode.UNSUPPORTED_SCOPE, "超出 Demo 范围：" + "；".join(scope) + "。", status_code=422)

    plan = plan_iteration(old_spec, old_lock, new_spec)

    project = db.get(Project, run.project_id)
    latest = latest_spec(db, project.id)
    row = put_draft(db, project, expected_version=latest.version if latest else 0, spec=new_spec)
    row = confirm_draft(db, project, expected_version=row.version)  # 冻结新规格版本
    new_run = create_run(
        db, project, row.id,
        idempotency_key=payload.idempotency_key or f"iterate-{run.id}-v{row.version}",
        spec_hash=row.hash,
    )
    append_event(db, new_run, "queued", "{}")
    return ApiResponse(
        request_id=get_request_id(request),
        data=IterationView(
            old_run_id=run.id, new_run_id=new_run.id, spec_id=row.id, spec_version=row.version,
            contract_changed=plan.impact.contract_changed, relock_required=plan.relock_required,
            reused_method_units=list(plan.reused_method_units), notes=list(plan.notes),
        ),
    )
