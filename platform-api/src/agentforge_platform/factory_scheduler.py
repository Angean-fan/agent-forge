"""FP05：后端（及通用）多部门异步并行调度池（``factory_scheduler.py``）。

核心能力：
1. **真正异步并发 + 双信号量限流**：
   - ``llm_dept_semaphore``（默认 3，可配 ``AGENTFORGE_FACTORY_MAX_CONCURRENT_DEPTS``）：限制同时执行
     规划/编码的部门数；
   - ``verifier_semaphore``（默认 2，可配 ``AGENTFORGE_FACTORY_MAX_CONCURRENT_VERIFIERS``）：限制同时
     触发单模块编译/测试验证器（Maven/Vitest）的进程数，防爆本机内存；
2. **单部门小循环与异常隔离**：
   - 单部门小循环硬上限 ``max_dept_rounds=3``（可配），支持第 3 轮可选模型升级；
   - 任一部门失败或抛出未预期异常时隔离在单部门内，``try/finally`` 释放信号量、清理脏 ``.staging/``
     暂存并释放未发出的 ``RESERVED`` 预算预留，绝不拖垮或污染其他部门；
3. **预算耗尽与取消平滑收敛**：
   - 遇到 ``BUDGET_EXCEEDED`` 或 ``Run.cancel_requested`` 时立即置位停止事件，不再派发新一轮任务，
     保留已 ``PASSED`` 部门检查点以支持加预算续跑；
4. **契约变更正规升级协议（每 run ≤ 2 次）**：
   - 部门遇到跨模块契约缺口时通过 :class:`ContractChangeRequest`（复用 ``module_coordinator.ChangeRequest``
     校验防环/防越权）升级交总管审批 → 升级 ``blueprint.version`` 并落库 ``APPROVED_FOR_BUILD`` →
     重新生成只读 Facade Stub → **仅触发提供方与受影响消费方部门局部重跑**，其余部门复用已有成果。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform.agent_files import STAGING_DIRNAME
from agentforge_platform.asset_cache import make_writable_tree
from agentforge_platform.blueprint_contracts import (
    ArchitectureBlueprint,
    generate_java_facade_stubs,
    validate_blueprint,
)
from agentforge_platform.budget_pg import PgBudgetLedger
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.factory_base_builder import (
    READONLY_MANIFEST_NAME,
    BaseManifestEntry,
    _compute_manifest_hash,
    _sha256_bytes,
    load_base_manifest,
)
from agentforge_platform.factory_department import DepartmentRunner
from agentforge_platform.factory_state import (
    BlueprintStatus,
    DeptStatus,
    FactoryCancelledError,
    FactoryPhase,
    FactoryStateStore,
)
from agentforge_platform.factory_tickets import (
    DefectTicket,
    DepartmentDeliverable,
    TestEvidence,
)
from agentforge_platform.module_coordinator import (
    KIND_CAPABILITY,
    KIND_SCOPE,
    STATUS_PENDING,
    ChangeRequest,
    ModuleCoordinator,
)
from agentforge_platform.module_registry import Module
from agentforge_platform.module_verifier import ModuleVerificationResult, ModuleVerifier
from agentforge_platform.spec import SoftwareSpecification

_STRICT = ConfigDict(extra="forbid")

CONCURRENT_DEPTS_ENV = "AGENTFORGE_FACTORY_MAX_CONCURRENT_DEPTS"
CONCURRENT_VERIFIERS_ENV = "AGENTFORGE_FACTORY_MAX_CONCURRENT_VERIFIERS"
MAX_DEPT_ROUNDS_ENV = "AGENTFORGE_FACTORY_MAX_DEPT_ROUNDS"
ROUND3_ESCALATION_ROLE_ENV = "AGENTFORGE_FACTORY_ROUND3_ESCALATION_ROLE"
FAIL_FAST_ENV = "AGENTFORGE_FACTORY_FAIL_FAST"

DEFAULT_MAX_CONCURRENT_DEPTS = 3
DEFAULT_MAX_CONCURRENT_VERIFIERS = 2
DEFAULT_MAX_DEPT_ROUNDS = 3
DEFAULT_MAX_CONTRACT_CHANGES_PER_RUN = 2


def factory_fail_fast(*, default: bool = False) -> bool:
    """是否开启同阶段单部门失败即早停（Fail-Fast），避免其余部门继续白烧 Token。"""
    raw = os.environ.get(FAIL_FAST_ENV, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _read_positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def max_concurrent_depts() -> int:
    return _read_positive_int_env(CONCURRENT_DEPTS_ENV, DEFAULT_MAX_CONCURRENT_DEPTS)


def max_concurrent_verifiers() -> int:
    return _read_positive_int_env(CONCURRENT_VERIFIERS_ENV, DEFAULT_MAX_CONCURRENT_VERIFIERS)


def max_dept_rounds() -> int:
    return _read_positive_int_env(MAX_DEPT_ROUNDS_ENV, DEFAULT_MAX_DEPT_ROUNDS)


def round3_escalation_role() -> str | None:
    raw = os.environ.get(ROUND3_ESCALATION_ROLE_ENV, "").strip()
    return raw or None


class ContractChangeRequest(BaseModel):
    """部门向调度池与项目总管发起的跨模块契约变更升级请求。"""

    model_config = _STRICT

    request_id: str
    requester_dept: str
    target_dept: str
    capability_id: str
    required_contract: str
    reason: str
    kind: Literal["CAPABILITY_REQUEST", "SCOPE_CHANGE"] = KIND_CAPABILITY

    def to_coordinator_request(self) -> ChangeRequest:
        return ChangeRequest(
            request_id=self.request_id,
            requester_module=self.requester_dept,
            target_module=self.target_dept,
            capability_id=self.capability_id,
            required_contract=self.required_contract,
            change_reason=self.reason,
            kind=self.kind,
        )


class ContractChangeEscalation(Exception):
    """部门执行过程中发现契约缺口时抛出的受控升级异常（禁止私改他部门 Facade）。"""

    def __init__(self, request: ContractChangeRequest) -> None:
        super().__init__(
            f"部门 {request.requester_dept} 请求升级 {request.target_dept} 契约 {request.capability_id}"
        )
        self.request = request


class ContractChangeDecision(BaseModel):
    """总管对契约变更请求的审批裁决。"""

    model_config = _STRICT

    approved: bool
    updated_blueprint: ArchitectureBlueprint | None = None
    affected_depts: list[str] = []
    reason: str = ""


class DepartmentTelemetry(BaseModel):
    """单部门并发执行遥测（用于审计真实并行度与检查点复用）。"""

    model_config = _STRICT

    dept_id: str
    started_at_monotonic: float
    finished_at_monotonic: float
    status: str
    rounds_used: int = 0
    reused_from_checkpoint: bool = False
    error_code: str | None = None


class PoolRunResult(BaseModel):
    """多部门异步并发调度池阶段执行汇总结果。"""

    model_config = _STRICT

    run_id: int
    phase: Literal["backend", "frontend"]
    ok: bool
    status: Literal["PASSED", "FAILED", "CANCELLED", "PAUSED_BUDGET"]
    blueprint_version: int
    deliverables: dict[str, DepartmentDeliverable] = {}
    telemetry: dict[str, DepartmentTelemetry] = {}
    reused_depts: list[str] = []
    executed_depts: list[str] = []
    failed_depts: list[str] = []
    cancelled_depts: list[str] = []
    contract_changes_applied: int = 0
    max_observed_llm_concurrency: int = 0
    max_observed_verifier_concurrency: int = 0
    error_code: str | None = None


ContractChangeApprover = Callable[
    [ContractChangeRequest, ArchitectureBlueprint, int],
    ContractChangeDecision,
]
DepartmentRunnerFactory = Callable[[Module, ModuleVerifier, str | None], DepartmentRunner]
DepartmentExecutor = Callable[
    [int, Module, ArchitectureBlueprint, ModuleVerifier, DefectTicket | None],
    Awaitable[DepartmentDeliverable] | DepartmentDeliverable,
]


class _ConcurrencyTracker:
    """线程安全的并发度峰值计数器（记录 LLM 与 Verifier 双信号量的真实峰值并发）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active_llm = 0
        self.max_llm = 0
        self.active_verifier = 0
        self.max_verifier = 0

    def enter_llm(self) -> int:
        with self._lock:
            self.active_llm += 1
            self.max_llm = max(self.max_llm, self.active_llm)
            return self.active_llm

    def exit_llm(self) -> None:
        with self._lock:
            self.active_llm = max(0, self.active_llm - 1)

    def enter_verifier(self) -> int:
        with self._lock:
            self.active_verifier += 1
            self.max_verifier = max(self.max_verifier, self.active_verifier)
            return self.active_verifier

    def exit_verifier(self) -> None:
        with self._lock:
            self.active_verifier = max(0, self.active_verifier - 1)


