"""M19 运行元数据与最小状态机。

状态是数据库权威值，只能通过**白名单迁移**：
``QUEUED → PLANNING → GENERATING → VERIFYING → SUCCEEDED/FAILED``（中途可判失败）。
事件 sequence 在每个 run 内单调递增；run 目录按 run_id 隔离；归属经 project.owner_id 校验。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from fastapi import Depends
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from agentforge_platform.accounts import Account
from agentforge_platform.auth import get_current_account
from agentforge_platform.db import get_db
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.models import (
    Artifact,
    ModelCall,
    Project,
    Run,
    RunEvent,
    RunTask,
    Worker,
)

DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 30
_CLAIM_ATTEMPTS = 5


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    GENERATING = "GENERATING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.QUEUED: {RunStatus.PLANNING},
    RunStatus.PLANNING: {RunStatus.GENERATING, RunStatus.FAILED},
    RunStatus.GENERATING: {RunStatus.VERIFYING, RunStatus.FAILED},
    RunStatus.VERIFYING: {RunStatus.SUCCEEDED, RunStatus.FAILED},
    RunStatus.SUCCEEDED: set(),
    RunStatus.FAILED: set(),
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def create_run(
    db: Session, project: Project, spec_id: int, *, idempotency_key: str | None = None
) -> Run:
    run = Run(
        project_id=project.id,
        spec_id=spec_id,
        status=RunStatus.QUEUED,
        idempotency_key=idempotency_key,
    )
    db.add(run)
    db.flush()
    db.refresh(run)
    return run


def find_run_by_idempotency(db: Session, project_id: int, key: str) -> Run | None:
    return db.scalars(
        select(Run).where(Run.project_id == project_id, Run.idempotency_key == key)
    ).first()


def claim_run(db: Session, worker_id: str, *, lease_seconds: int = 60) -> Run | None:
    """事务领取一个 QUEUED 且租约空闲/过期的 run；原子更新防双领。租约比较以数据库时间为准。"""
    for _ in range(_CLAIM_ATTEMPTS):
        run_id = db.scalars(
            select(Run.id)
            .where(Run.status == RunStatus.QUEUED)
            .where(or_(Run.lease_until.is_(None), Run.lease_until <= func.now()))
            .order_by(Run.id)
            .limit(1)
        ).first()
        if run_id is None:
            return None
        result = db.execute(
            update(Run)
            .where(Run.id == run_id)
            .where(Run.status == RunStatus.QUEUED)
            .where(or_(Run.lease_until.is_(None), Run.lease_until <= func.now()))
            .values(lease_owner=worker_id, lease_until=_now() + timedelta(seconds=lease_seconds))
        )
        if result.rowcount == 1:
            db.flush()
            return db.get(Run, run_id)
    return None


def heartbeat(db: Session, worker_id: str, hostname: str) -> Worker:
    worker = db.get(Worker, worker_id)
    now = _now()
    if worker is None:
        worker = Worker(worker_id=worker_id, hostname=hostname, last_heartbeat=now)
        db.add(worker)
    else:
        worker.last_heartbeat = now
    db.flush()
    return worker


def worker_heartbeat_timeout() -> int:
    return int(
        os.environ.get(
            "AGENTFORGE_WORKER_HEARTBEAT_TIMEOUT", str(DEFAULT_HEARTBEAT_TIMEOUT_SECONDS)
        )
    )


def is_worker_healthy(
    last_heartbeat: datetime, *, now: datetime | None = None, threshold_seconds: int | None = None
) -> bool:
    reference = now or _now()
    threshold = worker_heartbeat_timeout() if threshold_seconds is None else threshold_seconds
    return (reference - last_heartbeat) <= timedelta(seconds=threshold)


def transition(db: Session, run: Run, new_status: RunStatus, *, error_code: str | None = None) -> Run:
    current = RunStatus(run.status)
    if new_status not in ALLOWED_TRANSITIONS[current]:
        raise AppException(
            ErrorCode.RUN_INVALID_TRANSITION,
            f"非法状态转移：{current} → {new_status}",
            status_code=409,
        )
    run.status = new_status
    run.error_code = error_code if new_status is RunStatus.FAILED else None
    run.updated_at = _now()
    db.flush()
    return run


def append_event(db: Session, run: Run, event_type: str, payload: str) -> RunEvent:
    last = db.scalar(select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run.id))
    event = RunEvent(run_id=run.id, sequence=(last or 0) + 1, type=event_type, payload=payload)
    db.add(event)
    db.flush()
    return event


def record_task(db: Session, run: Run, task_id: str, *, status: str = "PENDING") -> RunTask:
    task = RunTask(run_id=run.id, task_id=task_id, status=status)
    db.add(task)
    db.flush()
    return task


def record_artifact(
    db: Session, run: Run, path: str, sha256: str, kind: str, *, verified: bool = False
) -> Artifact:
    artifact = Artifact(run_id=run.id, path=path, sha256=sha256, kind=kind, verified=verified)
    db.add(artifact)
    db.flush()
    return artifact


def run_directory(work_root: str | Path, run_id: int) -> Path:
    """每个 run 的隔离目录：``<work_root>/run-<id>``（按 id 构造，不接收任意路径）。"""
    directory = Path(work_root).resolve() / f"run-{run_id}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def work_root() -> Path:
    """平台运行根目录（与 worker 共用同一环境变量，默认 ``./var/runs``）。"""
    return Path(os.environ.get("RUNNER_WORK_ROOT", "./var/runs"))


def resolve_artifact_path(work_root_path: str | Path, run_id: int, artifact: Artifact) -> Path:
    """解析产物绝对路径；路径必须落在本 run 目录内（拒绝目录穿越）。"""
    run_dir = (Path(work_root_path).resolve() / f"run-{run_id}").resolve()
    candidate = (run_dir / artifact.path).resolve()
    if candidate != run_dir and run_dir not in candidate.parents:
        raise AppException(ErrorCode.ARTIFACT_NOT_FOUND, "产物路径非法。", status_code=404)
    return candidate


def find_artifact(db: Session, run_id: int, kind: str) -> Artifact | None:
    return db.scalars(
        select(Artifact)
        .where(Artifact.run_id == run_id, Artifact.kind == kind)
        .order_by(Artifact.id.desc())
    ).first()


def list_artifacts(db: Session, run_id: int) -> list[Artifact]:
    return list(
        db.scalars(select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.id)).all()
    )


def task_counts(db: Session, run_id: int) -> tuple[int, int]:
    """返回 (任务总数, 已完成数)。"""
    total = db.scalar(
        select(func.count()).select_from(RunTask).where(RunTask.run_id == run_id)
    )
    done = db.scalar(
        select(func.count())
        .select_from(RunTask)
        .where(RunTask.run_id == run_id, RunTask.status == "DONE")
    )
    return int(total or 0), int(done or 0)


def run_cost(db: Session, run_id: int) -> dict[str, int]:
    """本 run 生成期用量：已结算（settled）与预留（reserved）分列，未定价计 unknown。"""
    settled = db.scalar(
        select(func.coalesce(func.sum(ModelCall.cost_micro), 0)).where(
            ModelCall.run_id == run_id, ModelCall.status == "SETTLED"
        )
    )
    reserved = db.scalar(
        select(func.coalesce(func.sum(ModelCall.reserved_micro), 0)).where(
            ModelCall.run_id == run_id, ModelCall.status != "SETTLED"
        )
    )
    unknown = db.scalar(
        select(func.count(ModelCall.call_id)).where(
            ModelCall.run_id == run_id, ModelCall.status != "SETTLED"
        )
    )
    return {
        "settled_micro": int(settled or 0),
        "reserved_micro": int(reserved or 0),
        "unknown_calls": int(unknown or 0),
    }


def current_phase(db: Session, run_id: int) -> str | None:
    """最近一次 ``stage`` 事件的阶段名；无则 None（前端回退到状态）。"""
    row = db.scalars(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.type == "stage")
        .order_by(RunEvent.sequence.desc())
    ).first()
    if row is None:
        return None
    try:
        payload = json.loads(row.payload)
    except ValueError:
        return None
    stage = payload.get("stage") if isinstance(payload, dict) else None
    return stage if isinstance(stage, str) else None


def events_after(db: Session, run_id: int, after_sequence: int, limit: int) -> list[RunEvent]:
    return list(
        db.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.sequence > after_sequence)
            .order_by(RunEvent.sequence)
            .limit(limit)
        ).all()
    )


def latest_sequence(db: Session, run_id: int) -> int:
    return int(
        db.scalar(select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)) or 0
    )


def worker_status(db: Session) -> dict[str, object]:
    """汇总所有 worker 的心跳健康；供监控/告警探针使用。"""
    rows = db.scalars(select(Worker).order_by(Worker.worker_id)).all()
    items = [
        {
            "worker_id": worker.worker_id,
            "hostname": worker.hostname,
            "last_heartbeat": worker.last_heartbeat.isoformat(),
            "healthy": is_worker_healthy(worker.last_heartbeat),
        }
        for worker in rows
    ]
    unhealthy = [item["worker_id"] for item in items if not item["healthy"]]
    return {"workers": items, "unhealthy": unhealthy}


def verification_report(run_id: int) -> dict | None:
    """读取本 run 的 verification.json（含逐项 PASS/FAIL/NOT_RUN）；缺失则 None。"""
    path = work_root().resolve() / f"run-{run_id}" / "verification.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def require_run_owner(
    run_id: int,
    account: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise AppException(ErrorCode.RUN_NOT_FOUND, "运行不存在。", status_code=404)
    project = db.get(Project, run.project_id)
    if project is None or project.owner_id != account.id:
        raise AppException(ErrorCode.FORBIDDEN, "无权访问该运行。", status_code=403)
    return run
