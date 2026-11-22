"""FP01：异步工厂流水线特性开关与顶层编排器骨架（``factory_pipeline.py``）。

约束：
- ``AGENTFORGE_FACTORY_PIPELINE_ENABLED`` 默认为 ``false``；
- 开关关闭时，``worker.py`` 与 ``generation_graph.py`` 完全不触发本模块，原有串行路径零改动；
- 本步仅落地顶层运行生命周期、取消检查、崩溃恢复扫描与阶段调度骨架；未接入后续步骤的真实阶段执行器时，
  明确返回 ``FACTORY_STAGE_NOT_IMPLEMENTED`` 并落 ``FAILED``，严禁伪造 ``SUCCEEDED``。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from agentforge_platform.db import session_scope
from agentforge_platform.factory_state import (
    FactoryCancelledError,
    FactoryPhase,
    FactoryStateStore,
)
from agentforge_platform.models import Run
from agentforge_platform.runs import (
    RECOVERABLE_STATUSES,
    TERMINAL_STATUSES,
    RunStatus,
    append_event,
    transition,
    unsettled_call_count,
)

if TYPE_CHECKING:
    from agentforge_platform.generation_graph import GenerationContext

FACTORY_PIPELINE_ENV = "AGENTFORGE_FACTORY_PIPELINE_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

StageRunner = Callable[["GenerationContext", int, FactoryStateStore, FactoryPhase], dict[str, Any]]


def factory_pipeline_enabled() -> bool:
    """判定是否显式开启异步工厂流水线（默认 ``False``）。"""
    raw = os.environ.get(FACTORY_PIPELINE_ENV, "").strip().lower()
    return raw in _TRUTHY


def _transition_run_if_active(
    context: GenerationContext,
    run_id: int,
    target: RunStatus,
    *,
    event_type: str,
    payload: dict[str, Any],
    error_code: str | None = None,
) -> None:
    with session_scope(context.session_factory) as db:
        run = db.get(Run, run_id)
        if run is None:
            return
        if RunStatus(run.status) not in TERMINAL_STATUSES:
            transition(db, run, target, error_code=error_code if target is RunStatus.FAILED else None)
        append_event(db, run, event_type, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def run_factory_pipeline(
    context: GenerationContext,
    run_id: int,
    *,
    backend_runner: StageRunner | None = None,
    frontend_runner: StageRunner | None = None,
) -> dict[str, Any]:
    """FP01 顶层编排器骨架：处理领取/恢复、取消边界检查与阶段分发。

    后续步骤（FP02—FP09）将向本骨架注入真实的后端阶段、前端阶段与全栈总装打包执行器。
    """
    from agentforge_platform.generation_graph import GenerationError

    store = FactoryStateStore(context.session_factory)

    with session_scope(context.session_factory) as db:
        run = db.get(Run, run_id)
        if run is None:
            raise GenerationError("RUN_NOT_FOUND", f"run {run_id} 不存在")
        if run.cancel_requested:
            if RunStatus(run.status) not in TERMINAL_STATUSES:
                transition(db, run, RunStatus.CANCELLED)
                append_event(db, run, "cancelled", json.dumps({"stage": "factory_pre_claim"}))
            return {
                "run_id": run_id,
                "ok": False,
                "mode": "factory",
                "error_code": "RUN_CANCELLED",
            }
        recovering = RunStatus(run.status) in RECOVERABLE_STATUSES
        if recovering:
            if unsettled_call_count(db, run_id) > 0:
                transition(db, run, RunStatus.FAILED, error_code="NEEDS_RECONCILIATION")
                append_event(
                    db,
                    run,
                    "failed",
                    json.dumps({"code": "NEEDS_RECONCILIATION", "stage": "factory_recover"}),
                )
                return {
                    "run_id": run_id,
                    "ok": False,
                    "mode": "factory",
                    "error_code": "NEEDS_RECONCILIATION",
                }
            previous = str(run.status)
            run.status = RunStatus.PLANNING
            db.flush()
            append_event(db, run, "recovered", json.dumps({"from": previous, "mode": "factory"}))
        else:
            transition(db, run, RunStatus.PLANNING)
            append_event(db, run, "claimed", json.dumps({"mode": "factory"}))
            append_event(db, run, "factory_started", json.dumps({"recovering": False}))

    try:
        store.raise_if_cancelled(run_id, boundary="before_backend_recovery")
        backend_recovery = store.scan_recovery_plan(run_id, FactoryPhase.BACKEND)
        frontend_recovery = store.scan_recovery_plan(run_id, FactoryPhase.FRONTEND)
        recovery_summary = {
            "backend": backend_recovery.as_dict(),
            "frontend": frontend_recovery.as_dict(),
        }

        if backend_runner is None and frontend_runner is None:
            _transition_run_if_active(
                context,
                run_id,
                RunStatus.FAILED,
                event_type="failed",
                payload={
                    "code": "FACTORY_STAGE_NOT_IMPLEMENTED",
                    "mode": "factory",
                    "recovery": recovery_summary,
                },
                error_code="FACTORY_STAGE_NOT_IMPLEMENTED",
            )
            return {
                "run_id": run_id,
                "ok": False,
                "mode": "factory",
                "error_code": "FACTORY_STAGE_NOT_IMPLEMENTED",
                "recovery": recovery_summary,
            }

        results: dict[str, Any] = {}
        if backend_runner is not None:
            store.raise_if_cancelled(run_id, boundary="before_backend_stage")
            results["backend"] = backend_runner(context, run_id, store, FactoryPhase.BACKEND)

        if frontend_runner is not None:
            store.raise_if_cancelled(run_id, boundary="before_frontend_stage")
            results["frontend"] = frontend_runner(context, run_id, store, FactoryPhase.FRONTEND)

        store.raise_if_cancelled(run_id, boundary="before_finalize")
        return {
            "run_id": run_id,
            "ok": True,
            "mode": "factory",
            "recovery": recovery_summary,
            "stages": results,
        }
    except FactoryCancelledError:
        cancelled_depts = store.cancel_incomplete_departments(
            run_id, reason="run_cancel_requested"
        )
        _transition_run_if_active(
            context,
            run_id,
            RunStatus.CANCELLED,
            event_type="cancelled",
            payload={
                "code": "RUN_CANCELLED",
                "mode": "factory",
                "cancelled_depts": cancelled_depts,
            },
        )
        return {
            "run_id": run_id,
            "ok": False,
            "mode": "factory",
            "error_code": "RUN_CANCELLED",
            "cancelled_depts": cancelled_depts,
        }
    except Exception as exc:  # noqa: BLE001
        code = str(getattr(exc, "code", None) or type(exc).__name__)
        if code == "BUDGET_EXCEEDED":
            _transition_run_if_active(
                context,
                run_id,
                RunStatus.PAUSED_BUDGET,
                event_type="paused_budget",
                payload={"code": code, "mode": "factory"},
            )
            return {
                "run_id": run_id,
                "ok": False,
                "mode": "factory",
                "error_code": "PAUSED_BUDGET",
            }
        _transition_run_if_active(
            context,
            run_id,
            RunStatus.FAILED,
            event_type="failed",
            payload={"code": code, "mode": "factory"},
            error_code=code,
        )
        return {
            "run_id": run_id,
            "ok": False,
            "mode": "factory",
            "error_code": code,
        }


__all__ = [
    "FACTORY_PIPELINE_ENV",
    "StageRunner",
    "factory_pipeline_enabled",
    "run_factory_pipeline",
]