class _SemaphoreGuardedVerifier:
    """包装 :class:`ModuleVerifier`，跨线程强制受 ``verifier_semaphore`` 限流。"""

    def __init__(
        self,
        inner: ModuleVerifier,
        *,
        semaphore: asyncio.Semaphore,
        loop: asyncio.AbstractEventLoop,
        tracker: _ConcurrencyTracker,
    ) -> None:
        self._inner = inner
        self._semaphore = semaphore
        self._loop = loop
        self._tracker = tracker
        self.verifier_kind = inner.verifier_kind

    def verify_module(
        self,
        *,
        artifact_dir: str | Path,
        module_id: str,
        stack: str = "java",
        test_selector: str = "*Test",
        timeout_seconds: int = 120,
    ) -> ModuleVerificationResult:
        async def _acquire() -> None:
            await self._semaphore.acquire()

        fut = asyncio.run_coroutine_threadsafe(_acquire(), self._loop)
        fut.result()
        self._tracker.enter_verifier()
        try:
            return self._inner.verify_module(
                artifact_dir=artifact_dir,
                module_id=module_id,
                stack=stack,
                test_selector=test_selector,
                timeout_seconds=timeout_seconds,
            )
        finally:
            self._tracker.exit_verifier()
            self._loop.call_soon_threadsafe(self._semaphore.release)


