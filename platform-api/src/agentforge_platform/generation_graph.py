"""M23 LangGraph 固定生成图。

`load_confirmed_spec → resolve_assets → plan_tasks → assemble → verify → repair → check_coverage →
resolve_gaps → review → package_or_fail`，由 worker 驱动。

- 生成图**只接收已确认的** `spec_id/spec_hash`，不读取原始聊天、不重新解释需求；
- R13 可选技术规划（`planning_client`）、R17 缺口局部 Patch（`gap_engine_factory`）、
  R18 失败自动修复（`repair_engine_factory`）、R19 可选语义审查（`review_client`）**默认关闭**：
  未注入时保持确定性转换与既有行为，不调用任何外部模型；
- R18 `repair` 只修可复现的代码/契约错误（基础设施/`NOT_RUN` 不盲改、不计业务次数），每任务 ≤2、全 run ≤6；
- R19 `review` 经 `technical_planning`（Qwen3.7 Plus）做**最多一轮**语义审查，只产出问题并转为可验证修复任务
  （登记 PENDING），**不覆盖**确定性结果；`report.json` 保留首次失败、最终复验与来源链；
- R17 `resolve_gaps` 对真实缺口「装配优先、缺口局部 Patch」：无缺口时不启动 Agent、无副作用；
  有变更（复用装配或受限 Patch）后**必须重新验证并重算覆盖**，交付包只反映最后一次验证；
- 数据库为权威状态，节点入口检查运行有效性，记录 task、事件与阶段结果；
- **未通过全部必需检查不得进入 SUCCEEDED**（package_or_fail 决定终态）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform.agent_files import FileBackend
from agentforge_platform.assembler import assemble, manifest_hash_of
from agentforge_platform.asset_finder import (
    AssetSearchResult,
    derive_capability_query,
    find_assets,
)
from agentforge_platform.asset_registry import AssetRegistry, default_registry
from agentforge_platform.clients import ModelClient
from agentforge_platform.contract import BusinessContract, compile_from_spec
from agentforge_platform.coverage import CoverageGap, analyze_coverage
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException
from agentforge_platform.gap_patch import resolve_gaps
from agentforge_platform.limits import runtime_limits
from agentforge_platform.method_index import DEFAULT_METHODS_ROOT
from agentforge_platform.method_lock import resolve_lock
from agentforge_platform.models import Project, Run, RunTask, SpecVersion
from agentforge_platform.packaging import (
    PackagingError,
    build_cost_report,
    package_run,
    verification_passed,
)
from agentforge_platform.repair import (
    CODE as REPAIR_CODE,
)
from agentforge_platform.repair import (
    repair_allowed_paths,
    run_repair_loop,
)
from agentforge_platform.resolver import AssetLock
from agentforge_platform.review import (
    build_review_context,
    findings_to_tasks,
    missing_required_checks,
    review_enabled,
    run_semantic_review,
)
from agentforge_platform.runner_client import RunnerClient
from agentforge_platform.runs import (
    RECOVERABLE_STATUSES,
    TERMINAL_STATUSES,
    RunStatus,
    append_event,
    record_artifact,
    record_task_once,
    run_directory,
    transition,
    unsettled_call_count,
)
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import require_frozen_spec
from agentforge_platform.task_planning import plan_tasks


class GenerationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GenerationState(TypedDict, total=False):
    run_id: int
    project_id: int
    spec_id: int
    spec_hash: str
    profile: str
    spec: SoftwareSpecification
    contract: BusinessContract
    lock: AssetLock
    lock_hash: str
    artifact_dir: str
    manifest_hash: str
    verification: dict
    acceptance: dict
    asset_search: dict
    search_result: AssetSearchResult
    task_plan: dict
    coverage: dict
    first_failure: dict | None
    repair: dict
    review: dict
    error_code: str
    ok: bool


@dataclass(frozen=True)
class GenerationContext:
    session_factory: sessionmaker[Session]
    runner: RunnerClient
    work_root: Path
    registry: AssetRegistry
    methods_root: Path = Path(DEFAULT_METHODS_ROOT)
    # R13：技术规划客户端（默认 None → 确定性转换，不调用 Qwen）；测试可注入假客户端。
    planning_client: ModelClient | None = None
    # R17：缺口局部 Patch 的受限引擎工厂（默认 None → 仅装配优先/保持缺口，不调用 Agent）。
    # 签名：``(allowed_paths, task_id) -> (PatchEngine, FileBackend)``。
    gap_engine_factory: Callable[[list[str], str], tuple[Any, FileBackend]] | None = None
    # R18：失败自动修复的受限引擎工厂（默认 None → 不修复）。签名：``(model_role, allowed_paths, task_id)``。
    repair_engine_factory: Callable[[str, list[str], str], tuple[Any, FileBackend]] | None = None
    # R19：可选技术语义审查客户端（默认 None → 不审查；需 technical_planning 角色开启）。
    review_client: ModelClient | None = None
    # R21：运行总时限（单调时钟截止点）；由 run_generation 设置，节点边界检查超时。
    deadline_monotonic: float | None = None
    # R23：LangGraph checkpointer（默认 None → 无检查点持久化）。检查点存于状态，**不含密钥**。
    checkpointer: Any | None = None


def _require_active(db: Session, run_id: int, expected: RunStatus) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise GenerationError("RUN_NOT_FOUND", f"run {run_id} 不存在")
    if RunStatus(run.status) in TERMINAL_STATUSES:
        raise GenerationError("RUN_INVALID_TRANSITION", f"run 已终结：{run.status}")
    if RunStatus(run.status) is not expected:
        raise GenerationError("RUN_INVALID_TRANSITION", f"期望 {expected}，实为 {run.status}")
    return run


def _check_deadline(context: GenerationContext) -> None:
    """R21：运行总时限在**节点边界**检查；超时抛 RUN_TIMEOUT（避免无限占用 Runner）。"""
    if context.deadline_monotonic is not None and time.monotonic() > context.deadline_monotonic:
        raise GenerationError("RUN_TIMEOUT", "运行超过总时限")


def _check_cancel(context: GenerationContext, run_id: int) -> None:
    """R22：在**节点边界**观察取消请求；已请求即抛 RUN_CANCELLED，停止继续派发/发新模型请求。"""
    with session_scope(context.session_factory) as db:
        run = db.get(Run, run_id)
        if run is not None and run.cancel_requested:
            raise GenerationError("RUN_CANCELLED", "运行已被取消")


def _verify_error_code(verification: dict, acceptance: dict) -> str:
    codes = [c.get("error_code") for c in verification.get("checks", [])]
    codes += [c.get("error_code") for c in acceptance.get("cases", [])]
    if "INFRASTRUCTURE" in codes:
        return "INFRASTRUCTURE"
    return "VERIFICATION_FAILED"


def build_graph(context: GenerationContext):
    def load_confirmed_spec(state: GenerationState) -> GenerationState:
        run_id = state["run_id"]
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.PLANNING)
            spec_row = db.get(SpecVersion, run.spec_id)
            if spec_row is None or spec_row.confirmed_at is None:
                raise GenerationError("SPEC_NOT_FOUND", "缺少已确认（冻结）规格")
            # R06：冻结规格必须未被篡改，且仍与本 run 记录的冻结 hash 一致。
            try:
                spec = require_frozen_spec(spec_row)
            except AppException as exc:
                raise GenerationError(str(exc.code), exc.message) from exc
            if run.spec_hash is not None and run.spec_hash != spec_row.hash:
                raise GenerationError(
                    "SPEC_TAMPERED", "冻结规格 hash 与运行时记录不一致（疑似被篡改）"
                )
            contract = compile_from_spec(spec)
            record_task_once(db, run, "load_confirmed_spec", status="DONE")
            append_event(
                db, run, "stage",
                json.dumps({"stage": "load_confirmed_spec", "spec_hash": spec_row.hash}),
            )
            return {
                "project_id": run.project_id,
                "spec_id": run.spec_id,
                "spec_hash": spec_row.hash,
                "profile": spec.technical_profile.profile_id,
                "spec": spec,
                "contract": contract,
            }

    def resolve_assets_node(state: GenerationState) -> GenerationState:
        run_id = state["run_id"]
        # R23：恢复时**复用已持久化的 asset-lock**，不因 FTS5 索引变化而重新选取方法单元。
        persisted_lock = run_directory(context.work_root, run_id) / "asset-lock.json"
        if persisted_lock.is_file():
            lock = AssetLock.model_validate_json(persisted_lock.read_text(encoding="utf-8"))
        else:
            # R06D：先在 MVP 路径锁定模板/能力包；规格显式请求的预审核方法经 Finder 筛选后并入（锁升 0.2）。
            lock = resolve_lock(
                context.registry, state["profile"], state["spec"], state["contract"],
                methods_root_path=context.methods_root,
            )
        # R12：记录 Finder 审计（候选方法 / 复用方法 ID+版本 / 未覆盖规则），随 run 报告留痕。
        search = find_assets(
            derive_capability_query(state["spec"], state["contract"]),
            registry=context.registry, methods_root_path=context.methods_root,
        )
        audit = {
            "query_hash": search.query_hash,
            "index_revision": search.index_revision,
            "match_type": search.match_type,
            "candidate_refs": list(search.candidate_refs),
            "selected_refs": list(search.selected_refs),
            "filter_reasons": {ref: list(reasons) for ref, reasons in search.filter_reasons.items()},
            "uncovered_acceptance_ids": list(search.uncovered_acceptance_ids),
            "reused_method_units": [unit.model_dump() for unit in lock.method_units],
        }
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.PLANNING)
            record_task_once(db, run, "resolve_assets", status="DONE")
            append_event(
                db, run, "stage",
                json.dumps({"stage": "resolve_assets", "lock_hash": lock.lock_hash}),
            )
            append_event(db, run, "asset_search", json.dumps({
                "match_type": audit["match_type"],
                "selected_refs": audit["selected_refs"],
                "uncovered_acceptance_ids": audit["uncovered_acceptance_ids"],
            }))
        return {
            "lock_hash": lock.lock_hash, "lock": lock,
            "asset_search": audit, "search_result": search,
        }

    def plan_tasks_node(state: GenerationState) -> GenerationState:
        run_id = state["run_id"]
        plan_file = run_directory(context.work_root, run_id) / "task-plan.json"
        if plan_file.is_file():
            # R23：恢复时复用已持久化的计划，**不重复调用技术规划模型**。
            return {"task_plan": json.loads(plan_file.read_text(encoding="utf-8"))}
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.PLANNING)
            project = db.get(Project, run.project_id)
            assert project is not None
            plan, fallback_reasons = plan_tasks(
                db, project, state["spec"], state["contract"],
                state["search_result"], state["lock"],
                run_id=run_id, client=context.planning_client,
            )
            record_task_once(db, run, "plan_tasks", status="DONE")
            append_event(db, run, "task_plan", json.dumps({
                "execution_mode": plan.execution_mode,
                "task_ids": [task.task_id for task in plan.tasks],
                "uncovered_acceptance_ids": plan.uncovered_acceptance_ids,
                "fallback_reasons": fallback_reasons,
            }))
        payload = plan.model_dump(mode="json")
        plan_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"task_plan": payload}

    def assemble_node(state: GenerationState) -> GenerationState:
        run_id = state["run_id"]
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.PLANNING)
            transition(db, run, RunStatus.GENERATING)
        target = run_directory(context.work_root, run_id)
        staging_left = target.with_name(f"{target.name}.staging")
        # R23：已完成装配（有 asset-lock.json/spec.json 且无半成品暂存）→ 跳过，**不覆盖既有产物**。
        already = (
            (target / "asset-lock.json").is_file()
            and (target / "spec.json").is_file()
            and not staging_left.exists()
        )
        if already:
            manifest_hash = manifest_hash_of(target)
        else:
            result = assemble(
                state["contract"], state["lock"], context.registry, target,
                spec=state["spec"], methods_root=context.methods_root,
            )
            manifest_hash = result.manifest_hash
            with session_scope(context.session_factory) as db:
                run = _require_active(db, run_id, RunStatus.GENERATING)
                record_artifact(db, run, ".", manifest_hash, "source_tree")
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.GENERATING)
            record_task_once(db, run, "assemble", status="DONE")
            append_event(
                db, run, "stage",
                json.dumps({"stage": "assemble", "manifest_hash": manifest_hash,
                            "reused": already}),
            )
        return {"artifact_dir": target.name, "manifest_hash": manifest_hash}

    def verify_node(state: GenerationState) -> GenerationState:
        run_id = state["run_id"]
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.GENERATING)
            transition(db, run, RunStatus.VERIFYING)
        verification = context.runner.verify(state["artifact_dir"])
        acceptance = context.runner.accept(state["artifact_dir"])
        passed = verification_passed(verification, acceptance)
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.VERIFYING)
            record_task_once(db, run, "verify", status="DONE")
            append_event(
                db, run, "verification",
                json.dumps({"verify": verification.get("overall"),
                            "accept": acceptance.get("overall"),
                            "missing_required": missing_required_checks(verification)}),
            )
        return {
            "verification": verification,
            "acceptance": acceptance,
            # R19：保留**首次**失败快照（修复前后对照）；首次即通过则为 None。
            "first_failure": None if passed else {
                "verification": verification, "acceptance": acceptance,
            },
        }

    def repair_node(state: GenerationState) -> GenerationState:
        """R18：失败分类与有限自动修复；基础设施故障不计业务次数、不盲改。"""
        if context.repair_engine_factory is None:
            return {}
        run_id = state["run_id"]
        contract = state["contract"]
        allowed_paths = repair_allowed_paths(contract)
        run_dir = run_directory(context.work_root, run_id)
        with session_scope(context.session_factory) as db:
            used = db.query(RunTask).filter(
                RunTask.run_id == run_id, RunTask.task_id.like("repair:%")
            ).all()
            run_used = sum(int(row.attempt or 0) for row in used)
        outcome = run_repair_loop(
            verify=context.runner.verify, accept=context.runner.accept,
            artifact_dir=state["artifact_dir"], contract=contract, run_dir=run_dir,
            allowed_paths=allowed_paths, task_id="repair:verify",
            engine_factory=context.repair_engine_factory, run_used=run_used,
            search=state.get("search_result"), spec=state["spec"],
            methods_root=context.methods_root,
        )
        if outcome.classification != REPAIR_CODE:
            with session_scope(context.session_factory) as db:
                run = _require_active(db, run_id, RunStatus.VERIFYING)
                append_event(db, run, "repair", json.dumps({
                    "classification": outcome.classification, "attempts": 0, "roles": [],
                }))
            return {}
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.VERIFYING)
            row = (
                db.query(RunTask)
                .filter(RunTask.run_id == run_id, RunTask.task_id == "repair:verify")
                .one_or_none()
            )
            if row is None:
                row = record_task_once(db, run, "repair:verify", status="PENDING")
            row.attempt = run_used + outcome.attempts
            row.status = "DONE" if outcome.passed else "FAILED"
            append_event(db, run, "repair", json.dumps({
                "classification": outcome.classification, "attempts": outcome.attempts,
                "roles": outcome.roles, "passed": outcome.passed,
                "replaced_refs": outcome.replaced_refs,
            }))
        if outcome.attempts == 0:
            return {}
        return {
            "verification": outcome.verification,
            "acceptance": outcome.acceptance,
            "repair": {
                "classification": outcome.classification,
                "attempts": outcome.attempts,
                "roles": outcome.roles,
                "replaced_refs": outcome.replaced_refs,
                "passed": outcome.passed,
            },
        }

    def check_coverage_node(state: GenerationState) -> GenerationState:
        run_id = state["run_id"]
        report = analyze_coverage(
            state["spec"], state["contract"], state["lock"],
            state["search_result"], state["acceptance"],
            methods_root=context.methods_root,
        )
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.VERIFYING)
            record_task_once(db, run, "check_coverage", status="DONE")
            for gap in report.gaps:  # 仅对真实缺口登记 AGENT_PATCH 任务（本步不执行）
                record_task_once(db, run, gap.task_id, status="PENDING")
            append_event(db, run, "coverage", json.dumps({
                "mainline": report.mainline,
                "acceptance_passed": report.acceptance_passed,
                "covered_acceptance_ids": list(report.covered_acceptance_ids),
                "gaps": [{"acceptance_id": g.acceptance_id, "code": g.code} for g in report.gaps],
            }))
        return {"coverage": {
            "mainline": report.mainline,
            "acceptance_passed": report.acceptance_passed,
            "covered_acceptance_ids": list(report.covered_acceptance_ids),
            "gaps": [
                {"acceptance_id": g.acceptance_id, "code": g.code, "reason": g.reason,
                 "allowed_paths": list(g.allowed_paths), "task_id": g.task_id, "mode": g.mode}
                for g in report.gaps
            ],
        }}

    def resolve_gaps_node(state: GenerationState) -> GenerationState:
        """R17：对真实缺口「装配优先，缺口局部 Patch」。无缺口则不启动 Agent、无副作用。"""
        run_id = state["run_id"]
        coverage = state.get("coverage") or {}
        gaps_payload = coverage.get("gaps", [])
        if not gaps_payload:
            return {}
        run_dir = run_directory(context.work_root, run_id)
        gaps = [
            CoverageGap(
                acceptance_id=g["acceptance_id"], code=g["code"], reason=g["reason"],
                allowed_paths=tuple(g["allowed_paths"]), task_id=g["task_id"],
                mode=g.get("mode", "agent_patch"),
            )
            for g in gaps_payload
        ]
        resolutions = resolve_gaps(
            gaps, search=state["search_result"], spec=state["spec"], contract=state["contract"],
            lock=state["lock"], run_dir=run_dir, methods_root=context.methods_root,
            engine_factory=context.gap_engine_factory,
        )
        status_by_action = {"reuse": "DONE", "patch": "DONE", "refuse": "PENDING"}
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.VERIFYING)
            for resolution in resolutions:
                row = (
                    db.query(RunTask)
                    .filter(RunTask.run_id == run_id, RunTask.task_id == resolution.task_id)
                    .one_or_none()
                )
                if row is not None:
                    row.status = status_by_action[resolution.action]
            append_event(db, run, "gap_patch", json.dumps({
                "resolutions": [
                    {"task_id": r.task_id, "acceptance_id": r.acceptance_id, "action": r.action}
                    for r in resolutions
                ],
                "agent_calls": sum(1 for r in resolutions if r.action == "patch"),
            }))

        applied = [r for r in resolutions if r.action != "refuse"]
        if not applied:
            return {}
        # 变更后必须重新验证与重算覆盖：交付包只反映最后一次验证结果。
        verification = context.runner.verify(state["artifact_dir"])
        acceptance = context.runner.accept(state["artifact_dir"])
        reused_units = [r.detail["locked_unit"] for r in resolutions if r.action == "reuse"]
        lock = state["lock"]
        if reused_units:
            existing = {(u.id, u.version) for u in lock.method_units}
            extra = [u for u in reused_units if (u["id"], u["version"]) not in existing]
            from agentforge_platform.resolver import LockedAsset

            lock = lock.model_copy(update={
                "method_units": list(lock.method_units) + [LockedAsset(**u) for u in extra],
            })
        report = analyze_coverage(
            state["spec"], state["contract"], lock,
            state["search_result"], acceptance, methods_root=context.methods_root,
        )
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.VERIFYING)
            append_event(db, run, "verification", json.dumps({
                "verify": verification.get("overall"), "accept": acceptance.get("overall"),
                "after": "gap_patch",
            }))
        return {
            "verification": verification,
            "acceptance": acceptance,
            "coverage": {
                "mainline": report.mainline,
                "acceptance_passed": report.acceptance_passed,
                "covered_acceptance_ids": list(report.covered_acceptance_ids),
                "gaps": [
                    {"acceptance_id": g.acceptance_id, "code": g.code, "reason": g.reason,
                     "allowed_paths": list(g.allowed_paths), "task_id": g.task_id, "mode": g.mode}
                    for g in report.gaps
                ],
                "resolutions": [
                    {"task_id": r.task_id, "acceptance_id": r.acceptance_id, "action": r.action,
                     "reason": r.reason}
                    for r in resolutions
                ],
            },
        }

    def review_node(state: GenerationState) -> GenerationState:
        """R19：**最多一轮**技术语义审查（可选）。只产出问题并转成可验证修复任务，不覆盖确定性结果。"""
        if context.review_client is None or not review_enabled():
            return {}
        run_id = state["run_id"]
        run_dir = run_directory(context.work_root, run_id)
        review_context = build_review_context(
            state["spec"], state["contract"], state["verification"], state["acceptance"],
            run_dir=run_dir,
        )
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.VERIFYING)
            result = run_semantic_review(
                db, project_id=run.project_id, run_id=run_id,
                client=context.review_client, context=review_context, max_rounds=1,
            )
        tasks = findings_to_tasks(result.findings, state["contract"]) if result.invoked else []
        review_payload = {
            "invoked": result.invoked,
            "model_role": result.model_role,
            "error": result.error,
            "findings": [finding.model_dump(mode="json") for finding in result.findings],
            "tasks": tasks,
        }
        (run_dir / "review.json").write_text(
            json.dumps(review_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.VERIFYING)
            for task in tasks:  # 仅登记为 PENDING 修复任务，不在本步执行、不改冻结规格/固定测试
                record_task_once(db, run, task["task_id"], status="PENDING")
            append_event(db, run, "review", json.dumps({
                "invoked": result.invoked, "findings": len(result.findings),
                "tasks": [task["task_id"] for task in tasks], "error": result.error,
            }))
        return {"review": review_payload}

    def package_or_fail(state: GenerationState) -> GenerationState:
        run_id = state["run_id"]
        verification = state["verification"]
        acceptance = state["acceptance"]
        ok = verification_passed(verification, acceptance)
        run_dir = run_directory(context.work_root, run_id)
        if (run_dir / "source.zip").is_file():
            # R23：已打包（崩溃恢复续跑）→ 不重复打包/不覆盖既有产物；仅按当前复验结果落终态。
            with session_scope(context.session_factory) as db:
                run = _require_active(db, run_id, RunStatus.VERIFYING)
                if ok:
                    transition(db, run, RunStatus.SUCCEEDED)
                else:
                    transition(db, run, RunStatus.FAILED,
                               error_code=_verify_error_code(verification, acceptance))
                append_event(db, run, "recovered", json.dumps({"skip": "package"}))
            return {"ok": ok, "error_code": "" if ok else _verify_error_code(verification, acceptance)}

        # R12：Finder 审计随交付包留痕（候选方法 / 复用方法 ID+版本 / 未覆盖规则）。
        audit = state.get("asset_search")
        if audit is not None:
            (run_dir / "asset-search.json").write_text(
                json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        task_plan = state.get("task_plan")
        if task_plan is not None:
            (run_dir / "task-plan.json").write_text(
                json.dumps(task_plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        coverage = state.get("coverage")
        if coverage is not None:
            (run_dir / "coverage.json").write_text(
                json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )

        # R19：报告保留首次失败与最终复验、以及索引版本/采用-拒绝方法/装配与 Agent 修改的来源链。
        report = {
            "verified": ok,
            "missing_required_checks": missing_required_checks(verification),
            "first_failure": state.get("first_failure"),
            "final_verification": {"verification": verification, "acceptance": acceptance},
            "provenance": {
                "lock_hash": state.get("lock_hash"),
                "manifest_hash": state.get("manifest_hash"),
                "index_revision": (audit or {}).get("index_revision"),
                "adopted_method_units": (audit or {}).get("reused_method_units", []),
                "selected_refs": (audit or {}).get("selected_refs", []),
                "rejected": (audit or {}).get("filter_reasons", {}),
                "assembly": {"manifest_hash": state.get("manifest_hash"),
                             "lock_hash": state.get("lock_hash")},
                "agent_modifications": {
                    "gap_resolutions": (coverage or {}).get("resolutions", []),
                    "repair": state.get("repair"),
                },
            },
            "semantic_review": state.get("review"),
        }
        (run_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        with session_scope(context.session_factory) as db:
            cost_report = build_cost_report(db, project_id=state["project_id"], run_id=run_id)

        try:
            package = package_run(
                run_dir,
                spec=state["spec"],
                lock=state["lock"],
                contract=state["contract"],
                verification=verification,
                acceptance=acceptance,
                cost_report=cost_report,
            )
        except PackagingError as exc:
            with session_scope(context.session_factory) as db:
                run = _require_active(db, run_id, RunStatus.VERIFYING)
                transition(db, run, RunStatus.FAILED, error_code=exc.code)
                append_event(db, run, "failed", json.dumps({"code": exc.code, "stage": "package"}))
            return {"ok": False, "error_code": exc.code}

        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.VERIFYING)
            record_artifact(
                db, run, package.zip_path, package.zip_sha256, "zip", verified=package.verified
            )
            append_event(
                db, run, "packaged",
                json.dumps({"zip": package.zip_path, "files": package.file_count,
                            "verified": package.verified}),
            )
            if ok:
                transition(db, run, RunStatus.SUCCEEDED)
                append_event(db, run, "succeeded", "{}")
                return {"ok": True, "error_code": ""}
            code = _verify_error_code(verification, acceptance)
            transition(db, run, RunStatus.FAILED, error_code=code)
            append_event(db, run, "failed", json.dumps({"code": code, "stage": "verify"}))
            return {"ok": False, "error_code": code}

    graph = StateGraph(GenerationState)

    def guarded(node):
        def _wrapped(state: GenerationState) -> GenerationState:
            _check_deadline(context)          # R21：每个节点入口检查运行总时限
            _check_cancel(context, state["run_id"])  # R22：每个节点入口观察取消请求
            return node(state)

        return _wrapped

    graph.add_node("load_confirmed_spec", guarded(load_confirmed_spec))
    graph.add_node("resolve_assets", guarded(resolve_assets_node))
    graph.add_node("plan_tasks", guarded(plan_tasks_node))
    graph.add_node("assemble", guarded(assemble_node))
    graph.add_node("verify", guarded(verify_node))
    graph.add_node("repair", guarded(repair_node))
    graph.add_node("check_coverage", guarded(check_coverage_node))
    graph.add_node("resolve_gaps", guarded(resolve_gaps_node))
    graph.add_node("review", guarded(review_node))
    graph.add_node("package_or_fail", guarded(package_or_fail))
    graph.add_edge(START, "load_confirmed_spec")
    graph.add_edge("load_confirmed_spec", "resolve_assets")
    graph.add_edge("resolve_assets", "plan_tasks")
    graph.add_edge("plan_tasks", "assemble")
    graph.add_edge("assemble", "verify")
    graph.add_edge("verify", "repair")
    graph.add_edge("repair", "check_coverage")
    graph.add_edge("check_coverage", "resolve_gaps")
    graph.add_edge("resolve_gaps", "review")
    graph.add_edge("review", "package_or_fail")
    graph.add_edge("package_or_fail", END)
    if context.checkpointer is not None:
        return graph.compile(checkpointer=context.checkpointer)  # R23：检查点持久化（不含密钥）
    return graph.compile()


def _cleanup_runner(context: GenerationContext, run_id: int) -> None:
    """R21：运行超时后**尽力**触发 Runner 清理（客户端可选实现 ``cleanup``）。"""
    cleanup = getattr(context.runner, "cleanup", None)
    if cleanup is None:
        return
    try:
        cleanup(run_directory(context.work_root, run_id).name)
    except Exception:  # noqa: BLE001  # 清理失败不影响失败落库与账本
        return


def run_generation(context: GenerationContext, run_id: int) -> GenerationState:
    """worker 处理入口：QUEUED→PLANNING 后驱动生成图；异常落 FAILED 并留事件。"""
    context = replace(
        context, deadline_monotonic=time.monotonic() + runtime_limits().run_timeout_seconds
    )
    with session_scope(context.session_factory) as db:
        run = db.get(Run, run_id)
        if run is None:
            raise GenerationError("RUN_NOT_FOUND", f"run {run_id} 不存在")
        if run.cancel_requested:  # R22：领取前已请求取消 → 不派发任何工作
            if RunStatus(run.status) not in TERMINAL_STATUSES:
                transition(db, run, RunStatus.CANCELLED)
                append_event(db, run, "cancelled", json.dumps({"stage": "pre_claim"}))
            return {"run_id": run_id, "ok": False, "error_code": "RUN_CANCELLED"}
        recovering = RunStatus(run.status) in RECOVERABLE_STATUSES
        if recovering:
            # R23：崩溃恢复——状态不明（RESERVED/UNKNOWN）的模型调用须人工/账单对账，**不盲目重发**。
            if unsettled_call_count(db, run_id) > 0:
                transition(db, run, RunStatus.FAILED, error_code="NEEDS_RECONCILIATION")
                append_event(db, run, "failed", json.dumps({
                    "code": "NEEDS_RECONCILIATION", "stage": "recover",
                }))
                return {"run_id": run_id, "ok": False, "error_code": "NEEDS_RECONCILIATION"}
            # 回退到 PLANNING 以**节点幂等**重放：已提交任务（锁/装配/计划）复用、不重做、不覆盖。
            previous = str(run.status)
            run.status = RunStatus.PLANNING
            db.flush()
            append_event(db, run, "recovered", json.dumps({"from": previous}))
        else:
            transition(db, run, RunStatus.PLANNING)
            append_event(db, run, "claimed", "{}")

    config = {"configurable": {"thread_id": f"generation:{run_id}"}} if context.checkpointer else None
    try:
        return build_graph(context).invoke({"run_id": run_id}, config=config)
    except Exception as exc:  # noqa: BLE001  # 兜底落终态，避免未捕获异常造成假成功
        code = getattr(exc, "code", None) or type(exc).__name__
        if code in ("RUN_TIMEOUT", "RUN_CANCELLED"):
            _cleanup_runner(context, run_id)  # 尽力停止在途、释放受控资源
        final = RunStatus.CANCELLED if code == "RUN_CANCELLED" else RunStatus.FAILED
        event_type = "cancelled" if code == "RUN_CANCELLED" else "failed"
        with session_scope(context.session_factory) as db:
            run = db.get(Run, run_id)
            if run is not None and RunStatus(run.status) not in TERMINAL_STATUSES:
                transition(db, run, final, error_code=str(code) if final is RunStatus.FAILED else None)
            if run is not None:
                append_event(db, run, event_type, json.dumps({"code": str(code)}))
        return {"run_id": run_id, "ok": False, "error_code": str(code)}


def default_context(
    *,
    session_factory: sessionmaker[Session],
    work_root: str | Path,
    runner: RunnerClient,
    methods_root: str | Path = DEFAULT_METHODS_ROOT,
) -> GenerationContext:
    return GenerationContext(
        session_factory=session_factory,
        runner=runner,
        work_root=Path(work_root),
        registry=default_registry(),
        methods_root=Path(methods_root),
    )


__all__ = [
    "GenerationContext",
    "GenerationError",
    "GenerationState",
    "build_graph",
    "default_context",
    "run_generation",
]
