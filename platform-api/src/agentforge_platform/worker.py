"""M20/M23 独立 worker：心跳 + 事务领取（租约）+ 驱动 M23 生成图。

worker **不与 API 进程绑定**：任务持久化于 SQLite，worker 以独立进程按租约领取，
再调用处理函数（默认即 M23 生成图）。心跳/租约用 UTC + 数据库时间；进程内等待用单调时钟。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Self

from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform.asset_registry import default_registry
from agentforge_platform.db import SessionLocal, session_scope
from agentforge_platform.factory_pipeline import build_factory_runtime, factory_pipeline_enabled
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.method_index import DEFAULT_METHODS_ROOT
from agentforge_platform.production_routing import build_production_routing
from agentforge_platform.runner_client import HttpRunnerClient, RunnerClient
from agentforge_platform.runs import claim_run, heartbeat, renew_run_lease

Handler = Callable[[int], None]
_logger = logging.getLogger(__name__)


class _LeaseKeeper(AbstractContextManager["_LeaseKeeper"]):
    """长耗时运行期间的后台续租与心跳守护线程（防工厂模式多部门运行期间心跳超时或租约过期被重复领取）。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        run_id: int,
        worker_id: str,
        hostname: str,
        lease_seconds: int,
        heartbeat_interval: float,
    ) -> None:
        self._session_factory = session_factory
        self._run_id = run_id
        self._worker_id = worker_id
        self._hostname = hostname
        self._lease_seconds = max(1, int(lease_seconds))
        self._interval = max(0.05, min(float(heartbeat_interval), self._lease_seconds / 3.0))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with session_scope(self._session_factory) as db:
                    heartbeat(db, self._worker_id, self._hostname)
                    renew_run_lease(
                        db,
                        self._run_id,
                        self._worker_id,
                        lease_seconds=self._lease_seconds,
                    )
            except Exception:  # 续租偶发异常不得拖垮主处理线程
                _logger.debug("worker lease renewal failed for run %s", self._run_id, exc_info=True)

    def __enter__(self) -> Self:
        self._thread = threading.Thread(
            target=self._loop,
            name=f"lease-keeper-{self._run_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def build_generation_handler(
    session_factory: sessionmaker[Session],
    *,
    runner: RunnerClient | None = None,
    factory_runtime: object | None = None,
) -> Handler:
    """默认处理函数：驱动 M23 生成图或 FP09 异步工厂流水线（Runner 地址/密钥/工作根来自环境）。

    C01：按显式开关与凭据注入生产模型路由（默认全关 → 标准资产路径零 Qwen 请求）。
    FP09：当 ``AGENTFORGE_FACTORY_PIPELINE_ENABLED=1`` 时注入工厂运行时并由 ``run_generation`` 顶层分发。
    """
    work_root = Path(os.environ.get("RUNNER_WORK_ROOT", "./var/runs"))
    routing = build_production_routing(session_factory=session_factory, work_root=work_root)
    active_runner: RunnerClient = runner or HttpRunnerClient(
        base_url=os.environ.get("RUNNER_BASE_URL", "http://127.0.0.1:9000"),
        token=os.environ.get("RUNNER_TOKEN", ""),
    )
    resolved_factory_runtime = factory_runtime
    if resolved_factory_runtime is None and factory_pipeline_enabled():
        resolved_factory_runtime = build_factory_runtime(
            session_factory=session_factory,
            runner_client=active_runner,
            work_root=work_root,
        )
    context = GenerationContext(
        session_factory=session_factory,
        runner=active_runner,
        work_root=work_root,
        registry=default_registry(),
        methods_root=Path(os.environ.get("AGENTFORGE_METHODS_ROOT", str(DEFAULT_METHODS_ROOT))),
        planning_client=routing.planning_client,
        review_client=routing.review_client,
        coding_routing=routing.coding_routing,
        factory_runtime=resolved_factory_runtime,
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
            with _LeaseKeeper(
                session_factory=factory,
                run_id=claimed_id,
                worker_id=assigned_id,
                hostname=hostname,
                lease_seconds=lease_seconds,
                heartbeat_interval=heartbeat_interval,
            ):
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