def _cleanup_dirty_staging(run_dir: Path, phase: FactoryPhase, dept_id: str) -> None:
    """清理单部门未提交的 ``.staging/`` 临时目录，避免异常中断留下脏文件。"""
    dirty = run_dir / "staging" / phase.value / dept_id / STAGING_DIRNAME
    if dirty.exists():
        shutil.rmtree(dirty, ignore_errors=True)


def _refresh_base_and_staging_stubs(
    *,
    blueprint: ArchitectureBlueprint,
    run_dir: Path,
    base_dir: Path | None,
    phase: FactoryPhase,
    affected_depts: Sequence[str],
) -> list[str]:
    """契约升级后重新生成只读 Facade Stub，并同步更新受影响部门的暂存区只读接口视图。"""
    if base_dir is None or not base_dir.is_dir():
        return []

    resolved_base = base_dir.resolve()
    make_writable_tree(resolved_base)
    regenerated = generate_java_facade_stubs(blueprint, resolved_base)

    # 同步刷新 readonly-manifest.json，确保后续总装完整性门禁认可升级后的 Stub 哈希
    manifest_file = resolved_base / READONLY_MANIFEST_NAME
    if manifest_file.is_file():
        old_manifest = load_base_manifest(manifest_file)
        pkg_rel = blueprint.base_package.replace(".", "/")
        dto_paths = {
            f"backend/src/main/java/{pkg_rel}/modules/{dto.owner_module_id}/api/{dto.name}.java"
            for dto in blueprint.shared_dtos
        }
        entries_by_path: dict[str, BaseManifestEntry] = {
            e.path: e for e in old_manifest.files if e.category not in {"facade_stub", "shared_dto"}
        }
        facade_paths: list[str] = []
        shared_dto_paths: list[str] = []
        for rel in regenerated:
            data = (resolved_base / rel).read_bytes()
            cat = "shared_dto" if rel in dto_paths else "facade_stub"
            if cat == "shared_dto":
                shared_dto_paths.append(rel)
            else:
                facade_paths.append(rel)
            entries_by_path[rel] = BaseManifestEntry(
                path=rel,
                sha256=_sha256_bytes(data),
                size=len(data),
                category=cat,
                protected=True,
            )
        new_entries = [entries_by_path[k] for k in sorted(entries_by_path)]
        new_manifest = old_manifest.model_copy(
            update={
                "blueprint_hash": blueprint.blueprint_hash(),
                "blueprint_version": blueprint.blueprint_version,
                "facade_stub_paths": sorted(facade_paths),
                "shared_dto_paths": sorted(shared_dto_paths),
                "manifest_hash": _compute_manifest_hash(new_entries),
                "files": new_entries,
            }
        )
        manifest_file.write_text(
            json.dumps(
                new_manifest.model_dump(mode="json"),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    # 恢复 base_dir 文件只读权限
    for dirpath, _dirnames, filenames in os.walk(resolved_base, topdown=False, followlinks=False):
        here = Path(dirpath)
        for fname in filenames:
            fpath = here / fname
            if not fpath.is_symlink():
                fpath.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    # 将更新后的只读 Facade Stub 同步到受影响部门暂存区（未受影响部门零触碰）
    for dept_id in affected_depts:
        dept_staging = run_dir / "staging" / phase.value / dept_id
        if not dept_staging.is_dir():
            continue
        for rel in regenerated:
            src = resolved_base / rel
            dst = dept_staging / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                dst.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            shutil.copy2(src, dst)

    return regenerated


def _build_checkpoint_deliverable(
    run_id: int,
    phase: FactoryPhase,
    dept_row: Any,
) -> DepartmentDeliverable:
    api_fragment: dict[str, Any] = {}
    if dept_row.api_fragment_json:
        try:
            parsed = json.loads(dept_row.api_fragment_json)
            if isinstance(parsed, dict):
                api_fragment = parsed
        except ValueError:
            api_fragment = {}

    evidence: TestEvidence | None = None
    if dept_row.evidence_json:
        try:
            evidence = TestEvidence.model_validate_json(dept_row.evidence_json)
        except ValueError:
            evidence = None

    return DepartmentDeliverable(
        run_id=run_id,
        phase=phase.value,
        dept_id=dept_row.dept_id,
        status="PASSED",
        rounds_used=int(dept_row.current_round or 0),
        staging_dir=dept_row.staging_dir or "",
        api_fragment=api_fragment,
        evidence=evidence,
    )


class AsyncDepartmentPool:
    """FP05：多部门异步并发调度池（``asyncio`` + 双信号量 + 失败隔离 + 契约变更局部重跑）。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        verifier: ModuleVerifier,
        runner_factory: DepartmentRunnerFactory | None = None,
        dept_executor: DepartmentExecutor | None = None,
        contract_change_approver: ContractChangeApprover | None = None,
        ledger: PgBudgetLedger | None = None,
        state_store: FactoryStateStore | None = None,
        max_concurrent_depts_limit: int | None = None,
        max_concurrent_verifiers_limit: int | None = None,
        max_rounds: int | None = None,
        max_contract_changes: int = DEFAULT_MAX_CONTRACT_CHANGES_PER_RUN,
        escalated_round3_role: str | None = None,
        fail_fast: bool | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._verifier = verifier
        self._runner_factory = runner_factory
        self._dept_executor = dept_executor
        self._contract_change_approver = contract_change_approver
        self._ledger = ledger or PgBudgetLedger(session_factory)
        self._state_store = state_store or FactoryStateStore(session_factory)
        self._max_depts = (
            max(1, max_concurrent_depts_limit)
            if max_concurrent_depts_limit is not None
            else max_concurrent_depts()
        )
        self._max_verifiers = (
            max(1, max_concurrent_verifiers_limit)
            if max_concurrent_verifiers_limit is not None
            else max_concurrent_verifiers()
        )
        self._max_rounds = max(1, max_rounds) if max_rounds is not None else max_dept_rounds()
        self._max_contract_changes = max(0, max_contract_changes)
        self._round3_role = (
            escalated_round3_role
            if escalated_round3_role is not None
            else round3_escalation_role()
        )
        self._fail_fast = factory_fail_fast(default=False) if fail_fast is None else bool(fail_fast)

    def _compute_affected_depts(
        self,
        request: ContractChangeRequest,
        blueprint: ArchitectureBlueprint,
        decision: ContractChangeDecision,
    ) -> list[str]:
        valid_ids = {m.module_id for m in blueprint.modules}
        affected: set[str] = {request.target_dept, request.requester_dept}
        for mod in blueprint.modules:
            if request.capability_id in mod.readable_contract_refs:
                affected.add(mod.module_id)
        for explicit in decision.affected_depts:
            if explicit in valid_ids:
                affected.add(explicit)
        return sorted(affected & valid_ids)

    async def _run_single_dept(
        self,
        *,
        run_id: int,
        dept_id: str,
        module: Module,
        current_bp: ArchitectureBlueprint,
        spec: SoftwareSpecification,
        run_root: Path,
        base_root: Path | None,
        resolved_phase: FactoryPhase,
        llm_dept_semaphore: asyncio.Semaphore,
        guarded_verifier: ModuleVerifier,
        tracker: _ConcurrencyTracker,
        stop_event: asyncio.Event,
        stop_status: dict[str, str | None],
        escalations: list[ContractChangeRequest],
        escalations_lock: threading.Lock,
        tickets_map: dict[str, DefectTicket],
        deliverables: dict[str, DepartmentDeliverable],
        telemetry: dict[str, DepartmentTelemetry],
        executed_depts: list[str],
    ) -> None:
        # 在获取信号量之前先检查停止/取消信号，平滑停止不再派发新轮次
        if stop_event.is_set() or self._state_store.is_cancel_requested(run_id):
            if not stop_event.is_set():
                stop_status["status"] = "CANCELLED"
                stop_status["error_code"] = "RUN_CANCELLED"
                stop_event.set()
            return

        async with llm_dept_semaphore:
            if stop_event.is_set() or self._state_store.is_cancel_requested(run_id):
                if not stop_event.is_set():
                    stop_status["status"] = "CANCELLED"
                    stop_status["error_code"] = "RUN_CANCELLED"
                    stop_event.set()
                return

            tracker.enter_llm()
            started = time.monotonic()
            executed_depts.append(dept_id)
            initial_ticket = tickets_map.pop(dept_id, None)
            def _abort_check() -> None:
                if stop_event.is_set():
                    reason = stop_status.get("error_code") or "POOL_STOPPED"
                    if stop_status.get("status") == "PAUSED_BUDGET":
                        code = (
                            ErrorCode.CALL_LIMIT_EXCEEDED
                            if reason == ErrorCode.CALL_LIMIT_EXCEEDED.value
                            else ErrorCode.BUDGET_EXCEEDED
                        )
                        raise AppException(code, f"调度池已触发预算/配额早停：{reason}", status_code=429)
                    raise FactoryCancelledError(f"run {run_id} stopped at pool_stop:{dept_id}:{reason}")
                if self._state_store.is_cancel_requested(run_id):
                    raise FactoryCancelledError(f"run {run_id} cancelled at dept:{dept_id}:cancel_check")

            try:
                if self._dept_executor is not None:
                    maybe = self._dept_executor(
                        run_id,
                        module,
                        current_bp,
                        guarded_verifier,
                        initial_ticket,
                    )
                    result = await maybe if asyncio.iscoroutine(maybe) else maybe
                else:
                    runner = (
                        self._runner_factory(module, guarded_verifier, self._round3_role)
                        if self._runner_factory is not None
                        else DepartmentRunner(
                            session_factory=self._session_factory,
                            verifier=guarded_verifier,
                            ledger=self._ledger,
                            state_store=self._state_store,
                            round_3_coder_role=self._round3_role,
                        )
                    )
                    result = await asyncio.to_thread(
                        runner.run_department,
                        run_id,
                        module,
                        blueprint=current_bp,
                        spec=spec,
                        run_dir=run_root,
                        base_dir=base_root,
                        phase=resolved_phase,
                        max_rounds=self._max_rounds,
                        initial_ticket=initial_ticket,
                        abort_check=_abort_check,
                    )

                assert isinstance(result, DepartmentDeliverable)
                deliverables[dept_id] = result
                telemetry[dept_id] = DepartmentTelemetry(
                    dept_id=dept_id,
                    started_at_monotonic=started,
                    finished_at_monotonic=time.monotonic(),
                    status=result.status,
                    rounds_used=result.rounds_used,
                    error_code=result.error_code,
                )
                if result.status == "CANCELLED":
                    if stop_status["status"] is None:
                        stop_status["status"] = "CANCELLED"
                        stop_status["error_code"] = "RUN_CANCELLED"
                    stop_event.set()
                elif result.status == "FAILED" and self._fail_fast:
                    if stop_status["status"] is None:
                        stop_status["status"] = "FAIL_FAST"
                        stop_status["error_code"] = result.error_code or "DEPT_FAILED"
                    stop_event.set()
            except ContractChangeEscalation as esc:
                with escalations_lock:
                    escalations.append(esc.request)
                telemetry[dept_id] = DepartmentTelemetry(
                    dept_id=dept_id,
                    started_at_monotonic=started,
                    finished_at_monotonic=time.monotonic(),
                    status="REWORK",
                    error_code="CONTRACT_CHANGE_REQUESTED",
                )
            except FactoryCancelledError:
                if stop_status["status"] is None:
                    stop_status["status"] = "CANCELLED"
                    stop_status["error_code"] = "RUN_CANCELLED"
                stop_event.set()
                telemetry[dept_id] = DepartmentTelemetry(
                    dept_id=dept_id,
                    started_at_monotonic=started,
                    finished_at_monotonic=time.monotonic(),
                    status="CANCELLED",
                    error_code=stop_status["error_code"] or "RUN_CANCELLED",
                )
            except AppException as exc:
                if exc.code in {ErrorCode.BUDGET_EXCEEDED, ErrorCode.CALL_LIMIT_EXCEEDED}:
                    stop_status["status"] = "PAUSED_BUDGET"
                    stop_status["error_code"] = exc.code.value
                    stop_event.set()
                    telemetry[dept_id] = DepartmentTelemetry(
                        dept_id=dept_id,
                        started_at_monotonic=started,
                        finished_at_monotonic=time.monotonic(),
                        status="PAUSED_BUDGET",
                        error_code=exc.code.value,
                    )
                else:
                    dept_row = self._state_store.get_department(run_id, resolved_phase, dept_id)
                    if dept_row is not None and DeptStatus(dept_row.status) not in {
                        DeptStatus.PASSED,
                        DeptStatus.FAILED,
                        DeptStatus.CANCELLED,
                    }:
                        self._state_store.transition_department(
                            run_id,
                            resolved_phase,
                            dept_id,
                            DeptStatus.FAILED,
                            error_code=exc.code.value,
                            check_cancel=False,
                        )
                    deliverables[dept_id] = DepartmentDeliverable(
                        run_id=run_id,
                        phase=resolved_phase.value,
                        dept_id=dept_id,
                        status="FAILED",
                        rounds_used=int(dept_row.current_round or 0) if dept_row else 0,
                        staging_dir=str(run_root / "staging" / resolved_phase.value / dept_id),
                        error_code=exc.code.value,
                    )
                    telemetry[dept_id] = DepartmentTelemetry(
                        dept_id=dept_id,
                        started_at_monotonic=started,
                        finished_at_monotonic=time.monotonic(),
                        status="FAILED",
                        error_code=exc.code.value,
                    )
                    if self._fail_fast:
                        if stop_status["status"] is None:
                            stop_status["status"] = "FAIL_FAST"
                            stop_status["error_code"] = exc.code.value
                        stop_event.set()
            except Exception as exc:  # noqa: BLE001  # 单部门未预期异常严格隔离，绝不拖垮其他部门
                err_code = type(exc).__name__
                dept_row = self._state_store.get_department(run_id, resolved_phase, dept_id)
                if dept_row is not None and DeptStatus(dept_row.status) not in {
                    DeptStatus.PASSED,
                    DeptStatus.FAILED,
                    DeptStatus.CANCELLED,
                }:
                    self._state_store.transition_department(
                        run_id,
                        resolved_phase,
                        dept_id,
                        DeptStatus.FAILED,
                        error_code=err_code,
                        check_cancel=False,
                    )
                deliverables[dept_id] = DepartmentDeliverable(
                    run_id=run_id,
                    phase=resolved_phase.value,
                    dept_id=dept_id,
                    status="FAILED",
                    rounds_used=int(dept_row.current_round or 0) if dept_row else 0,
                    staging_dir=str(run_root / "staging" / resolved_phase.value / dept_id),
                    error_code=err_code,
                )
                telemetry[dept_id] = DepartmentTelemetry(
                    dept_id=dept_id,
                    started_at_monotonic=started,
                    finished_at_monotonic=time.monotonic(),
                    status="FAILED",
                    error_code=err_code,
                )
                if self._fail_fast:
                    if stop_status["status"] is None:
                        stop_status["status"] = "FAIL_FAST"
                        stop_status["error_code"] = err_code
                    stop_event.set()
            finally:
                _cleanup_dirty_staging(run_root, resolved_phase, dept_id)
                tracker.exit_llm()

    async def run_phase(
        self,
        *,
        run_id: int,
        blueprint: ArchitectureBlueprint,
        spec: SoftwareSpecification,
        run_dir: str | Path,
        base_dir: str | Path | None = None,
        phase: FactoryPhase | str = FactoryPhase.BACKEND,
        target_depts: Sequence[str] | None = None,
        rework_tickets: dict[str, DefectTicket] | None = None,
    ) -> PoolRunResult:
        """异步并发调度某阶段所有（或指定）部门，带双信号量限流、预算/取消平滑收敛与契约变更升级。"""
        resolved_phase = FactoryPhase(phase)
        run_root = Path(run_dir).resolve()
        base_root = Path(base_dir).resolve() if base_dir is not None else None
        current_bp = blueprint
        tickets_map = dict(rework_tickets or {})

        llm_dept_semaphore = asyncio.Semaphore(self._max_depts)
        verifier_semaphore = asyncio.Semaphore(self._max_verifiers)
        tracker = _ConcurrencyTracker()
        loop = asyncio.get_running_loop()

        guarded_verifier = _SemaphoreGuardedVerifier(
            self._verifier,
            semaphore=verifier_semaphore,
            loop=loop,
            tracker=tracker,
        )

        # 幂等注册全部部门（不覆盖已 PASSED 检查点）
        self._state_store.register_departments(
            run_id,
            resolved_phase,
            [m.module_id for m in current_bp.modules],
            max_rounds=self._max_rounds,
            staging_dirs={
                m.module_id: str(run_root / "staging" / resolved_phase.value / m.module_id)
                for m in current_bp.modules
            },
        )

        deliverables: dict[str, DepartmentDeliverable] = {}
        telemetry: dict[str, DepartmentTelemetry] = {}
        reused_depts: list[str] = []
        executed_depts: list[str] = []
        contract_changes_applied = 0

        # 首次规划要执行的部门列表：自动跳过已 PASSED 检查点
        recovery = self._state_store.scan_recovery_plan(run_id, resolved_phase, reset_inflight=True)
        for passed_id in recovery.skipped_passed:
            row = self._state_store.get_department(run_id, resolved_phase, passed_id)
            if row is not None:
                deliverables[passed_id] = _build_checkpoint_deliverable(run_id, resolved_phase, row)
                now_m = time.monotonic()
                telemetry[passed_id] = DepartmentTelemetry(
                    dept_id=passed_id,
                    started_at_monotonic=now_m,
                    finished_at_monotonic=now_m,
                    status="PASSED",
                    rounds_used=int(row.current_round or 0),
                    reused_from_checkpoint=True,
                )
                if passed_id not in reused_depts:
                    reused_depts.append(passed_id)

        if target_depts is not None:
            pending_depts = [d for d in target_depts if d in {m.module_id for m in current_bp.modules}]
        else:
            pending_depts = list(recovery.to_run)

        while True:
            if not pending_depts:
                break

            if self._state_store.is_cancel_requested(run_id):
                cancelled = self._state_store.cancel_incomplete_departments(
                    run_id, resolved_phase, reason="pool_preflight_cancel"
                )
                return PoolRunResult(
                    run_id=run_id,
                    phase=resolved_phase.value,
                    ok=False,
                    status="CANCELLED",
                    blueprint_version=current_bp.blueprint_version,
                    deliverables=deliverables,
                    telemetry=telemetry,
                    reused_depts=sorted(set(reused_depts)),
                    executed_depts=sorted(set(executed_depts)),
                    cancelled_depts=cancelled,
                    contract_changes_applied=contract_changes_applied,
                    max_observed_llm_concurrency=tracker.max_llm,
                    max_observed_verifier_concurrency=tracker.max_verifier,
                    error_code="RUN_CANCELLED",
                )

            modules_by_id = {m.module_id: m for m in current_bp.modules}
            stop_event = asyncio.Event()
            stop_status: dict[str, str | None] = {"status": None, "error_code": None}
            escalations: list[ContractChangeRequest] = []
            escalations_lock = threading.Lock()

            # 同时异步启动本波全部待跑部门（双信号量自动控流）
            await asyncio.gather(
                *(
                    self._run_single_dept(
                        run_id=run_id,
                        dept_id=dept_id,
                        module=modules_by_id[dept_id],
                        current_bp=current_bp,
                        spec=spec,
                        run_root=run_root,
                        base_root=base_root,
                        resolved_phase=resolved_phase,
                        llm_dept_semaphore=llm_dept_semaphore,
                        guarded_verifier=guarded_verifier,
                        tracker=tracker,
                        stop_event=stop_event,
                        stop_status=stop_status,
                        escalations=escalations,
                        escalations_lock=escalations_lock,
                        tickets_map=tickets_map,
                        deliverables=deliverables,
                        telemetry=telemetry,
                        executed_depts=executed_depts,
                    )
                    for dept_id in pending_depts
                )
            )

            # 1. 优先响应取消 / 预算暂停（平滑停止并保留已完成部门检查点）
            if stop_status["status"] == "CANCELLED" or self._state_store.is_cancel_requested(run_id):
                cancelled = self._state_store.cancel_incomplete_departments(
                    run_id, resolved_phase, reason="pool_cancelled"
                )
                return PoolRunResult(
                    run_id=run_id,
                    phase=resolved_phase.value,
                    ok=False,
                    status="CANCELLED",
                    blueprint_version=current_bp.blueprint_version,
                    deliverables=deliverables,
                    telemetry=telemetry,
                    reused_depts=sorted(set(reused_depts)),
                    executed_depts=sorted(set(executed_depts)),
                    cancelled_depts=cancelled,
                    contract_changes_applied=contract_changes_applied,
                    max_observed_llm_concurrency=tracker.max_llm,
                    max_observed_verifier_concurrency=tracker.max_verifier,
                    error_code="RUN_CANCELLED",
                )

            if stop_status["status"] == "PAUSED_BUDGET":
                # 将未完成的在途部门重置为 QUEUED，保留已 PASSED 检查点以便加预算续跑
                self._state_store.scan_recovery_plan(run_id, resolved_phase, reset_inflight=True)
                return PoolRunResult(
                    run_id=run_id,
                    phase=resolved_phase.value,
                    ok=False,
                    status="PAUSED_BUDGET",
                    blueprint_version=current_bp.blueprint_version,
                    deliverables=deliverables,
                    telemetry=telemetry,
                    reused_depts=sorted(set(reused_depts)),
                    executed_depts=sorted(set(executed_depts)),
                    contract_changes_applied=contract_changes_applied,
                    max_observed_llm_concurrency=tracker.max_llm,
                    max_observed_verifier_concurrency=tracker.max_verifier,
                    error_code=stop_status["error_code"] or ErrorCode.BUDGET_EXCEEDED.value,
                )

            if stop_status["status"] == "FAIL_FAST":
                # Fail-Fast 早停：将尚未完成的其余部门标为 CANCELLED，保留已失败部门与已通过部门状态
                self._state_store.cancel_incomplete_departments(
                    run_id, resolved_phase, reason="pool_fail_fast"
                )
                break

            # 2. 若无契约变更升级请求，本阶段调度结束
            if not escalations:
                break

            # 3. 契约变更升级处理（每 run <= max_contract_changes 次，仅局部重跑受影响部门）
            req = escalations[0]
            if contract_changes_applied >= self._max_contract_changes:
                self._mark_dept_failed(
                    run_id,
                    resolved_phase,
                    req.requester_dept,
                    "CONTRACT_CHANGE_LIMIT_EXCEEDED",
                    deliverables,
                    run_root,
                )
                break

            coordinator = ModuleCoordinator(current_bp.to_module_registry())
            coord_result = coordinator.submit(req.to_coordinator_request(), budget_available=True)
            if coord_result.status != STATUS_PENDING or req.kind == KIND_SCOPE:
                err = (
                    "SCOPE_CHANGE_REQUIRES_USER"
                    if req.kind == KIND_SCOPE
                    else "CONTRACT_CHANGE_BLOCKED"
                )
                self._mark_dept_failed(
                    run_id, resolved_phase, req.requester_dept, err, deliverables, run_root
                )
                break

            if self._contract_change_approver is None:
                self._mark_dept_failed(
                    run_id,
                    resolved_phase,
                    req.requester_dept,
                    "CONTRACT_APPROVER_MISSING",
                    deliverables,
                    run_root,
                )
                break

            decision = self._contract_change_approver(req, current_bp, run_id)
            if not decision.approved or decision.updated_blueprint is None:
                self._mark_dept_failed(
                    run_id,
                    resolved_phase,
                    req.requester_dept,
                    "CONTRACT_CHANGE_REJECTED",
                    deliverables,
                    run_root,
                )
                break

            updated_bp = decision.updated_blueprint
            if updated_bp.blueprint_version <= current_bp.blueprint_version:
                self._mark_dept_failed(
                    run_id,
                    resolved_phase,
                    req.requester_dept,
                    "CONTRACT_VERSION_NOT_INCREMENTED",
                    deliverables,
                    run_root,
                )
                break

            bp_issues = validate_blueprint(updated_bp, spec)
            if bp_issues:
                self._mark_dept_failed(
                    run_id,
                    resolved_phase,
                    req.requester_dept,
                    ErrorCode.BLUEPRINT_INVALID.value,
                    deliverables,
                    run_root,
                )
                break

            # 冻结新版蓝图（自动将旧版标记为 SUPERSEDED）
            self._state_store.save_blueprint(
                run_id,
                resolved_phase,
                version=updated_bp.blueprint_version,
                blueprint_json=json.dumps(
                    updated_bp.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                blueprint_hash=updated_bp.blueprint_hash(),
                status=BlueprintStatus.APPROVED_FOR_BUILD,
            )
            current_bp = updated_bp
            contract_changes_applied += 1

            affected = self._compute_affected_depts(req, current_bp, decision)
            _refresh_base_and_staging_stubs(
                blueprint=current_bp,
                run_dir=run_root,
                base_dir=base_root,
                phase=resolved_phase,
                affected_depts=affected,
            )

            # 仅把受影响部门迁回 REWORK/QUEUED；其余未受影响已 PASSED 部门原封不动复用
            self._state_store.scan_recovery_plan(run_id, resolved_phase, reset_inflight=True)
            for dept_id in affected:
                row = self._state_store.get_department(run_id, resolved_phase, dept_id)
                if row is not None and DeptStatus(row.status) is DeptStatus.PASSED:
                    self._state_store.transition_department(
                        run_id,
                        resolved_phase,
                        dept_id,
                        DeptStatus.REWORK,
                        allow_integration_rework=True,
                        reason=f"contract_change:{req.request_id}",
                    )
                    deliverables.pop(dept_id, None)

            # 记录未受影响并复用的部门
            for m in current_bp.modules:
                if m.module_id not in affected:
                    row = self._state_store.get_department(run_id, resolved_phase, m.module_id)
                    if (
                        row is not None
                        and DeptStatus(row.status) is DeptStatus.PASSED
                        and m.module_id not in reused_depts
                    ):
                        reused_depts.append(m.module_id)

            self._state_store.record_event(
                run_id,
                "factory_contract_change",
                {
                    "phase": resolved_phase.value,
                    "request_id": req.request_id,
                    "capability_id": req.capability_id,
                    "blueprint_version": current_bp.blueprint_version,
                    "affected_depts": affected,
                    "reused_depts": sorted(set(reused_depts)),
                },
            )
            pending_depts = list(affected)

        # 汇总最终状态
        all_rows = self._state_store.list_departments(run_id, resolved_phase)
        failed_depts = sorted(
            r.dept_id for r in all_rows if DeptStatus(r.status) is DeptStatus.FAILED
        )
        cancelled_depts = sorted(
            r.dept_id for r in all_rows if DeptStatus(r.status) is DeptStatus.CANCELLED
        )
        all_passed = bool(all_rows) and all(
            DeptStatus(r.status) is DeptStatus.PASSED for r in all_rows
        )
        final_status: Literal["PASSED", "FAILED", "CANCELLED", "PAUSED_BUDGET"] = (
            "PASSED" if all_passed else ("CANCELLED" if cancelled_depts and not failed_depts else "FAILED")
        )
        return PoolRunResult(
            run_id=run_id,
            phase=resolved_phase.value,
            ok=all_passed,
            status=final_status,
            blueprint_version=current_bp.blueprint_version,
            deliverables=deliverables,
            telemetry=telemetry,
            reused_depts=sorted(set(reused_depts)),
            executed_depts=sorted(set(executed_depts)),
            failed_depts=failed_depts,
            cancelled_depts=cancelled_depts,
            contract_changes_applied=contract_changes_applied,
            max_observed_llm_concurrency=tracker.max_llm,
            max_observed_verifier_concurrency=tracker.max_verifier,
            error_code=None if all_passed else (
                deliverables[failed_depts[0]].error_code
                if failed_depts and failed_depts[0] in deliverables
                else "DEPT_FAILED"
            ),
        )

    def _mark_dept_failed(
        self,
        run_id: int,
        phase: FactoryPhase,
        dept_id: str,
        error_code: str,
        deliverables: dict[str, DepartmentDeliverable],
        run_root: Path,
    ) -> None:
        row = self._state_store.get_department(run_id, phase, dept_id)
        if row is not None and DeptStatus(row.status) not in {
            DeptStatus.PASSED,
            DeptStatus.FAILED,
            DeptStatus.CANCELLED,
        }:
            self._state_store.transition_department(
                run_id,
                phase,
                dept_id,
                DeptStatus.FAILED,
                error_code=error_code,
                check_cancel=False,
            )
        deliverables[dept_id] = DepartmentDeliverable(
            run_id=run_id,
            phase=phase.value,
            dept_id=dept_id,
            status="FAILED",
            rounds_used=int(row.current_round or 0) if row else 0,
            staging_dir=str(run_root / "staging" / phase.value / dept_id),
            error_code=error_code,
        )


__all__ = [
    "CONCURRENT_DEPTS_ENV",
    "CONCURRENT_VERIFIERS_ENV",
    "DEFAULT_MAX_CONCURRENT_DEPTS",
    "DEFAULT_MAX_CONCURRENT_VERIFIERS",
    "DEFAULT_MAX_CONTRACT_CHANGES_PER_RUN",
    "DEFAULT_MAX_DEPT_ROUNDS",
    "MAX_DEPT_ROUNDS_ENV",
    "ROUND3_ESCALATION_ROLE_ENV",
    "AsyncDepartmentPool",
    "ContractChangeApprover",
    "ContractChangeDecision",
    "ContractChangeEscalation",
    "ContractChangeRequest",
    "DepartmentExecutor",
    "DepartmentRunnerFactory",
    "DepartmentTelemetry",
    "PoolRunResult",
    "max_concurrent_depts",
    "max_concurrent_verifiers",
    "max_dept_rounds",
    "round3_escalation_role",
]
