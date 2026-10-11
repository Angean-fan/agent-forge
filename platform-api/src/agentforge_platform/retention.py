"""M28 运行产物保留与配额策略。

- **只清理已终结**（SUCCEEDED/FAILED）且**超过保留期**的 run 目录；
  进行中的 run 与仍在保留期内（承诺可下载）的产物一律不动。
- 总用量超过配额或可用磁盘低于阈值时，**拒绝新运行**（`start_run` 前判定）；
  维护任务清理过期产物即可回收空间。
- 阈值经环境变量配置：`AGENTFORGE_RUN_QUOTA_BYTES`（0=不限）、
  `AGENTFORGE_MIN_FREE_BYTES`（0=不检查）、`AGENTFORGE_RUN_RETENTION_DAYS`。
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.models import Run
from agentforge_platform.runs import work_root

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED"}


def run_retention_days() -> int:
    return int(os.environ.get("AGENTFORGE_RUN_RETENTION_DAYS", "14"))


def run_quota_bytes() -> int:
    return int(os.environ.get("AGENTFORGE_RUN_QUOTA_BYTES", str(5 * 1024**3)))


def min_free_bytes() -> int:
    return int(os.environ.get("AGENTFORGE_MIN_FREE_BYTES", str(512 * 1024**2)))


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def run_dir_path(run_id: int) -> Path:
    return work_root().resolve() / f"run-{run_id}"


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def usage_bytes() -> int:
    root = work_root().resolve()
    if not root.exists():
        return 0
    return sum(
        directory_size(child) for child in root.iterdir() if child.is_dir() and child.name.startswith("run-")
    )


def _existing_dir(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def free_bytes() -> int:
    return shutil.disk_usage(_existing_dir(work_root().resolve())).free


def storage_status() -> dict[str, int | bool]:
    quota = run_quota_bytes()
    min_free = min_free_bytes()
    used = usage_bytes()
    free = free_bytes()
    return {
        "used_bytes": used,
        "quota_bytes": quota,
        "free_bytes": free,
        "min_free_bytes": min_free,
        "over_quota": quota > 0 and used > quota,
        "low_free": min_free > 0 and free < min_free,
    }


def ensure_run_storage_available() -> None:
    """配额超限或剩余空间不足时拒绝新运行。"""
    status = storage_status()
    if status["over_quota"]:
        raise AppException(
            ErrorCode.STORAGE_QUOTA_EXCEEDED,
            "运行产物已达配额上限，请先清理过期运行后再提交。",
            status_code=507,
        )
    if status["low_free"]:
        raise AppException(
            ErrorCode.STORAGE_QUOTA_EXCEEDED,
            "磁盘剩余空间不足，已拒绝新的生成运行。",
            status_code=507,
        )


def select_purgeable(
    db: Session, *, retention_days: int | None = None, now: datetime | None = None
) -> list[tuple[Run, Path, int]]:
    """返回可清理的 (run, 目录, 字节数)：仅已终结且超过保留期、且目录存在。"""
    days = run_retention_days() if retention_days is None else retention_days
    cutoff = (now or _now()) - timedelta(days=days)
    rows = db.scalars(
        select(Run).where(Run.status.in_(TERMINAL_STATUSES), Run.updated_at < cutoff)
    ).all()
    purgeable: list[tuple[Run, Path, int]] = []
    for run in rows:
        path = run_dir_path(run.id)
        if path.exists():
            purgeable.append((run, path, directory_size(path)))
    return purgeable


def cleanup_runs(
    db: Session, *, retention_days: int | None = None, now: datetime | None = None, dry_run: bool = False
) -> dict[str, object]:
    """清理过期终态 run 的目录；进行中与保留期内的产物不受影响。"""
    purgeable = select_purgeable(db, retention_days=retention_days, now=now)
    removed: list[int] = []
    freed = 0
    for run, path, size in purgeable:
        if not dry_run:
            shutil.rmtree(path, ignore_errors=True)
        removed.append(run.id)
        freed += size
    return {
        "removed_run_ids": removed,
        "freed_bytes": freed,
        "dry_run": dry_run,
        "retention_days": run_retention_days() if retention_days is None else retention_days,
    }
