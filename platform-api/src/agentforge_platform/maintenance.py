"""M28 平台维护任务：过期会话批量清理、运行产物保留/配额清理。

用法：
    uv run python -m agentforge_platform.maintenance sessions purge
    uv run python -m agentforge_platform.maintenance sessions purge --loop 3600
    uv run python -m agentforge_platform.maintenance runs status
    uv run python -m agentforge_platform.maintenance runs cleanup [--dry-run]

可由 cron / systemd timer / compose 维护服务定期调用（见 docs/MVP_DEPLOY.md）。
"""

from __future__ import annotations

import argparse
import json
import time

from agentforge_platform.auth import purge_expired_sessions
from agentforge_platform.db import SessionLocal, session_scope
from agentforge_platform.retention import cleanup_runs, storage_status
from agentforge_platform.runs import worker_status


def _sessions_purge(loop_seconds: int | None) -> int:
    while True:
        with session_scope(SessionLocal) as db:
            removed = purge_expired_sessions(db)
        print(json.dumps({"purged_sessions": removed}, ensure_ascii=False), flush=True)
        if loop_seconds is None:
            return 0
        time.sleep(loop_seconds)


def _runs_status() -> int:
    print(json.dumps(storage_status(), ensure_ascii=False), flush=True)
    return 0


def _runs_cleanup(dry_run: bool) -> int:
    with session_scope(SessionLocal) as db:
        result = cleanup_runs(db, dry_run=dry_run)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


def _workers_check() -> int:
    """worker 心跳停滞告警探针：存在不健康 worker 时退出码非 0（供监控告警）。"""
    with session_scope(SessionLocal) as db:
        status = worker_status(db)
    print(json.dumps(status, ensure_ascii=False), flush=True)
    return 1 if status["unhealthy"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentforge_platform.maintenance")
    sub = parser.add_subparsers(dest="group", required=True)

    sessions = sub.add_parser("sessions", help="平台会话维护")
    sessions_sub = sessions.add_subparsers(dest="action", required=True)
    purge = sessions_sub.add_parser("purge", help="批量删除已过期会话")
    purge.add_argument("--loop", type=int, default=None, metavar="SECONDS", help="循环执行间隔秒数")

    runs = sub.add_parser("runs", help="运行产物维护")
    runs_sub = runs.add_subparsers(dest="action", required=True)
    runs_sub.add_parser("status", help="打印配额与剩余空间状态")
    cleanup = runs_sub.add_parser("cleanup", help="清理过期终态运行目录")
    cleanup.add_argument("--dry-run", action="store_true", help="只列出将清理的 run，不实际删除")

    workers = sub.add_parser("workers", help="worker 健康")
    workers_sub = workers.add_subparsers(dest="action", required=True)
    workers_sub.add_parser("check", help="检查 worker 心跳；有不健康则退出码非 0")

    args = parser.parse_args(argv)
    if args.group == "sessions" and args.action == "purge":
        return _sessions_purge(args.loop)
    if args.group == "runs" and args.action == "status":
        return _runs_status()
    if args.group == "runs" and args.action == "cleanup":
        return _runs_cleanup(args.dry_run)
    if args.group == "workers" and args.action == "check":
        return _workers_check()
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
