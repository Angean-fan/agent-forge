"""M20/M23 独立 worker：心跳 + 事务领取（租约）+ 驱动 M23 生成图。

worker **不与 API 进程绑定**：任务持久化于 SQLite，worker 以独立进程按租约领取，
再调用处理函数（默认即 M23 生成图）。心跳/租约用 UTC + 数据库时间；进程内等待用单调时钟。
"""

from __future__ import annotations

import os
import socket
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform.asset_registry import default_registry
from agentforge_platform.db import SessionLocal, session_scope
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.runner_client import HttpRunnerClient
from agentforge_platform.runs import claim_run, heartbeat

Handler = Callable[[int], None]


def build_generation_handler(session_factory: sessionmaker[Session]) -> Handler:
    """默认处理函数：驱动 M23 生成图（Runner 地址/密钥/工作根来自环境）。"""
    context = GenerationContext(
        session_factory=session_factory,
        runner=HttpRunnerClient(
            base_url=os.environ.get("RUNNER_BASE_URL", "http://127.0.0.1:9000"),
            token=os.environ.get("RUNNER_TOKEN", ""),
        ),
        work_root=Path(os.environ.get("RUNNER_WORK_ROOT", "./var/runs")),
        registry=default_registry(),
    )

    def handler(run_id: int) -> None:
        run_generation(context, run_id)

    return handler


def run_worker(
    *,
    session_factory: sessionmaker[Session] | None = None,
    handler: Handler | None = None,
    worker_id: str | None = None,
    iterations: int | None = None,
    poll_interval: float = 1.0,
    lease_seconds: int = 60,
    heartbeat_interval: float = 10.0,
) -> str:
    """worker 主循环；`iterations` 有界便于测试，None 表示长期运行。返回 worker_id。"""
    factory = session_factory or SessionLocal
    active_handler = handler or build_generation_handler(factory)
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
            claimed_id = run.id if run is not None else None

        if claimed_id is not None:
            active_handler(claimed_id)

        count += 1
        if iterations is None or count < iterations:
            time.sleep(poll_interval)

    return assigned_id


def main() -> int:
    run_worker()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
