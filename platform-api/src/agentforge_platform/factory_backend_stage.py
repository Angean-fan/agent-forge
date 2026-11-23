"""FP06：后端总装、强制 Docker Runner 总验收与总管归因打回大循环（``factory_backend_stage.py``）。

串联完整后端合龙闭环：
``后端多部门异步并行产物 → 确定性总装（零冲突/零底座篡改/V900__relations.sql/openapi.json）``
``→ 独立 Docker Runner 强制总验收（严禁降级为 LocalProcessModuleVerifier）``
``→ 若 INFRASTRUCTURE 错误：立即停止（返工次数 = 0），绝不打回改业务代码``
``→ 若业务/集成错误：总管根因归因生成 DefectTicket → 仅定向打回责任部门 REWORK 重跑 → 重装复验``
``→ 全 PASS 后正式冻结权威 backend/openapi.json 供前端流水线消费。``

大循环上限硬编码默认 ``max_integration_rounds = 2``（可配收紧至 1，绝不静默放大）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from agentforge_platform.blueprint_contracts import ArchitectureBlueprint
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.factory_assembler import (
    BackendMergeResult,
    freeze_authoritative_openapi,
    merge_department_deliverables,
)
from agentforge_platform.factory_chief import ChiefArchitect
from agentforge_platform.factory_scheduler import AsyncDepartmentPool
from agentforge_platform.factory_state import (
    DeptStatus,
    FactoryCancelledError,
    FactoryPhase,
    FactoryStateStore,
)
from agentforge_platform.factory_tickets import DefectTicket, DepartmentDeliverable
from agentforge_platform.module_verifier import (
    require_docker_runner_for_final_acceptance,
    run_final_acceptance_accept,
    run_final_acceptance_verify,
)
from agentforge_platform.packaging import VERIFY_REQUIRED, verification_passed
from agentforge_platform.runner_client import RunnerClient
from agentforge_platform.spec import SoftwareSpecification

_STRICT = ConfigDict(extra="forbid")

MAX_INTEGRATION_ROUNDS_ENV = "AGENTFORGE_FACTORY_MAX_INTEGRATION_ROUNDS"
DEFAULT_MAX_INTEGRATION_ROUNDS = 2
BACKEND_VERIFY_REQUIRED = ("static_manifest", "backend_test", "backend_build")
BACKEND_ASSEMBLED_DIRNAME = "backend_assembled"
BACKEND_INTEGRATION_REPORT_NAME = "backend-integration-report.json"

_INFRA_MARKERS = (
    "infrastructure",
    "docker",
    "connection refused",
    "cannot connect to the docker daemon",
    "image not found",
    "pull access denied",
    "runner 连接失败",
    "runner http 5",
    "timeout",
    "timed out",
)


def max_integration_rounds_limit(override: int | None = None) -> int:
    """大循环上限：硬编码默认 2，可经环境变量收紧（区间 [1, 2]）。"""
    if override is not None:
        return max(1, min(int(override), DEFAULT_MAX_INTEGRATION_ROUNDS))
    raw = os.environ.get(MAX_INTEGRATION_ROUNDS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_INTEGRATION_ROUNDS
    try:
        return max(1, min(int(raw), DEFAULT_MAX_INTEGRATION_ROUNDS))
    except ValueError:
        return DEFAULT_MAX_INTEGRATION_ROUNDS


class BackendStageResult(BaseModel):
    """后端阶段（总装 + 强制 Docker Runner 总验收 + 归因大循环）最终结果。"""

    model_config = _STRICT

    run_id: int
    ok: bool
    status: Literal["PASSED", "FAILED", "INFRASTRUCTURE", "CANCELLED", "PAUSED_BUDGET"]
    integration_rounds_used: int = 0
    rework_rounds: int = 0
    assembled_dir: str
    report_path: str | None = None
    frozen_openapi_path: str | None = None
    frozen_openapi_sha256: str | None = None
    frozen_openapi_doc: dict[str, Any] | None = None
    verify_report: dict[str, Any] = {}
    accept_report: dict[str, Any] = {}
    reworked_depts_history: list[list[str]] = []
    triage_tickets: list[DefectTicket] = []
    deliverables: dict[str, DepartmentDeliverable] = {}
    error_code: str | None = None


def _is_infrastructure_report(report: dict[str, Any] | None) -> bool:
    if not isinstance(report, dict):
        return False
    overall = str(report.get("overall") or "").strip().upper()
    err = str(report.get("error_code") or report.get("category") or "").strip().upper()
    if overall == "INFRASTRUCTURE" or err == "INFRASTRUCTURE":
        return True
    for key in ("checks", "scenarios"):
        items = report.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item_status = str(item.get("status") or "").strip().upper()
            item_cls = str(
                item.get("error_class") or item.get("category") or item.get("error_code") or ""
            ).strip().upper()
            if item_cls == "INFRASTRUCTURE":
                return True
            if item_status == "NOT_RUN":
                detail_blob = (
                    f"{item.get('detail') or ''} {item.get('log_excerpt') or ''}"
                ).lower()
                if any(marker in detail_blob for marker in _INFRA_MARKERS):
                    return True
    return False


def _backend_acceptance_passed(
    verify_report: dict[str, Any],
    accept_report: dict[str, Any],
) -> bool:
    checks = {
        str(c.get("name")): str(c.get("status"))
        for c in verify_report.get("checks", [])
        if isinstance(c, dict)
    }
    if "frontend_build" in checks and all(name in checks for name in VERIFY_REQUIRED):
        return verification_passed(verify_report, accept_report)
    if verify_report.get("overall") != "PASS" or accept_report.get("overall") != "PASS":
        return False
    if any(checks.get(name) != "PASS" for name in BACKEND_VERIFY_REQUIRED if name in checks):
        return False
    for c in verify_report.get("checks", []):
        if isinstance(c, dict) and c.get("status") != "PASS":
            return False
    for s in accept_report.get("scenarios", []):
        if isinstance(s, dict) and s.get("status") != "PASS":
            return False
    return bool(checks)


def _write_integration_report(
    run_root: Path,
    payload: dict[str, Any],
) -> str:
    report_file = run_root / BACKEND_INTEGRATION_REPORT_NAME
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return str(report_file)


async def run_backend_factory_stage(
    *,
    run_id: int,
    blueprint: ArchitectureBlueprint,
    spec: SoftwareSpecification,
    run_dir: str | Path,
    base_dir: str | Path,
    pool: AsyncDepartmentPool,
    chief: ChiefArchitect,
    runner_client: RunnerClient,
    state_store: FactoryStateStore,
    deliverables: dict[str, DepartmentDeliverable] | None = None,
    max_integration_rounds: int | None = None,
) -> BackendStageResult:
    """执行后端工厂总装、强制 Docker Runner 总验收与归因打回大循环。"""
    # 硬守卫：后端总验收必须强制走独立 Docker Runner（严禁 LocalProcessModuleVerifier）
    require_docker_runner_for_final_acceptance(runner_client)

    run_root = Path(run_dir).resolve()
    base_root = Path(base_dir).resolve()
    assembled_dir = run_root / BACKEND_ASSEMBLED_DIRNAME
    effective_max_rounds = max_integration_rounds_limit(max_integration_rounds)

    current_bp = blueprint
    active_deliverables: dict[str, DepartmentDeliverable] = dict(deliverables or {})
    reworked_history: list[list[str]] = []
    all_triage_tickets: list[DefectTicket] = []
    rework_rounds = 0
    rounds_used = 0
    last_verify: dict[str, Any] = {}
    last_accept: dict[str, Any] = {}

    try:
        # 若尚未提供部门交付物，先触发 FP05 异步并发池跑完全部后端部门
        if not active_deliverables:
            state_store.raise_if_cancelled(run_id, boundary="backend_stage_before_pool")
            pool_res = await pool.run_phase(
                run_id=run_id,
                blueprint=current_bp,
                spec=spec,
                run_dir=run_root,
                base_dir=base_root,
                phase=FactoryPhase.BACKEND,
            )
            active_deliverables = dict(pool_res.deliverables)
            if not pool_res.ok:
                report_path = _write_integration_report(
                    run_root,
                    {
                        "run_id": run_id,
                        "status": pool_res.status,
                        "error_code": pool_res.error_code,
                        "failed_depts": pool_res.failed_depts,
                        "cancelled_depts": pool_res.cancelled_depts,
                    },
                )
                return BackendStageResult(
                    run_id=run_id,
                    ok=False,
                    status=pool_res.status,
                    integration_rounds_used=0,
                    rework_rounds=0,
                    assembled_dir=str(assembled_dir),
                    report_path=report_path,
                    deliverables=active_deliverables,
                    error_code=pool_res.error_code,
                )

        # 大循环：总装 → 强制 Docker Runner 总验收 → 失败归因定向返工（上限 max_integration_rounds=2）
        for integration_round in range(1, effective_max_rounds + 1):
            rounds_used = integration_round
            state_store.raise_if_cancelled(
                run_id,
                boundary=f"backend_integration_round_{integration_round}",
            )

            merge_res: BackendMergeResult = merge_department_deliverables(
                base_root,
                active_deliverables,
                assembled_dir,
                blueprint=current_bp,
                spec=spec,
                run_id=run_id,
                integration_round=integration_round,
            )

            # 合并门禁失败（路径冲突 / 底座或 Facade 篡改 / SQL 号段越界 / OpenAPI 路由冲突）
            if not merge_res.ok:
                persisted_conflicts: list[DefectTicket] = []
                for t in merge_res.conflict_tickets:
                    row = state_store.open_defect_ticket(
                        run_id,
                        FactoryPhase.BACKEND,
                        t.dept_id,
                        scope="integration",
                        round_num=integration_round,
                        error_class=t.error_class,
                        payload=t.model_dump(mode="json"),
                    )
                    persisted_conflicts.append(t.model_copy(update={"ticket_id": str(row.id)}))
                all_triage_tickets.extend(persisted_conflicts)

                if integration_round >= effective_max_rounds:
                    report_path = _write_integration_report(
                        run_root,
                        {
                            "run_id": run_id,
                            "status": "FAILED",
                            "error_code": "CONTRACT_MISMATCH",
                            "integration_rounds_used": rounds_used,
                            "rework_rounds": rework_rounds,
                            "conflicts": [t.model_dump(mode="json") for t in persisted_conflicts],
                        },
                    )
                    return BackendStageResult(
                        run_id=run_id,
                        ok=False,
                        status="FAILED",
                        integration_rounds_used=rounds_used,
                        rework_rounds=rework_rounds,
                        assembled_dir=str(assembled_dir),
                        report_path=report_path,
                        reworked_depts_history=reworked_history,
                        triage_tickets=all_triage_tickets,
                        deliverables=active_deliverables,
                        error_code="CONTRACT_MISMATCH",
                    )

                blamed = sorted({t.dept_id for t in persisted_conflicts})
                rework_result = await _rework_blamed_departments(
                    run_id=run_id,
                    blamed_depts=blamed,
                    tickets=persisted_conflicts,
                    blueprint=current_bp,
                    spec=spec,
                    run_root=run_root,
                    base_root=base_root,
                    pool=pool,
                    state_store=state_store,
                    active_deliverables=active_deliverables,
                )
                reworked_history.append(blamed)
                rework_rounds += 1
                if not rework_result["ok"]:
                    return _build_pool_stop_result(
                        run_id=run_id,
                        status=rework_result["status"],
                        error_code=rework_result["error_code"],
                        rounds_used=rounds_used,
                        rework_rounds=rework_rounds,
                        assembled_dir=assembled_dir,
                        run_root=run_root,
                        reworked_history=reworked_history,
                        all_triage_tickets=all_triage_tickets,
                        active_deliverables=active_deliverables,
                    )
                continue

            # 强制独立 Docker Runner 总验收（verify + accept）
            try:
                last_verify = run_final_acceptance_verify(runner_client, str(assembled_dir))
                if _is_infrastructure_report(last_verify):
                    raise _InfraStopError("Docker Runner verify 报 INFRASTRUCTURE")
                if last_verify.get("overall") == "PASS":
                    last_accept = run_final_acceptance_accept(runner_client, str(assembled_dir))
                    if _is_infrastructure_report(last_accept):
                        raise _InfraStopError("Docker Runner accept 报 INFRASTRUCTURE")
                else:
                    last_accept = {"overall": "SKIPPED", "scenarios": []}
            except _InfraStopError as infra_exc:
                state_store.record_event(
                    run_id,
                    "factory_infra_failed",
                    {
                        "phase": FactoryPhase.BACKEND.value,
                        "integration_round": integration_round,
                        "detail": str(infra_exc),
                    },
                )
                report_path = _write_integration_report(
                    run_root,
                    {
                        "run_id": run_id,
                        "status": "INFRASTRUCTURE",
                        "error_code": "INFRASTRUCTURE",
                        "integration_rounds_used": rounds_used,
                        "rework_rounds": rework_rounds,
                        "verify_report": last_verify,
                        "accept_report": last_accept,
                    },
                )
                return BackendStageResult(
                    run_id=run_id,
                    ok=False,
                    status="INFRASTRUCTURE",
                    integration_rounds_used=rounds_used,
                    rework_rounds=rework_rounds,
                    assembled_dir=str(assembled_dir),
                    report_path=report_path,
                    verify_report=last_verify,
                    accept_report=last_accept,
                    reworked_depts_history=reworked_history,
                    triage_tickets=all_triage_tickets,
                    deliverables=active_deliverables,
                    error_code="INFRASTRUCTURE",
                )
            except (RuntimeError, OSError, TimeoutError) as runner_exc:
                # Runner 不可达 / 网络断开 / 超时均归为 INFRASTRUCTURE，绝不打回部门改业务代码
                last_verify = last_verify or {
                    "overall": "INFRASTRUCTURE",
                    "error_code": "INFRASTRUCTURE",
                    "detail": str(runner_exc),
                }
                state_store.record_event(
                    run_id,
                    "factory_infra_failed",
                    {
                        "phase": FactoryPhase.BACKEND.value,
                        "integration_round": integration_round,
                        "detail": str(runner_exc)[:300],
                    },
                )
                report_path = _write_integration_report(
                    run_root,
                    {
                        "run_id": run_id,
                        "status": "INFRASTRUCTURE",
                        "error_code": "INFRASTRUCTURE",
                        "integration_rounds_used": rounds_used,
                        "rework_rounds": rework_rounds,
                        "detail": str(runner_exc),
                        "verify_report": last_verify,
                        "accept_report": last_accept,
                    },
                )
                return BackendStageResult(
                    run_id=run_id,
                    ok=False,
                    status="INFRASTRUCTURE",
                    integration_rounds_used=rounds_used,
                    rework_rounds=rework_rounds,
                    assembled_dir=str(assembled_dir),
                    report_path=report_path,
                    verify_report=last_verify,
                    accept_report=last_accept,
                    reworked_depts_history=reworked_history,
                    triage_tickets=all_triage_tickets,
                    deliverables=active_deliverables,
                    error_code="INFRASTRUCTURE",
                )

            # 总验收全 PASS → 正式冻结权威 backend/openapi.json 并传给前端流水线
            if _backend_acceptance_passed(last_verify, last_accept):
                frozen_path, frozen_sha, frozen_doc = freeze_authoritative_openapi(
                    assembled_dir,
                    merge_res.openapi_doc,
                )
                state_store.record_event(
                    run_id,
                    "factory_openapi_frozen",
                    {
                        "phase": FactoryPhase.BACKEND.value,
                        "integration_round": integration_round,
                        "openapi_sha256": frozen_sha,
                        "path_count": len(frozen_doc.get("paths", {})),
                    },
                )
                report_path = _write_integration_report(
                    run_root,
                    {
                        "run_id": run_id,
                        "status": "PASSED",
                        "integration_rounds_used": rounds_used,
                        "rework_rounds": rework_rounds,
                        "frozen_openapi_sha256": frozen_sha,
                        "reworked_depts_history": reworked_history,
                        "verify_report": last_verify,
                        "accept_report": last_accept,
                    },
                )
                return BackendStageResult(
                    run_id=run_id,
                    ok=True,
                    status="PASSED",
                    integration_rounds_used=rounds_used,
                    rework_rounds=rework_rounds,
                    assembled_dir=str(assembled_dir),
                    report_path=report_path,
                    frozen_openapi_path=str(frozen_path),
                    frozen_openapi_sha256=frozen_sha,
                    frozen_openapi_doc=frozen_doc,
                    verify_report=last_verify,
                    accept_report=last_accept,
                    reworked_depts_history=reworked_history,
                    triage_tickets=all_triage_tickets,
                    deliverables=active_deliverables,
                )

            # 业务/集成失败：若已达大循环硬上限，落 FAILED 并保留完整失败产物与报告（不冻结 OpenAPI）
            if integration_round >= effective_max_rounds:
                report_path = _write_integration_report(
                    run_root,
                    {
                        "run_id": run_id,
                        "status": "FAILED",
                        "error_code": "INTEGRATION_FAILED",
                        "integration_rounds_used": rounds_used,
                        "rework_rounds": rework_rounds,
                        "reworked_depts_history": reworked_history,
                        "verify_report": last_verify,
                        "accept_report": last_accept,
                    },
                )
                return BackendStageResult(
                    run_id=run_id,
                    ok=False,
                    status="FAILED",
                    integration_rounds_used=rounds_used,
                    rework_rounds=rework_rounds,
                    assembled_dir=str(assembled_dir),
                    report_path=report_path,
                    verify_report=last_verify,
                    accept_report=last_accept,
                    reworked_depts_history=reworked_history,
                    triage_tickets=all_triage_tickets,
                    deliverables=active_deliverables,
                    error_code="INTEGRATION_FAILED",
                )

            # 总管精准归因：仅点名打回责任部门 REWORK，其余未出错部门原地保留不重跑
            triage_tickets = chief.triage_integration_failures(
                run_id=run_id,
                phase=FactoryPhase.BACKEND,
                blueprint=current_bp,
                failure_report={
                    "verify_report": last_verify,
                    "accept_report": last_accept,
                },
                integration_round=integration_round,
            )
            all_triage_tickets.extend(triage_tickets)
            blamed_depts = sorted({t.dept_id for t in triage_tickets})

            rework_result = await _rework_blamed_departments(
                run_id=run_id,
                blamed_depts=blamed_depts,
                tickets=triage_tickets,
                blueprint=current_bp,
                spec=spec,
                run_root=run_root,
                base_root=base_root,
                pool=pool,
                state_store=state_store,
                active_deliverables=active_deliverables,
            )
            reworked_history.append(blamed_depts)
            rework_rounds += 1
            if not rework_result["ok"]:
                return _build_pool_stop_result(
                    run_id=run_id,
                    status=rework_result["status"],
                    error_code=rework_result["error_code"],
                    rounds_used=rounds_used,
                    rework_rounds=rework_rounds,
                    assembled_dir=assembled_dir,
                    run_root=run_root,
                    reworked_history=reworked_history,
                    all_triage_tickets=all_triage_tickets,
                    active_deliverables=active_deliverables,
                )
    except FactoryCancelledError:
        state_store.cancel_incomplete_departments(
            run_id, FactoryPhase.BACKEND, reason="backend_stage_cancelled"
        )
        return BackendStageResult(
            run_id=run_id,
            ok=False,
            status="CANCELLED",
            integration_rounds_used=rounds_used,
            rework_rounds=rework_rounds,
            assembled_dir=str(assembled_dir),
            reworked_depts_history=reworked_history,
            triage_tickets=all_triage_tickets,
            deliverables=active_deliverables,
            error_code="RUN_CANCELLED",
        )
    except AppException as exc:
        if exc.code == ErrorCode.BUDGET_EXCEEDED:
            return BackendStageResult(
                run_id=run_id,
                ok=False,
                status="PAUSED_BUDGET",
                integration_rounds_used=rounds_used,
                rework_rounds=rework_rounds,
                assembled_dir=str(assembled_dir),
                reworked_depts_history=reworked_history,
                triage_tickets=all_triage_tickets,
                deliverables=active_deliverables,
                error_code=ErrorCode.BUDGET_EXCEEDED.value,
            )
        raise

    return BackendStageResult(
        run_id=run_id,
        ok=False,
        status="FAILED",
        integration_rounds_used=rounds_used,
        rework_rounds=rework_rounds,
        assembled_dir=str(assembled_dir),
        verify_report=last_verify,
        accept_report=last_accept,
        reworked_depts_history=reworked_history,
        triage_tickets=all_triage_tickets,
        deliverables=active_deliverables,
        error_code="INTEGRATION_FAILED",
    )


class _InfraStopError(RuntimeError):
    pass


async def _rework_blamed_departments(
    *,
    run_id: int,
    blamed_depts: list[str],
    tickets: list[DefectTicket],
    blueprint: ArchitectureBlueprint,
    spec: SoftwareSpecification,
    run_root: Path,
    base_root: Path,
    pool: AsyncDepartmentPool,
    state_store: FactoryStateStore,
    active_deliverables: dict[str, DepartmentDeliverable],
) -> dict[str, Any]:
    """仅将责任部门从 ``PASSED`` 迁回 ``REWORK`` 并定向重跑；未出错部门原地保留不动。"""
    ticket_by_dept: dict[str, DefectTicket] = {}
    for t in tickets:
        ticket_by_dept.setdefault(t.dept_id, t)

    for dept_id in blamed_depts:
        row = state_store.get_department(run_id, FactoryPhase.BACKEND, dept_id)
        if row is not None and DeptStatus(row.status) is DeptStatus.PASSED:
            state_store.transition_department(
                run_id,
                FactoryPhase.BACKEND,
                dept_id,
                DeptStatus.REWORK,
                allow_integration_rework=True,
                reason=f"integration_triage:{ticket_by_dept[dept_id].error_class}",
            )

    rework_pool_res = await pool.run_phase(
        run_id=run_id,
        blueprint=blueprint,
        spec=spec,
        run_dir=run_root,
        base_dir=base_root,
        phase=FactoryPhase.BACKEND,
        target_depts=blamed_depts,
        rework_tickets=ticket_by_dept,
    )
    for dept_id in blamed_depts:
        if dept_id in rework_pool_res.deliverables:
            active_deliverables[dept_id] = rework_pool_res.deliverables[dept_id]
        if rework_pool_res.ok:
            state_store.resolve_department_tickets(
                run_id, FactoryPhase.BACKEND, dept_id, scope="integration"
            )
    return {
        "ok": rework_pool_res.ok,
        "status": rework_pool_res.status,
        "error_code": rework_pool_res.error_code,
    }


def _build_pool_stop_result(
    *,
    run_id: int,
    status: str,
    error_code: str | None,
    rounds_used: int,
    rework_rounds: int,
    assembled_dir: Path,
    run_root: Path,
    reworked_history: list[list[str]],
    all_triage_tickets: list[DefectTicket],
    active_deliverables: dict[str, DepartmentDeliverable],
) -> BackendStageResult:
    safe_status: Literal["PASSED", "FAILED", "INFRASTRUCTURE", "CANCELLED", "PAUSED_BUDGET"] = (
        status if status in {"FAILED", "CANCELLED", "PAUSED_BUDGET"} else "FAILED"  # type: ignore[assignment]
    )
    report_path = _write_integration_report(
        run_root,
        {
            "run_id": run_id,
            "status": safe_status,
            "error_code": error_code,
            "integration_rounds_used": rounds_used,
            "rework_rounds": rework_rounds,
            "reworked_depts_history": reworked_history,
        },
    )
    return BackendStageResult(
        run_id=run_id,
        ok=False,
        status=safe_status,
        integration_rounds_used=rounds_used,
        rework_rounds=rework_rounds,
        assembled_dir=str(assembled_dir),
        report_path=report_path,
        reworked_depts_history=reworked_history,
        triage_tickets=all_triage_tickets,
        deliverables=active_deliverables,
        error_code=error_code,
    )


__all__ = [
    "BACKEND_ASSEMBLED_DIRNAME",
    "BACKEND_INTEGRATION_REPORT_NAME",
    "BACKEND_VERIFY_REQUIRED",
    "DEFAULT_MAX_INTEGRATION_ROUNDS",
    "MAX_INTEGRATION_ROUNDS_ENV",
    "BackendStageResult",
    "max_integration_rounds_limit",
    "run_backend_factory_stage",
]
