"""M20 独立 worker：心跳 + 事务领取（租约）+ 处理。

worker **不与 API 进程绑定**：任务持久化于 SQLite，worker 以独立进程按租约领取。
心跳与领取用 UTC + 数据库时间；进程内等待用单调时钟。真实生成图在 M23 接入——
本步的默认处理**如实判失败**（`GENERATION_NOT_IMPLEMENTED`），不伪造成功。
"""

from __future__ import annotations

import socket
import time
import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform.db import SessionLocal, session_scope
from agentforge_platform.models import Run
from agentforge_platform.runs import RunStatus, append_event, claim_run, heartbeat, transition

Handler = Callable[[Session, Run], None]


def not_implemented_handler(db: Session, run: Run) -> None:
    """M20 占位：进入 PLANNING 后如实判失败（生成图未实现，交 M23）。"""
    transition(db, run, RunStatus.PLANNING)
    append_event(db, run, "stage", '{"stage":"planning"}')
    transition(db, run, RunStatus.FAILED, error_code="GENERATION_NOT_IMPLEMENTED")
    append_event(db, run, "failed", '{"code":"GENERATION_NOT_IMPLEMENTED"}')


def run_worker(
    *,
    session_factory: sessionmaker[Session] | None = None,
    handler: Handler = not_implemented_handler,
    worker_id: str | None = None,
    iterations: int | None = None,
    poll_interval: float = 1.0,
    lease_seconds: int = 60,
    heartbeat_interval: float = 10.0,
) -> str:
    """worker 主循环；`iterations` 有界便于测试，None 表示长期运行。返回 worker_id。"""
    factory = session_factory or SessionLocal
    assigned_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    hostname = socket.gethostname()
    last_heartbeat = float("-inf")
    count = 0

    while iterations is None or count < iterations:
        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_interval:
            with session_scope(factory) as db:
                heartbeat(db, assigned_id, hostname)
            last_heartbeat = now

        with session_scope(factory) as db:
            run = claim_run(db, assigned_id, lease_seconds=lease_seconds)
            if run is not None:
                handler(db, run)

        count += 1
        if iterations is None or count < iterations:
            time.sleep(poll_interval)

    return assigned_id


def main() -> int:
    run_worker()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
