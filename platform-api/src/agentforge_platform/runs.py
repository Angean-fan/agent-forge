"""M19 运行元数据与最小状态机。

状态是数据库权威值，只能通过**白名单迁移**：
``QUEUED → PLANNING → GENERATING → VERIFYING → SUCCEEDED/FAILED``（中途可判失败）。
事件 sequence 在每个 run 内单调递增；run 目录按 run_id 隔离；归属经 project.owner_id 校验。
"""

from __future__ import annotations

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
from agentforge_platform.models import Artifact, Project, Run, RunEvent, RunTask, Worker

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
