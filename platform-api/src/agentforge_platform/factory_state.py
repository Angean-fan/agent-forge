"""FP01：异步工厂流水线持久化状态机、并发安全存储与取消/恢复语义（``factory_state.py``）。

核心职责：
1. 定义阶段枚举 ``FactoryPhase``（``backend`` / ``frontend``）与部门状态 ``DeptStatus``；
2. 强制显式状态迁移白名单：``PASSED`` 仅在总装归因打回（``allow_integration_rework=True``）时
   可迁回 ``REWORK``，禁止被外部或常规流程非法覆盖；
3. ``FactoryStateStore``：
   - PostgreSQL 下以固定加锁顺序（``Run`` 行锁 → ``DepartmentRun`` 行锁）保证多部门并发状态更新
     与 ``RunEvent`` 序号递增零冲突、零死锁；SQLite 下通过连接串粒度锁 + ``BEGIN IMMEDIATE`` 串行化；
   - 与 ``Run.cancel_requested`` 共享同一取消信号，随时检测并收敛未完成部门为 ``CANCELLED``；
   - 崩溃恢复扫描（``scan_recovery_plan``）：自动跳过已 ``PASSED`` 部门，仅将中断在途部门重置后重跑。
"""

from __future__ import annotations

import json
import threading
import weakref
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform.db import is_postgres, lock_row
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.models import (
    DefectTicketRow,
    DepartmentRun,
    FactoryBlueprint,
    Run,
    RunEvent,
)
from agentforge_platform.runs import RunStatus, append_event


class FactoryPhase(StrEnum):
    BACKEND = "backend"
    FRONTEND = "frontend"


class DeptStatus(StrEnum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    CODING = "CODING"
    TESTING = "TESTING"
    PASSED = "PASSED"
    REWORK = "REWORK"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class BlueprintStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED_FOR_BUILD = "APPROVED_FOR_BUILD"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


TERMINAL_DEPT_STATUSES: frozenset[DeptStatus] = frozenset(
    {DeptStatus.FAILED, DeptStatus.CANCELLED}
)
IN_FLIGHT_DEPT_STATUSES: frozenset[DeptStatus] = frozenset(
    {DeptStatus.PLANNING, DeptStatus.CODING, DeptStatus.TESTING}
)

# 部门常规状态迁移白名单（PASSED 不在常规允许集合中，仅允许总装归因显式打回 REWORK；
# PLANNING → TESTING 允许纯测试返工轮直接跳过 DeptCoder 进入 DeptTester 修复）。
ALLOWED_DEPT_TRANSITIONS: dict[DeptStatus, frozenset[DeptStatus]] = {
    DeptStatus.QUEUED: frozenset({DeptStatus.PLANNING, DeptStatus.FAILED, DeptStatus.CANCELLED}),
    DeptStatus.PLANNING: frozenset(
        {DeptStatus.CODING, DeptStatus.TESTING, DeptStatus.FAILED, DeptStatus.CANCELLED}
    ),
    DeptStatus.CODING: frozenset({DeptStatus.TESTING, DeptStatus.FAILED, DeptStatus.CANCELLED}),
    DeptStatus.TESTING: frozenset(
        {DeptStatus.PASSED, DeptStatus.REWORK, DeptStatus.FAILED, DeptStatus.CANCELLED}
    ),
    DeptStatus.REWORK: frozenset({DeptStatus.PLANNING, DeptStatus.FAILED, DeptStatus.CANCELLED}),
    DeptStatus.PASSED: frozenset(),
    DeptStatus.FAILED: frozenset(),
    DeptStatus.CANCELLED: frozenset(),
}

_SQLITE_LOCKS_GUARD = threading.Lock()
_SQLITE_URL_LOCKS: dict[str, threading.Lock] = {}
_SQLITE_ENGINE_LOCKS: weakref.WeakKeyDictionary[Any, threading.Lock] = weakref.WeakKeyDictionary()


class FactoryCancelledError(Exception):
    """工厂流水线观察到取消信号时抛出的受控异常，与 ``RUN_CANCELLED`` 错误码对齐。"""

    def __init__(self, message: str = "run cancelled") -> None:
        super().__init__(message)
        self.code = "RUN_CANCELLED"


@dataclass(frozen=True)
class RecoveryPlan:
    """崩溃重启后的部门恢复计划：已 ``PASSED`` 部门严格跳过，只重跑未完成部门。"""

    run_id: int
    phase: FactoryPhase
    skipped_passed: tuple[str, ...]
    to_run: tuple[str, ...]
    recovered_from_inflight: tuple[str, ...]
    failed: tuple[str, ...]
    cancelled: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "phase": self.phase.value,
            "skipped_passed": list(self.skipped_passed),
            "to_run": list(self.to_run),
            "recovered_from_inflight": list(self.recovered_from_inflight),
            "failed": list(self.failed),
            "cancelled": list(self.cancelled),
        }


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _coerce_phase(phase: FactoryPhase | str) -> FactoryPhase:
    try:
        return FactoryPhase(phase)
    except ValueError as exc:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"非法工厂阶段：{phase!r}，仅允许 backend / frontend",
            status_code=422,
        ) from exc


def _coerce_dept_status(status: DeptStatus | str) -> DeptStatus:
    try:
        return DeptStatus(status)
    except ValueError as exc:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"非法部门状态：{status!r}",
            status_code=422,
        ) from exc


def _coerce_blueprint_status(status: BlueprintStatus | str) -> BlueprintStatus:
    try:
        return BlueprintStatus(status)
    except ValueError as exc:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"非法蓝图状态：{status!r}",
            status_code=422,
        ) from exc


def _sqlite_lock_for(db: Session) -> threading.Lock:
    bind = db.get_bind()
    engine = getattr(bind, "engine", bind)
    url_str = str(getattr(engine, "url", ""))
    with _SQLITE_LOCKS_GUARD:
        if ":memory:" in url_str or not url_str:
            lock = _SQLITE_ENGINE_LOCKS.get(engine)
            if lock is None:
                lock = threading.Lock()
                _SQLITE_ENGINE_LOCKS[engine] = lock
            return lock
        lock = _SQLITE_URL_LOCKS.get(url_str)
        if lock is None:
            lock = threading.Lock()
            _SQLITE_URL_LOCKS[url_str] = lock
        return lock


def _validate_dept_transition(
    current: DeptStatus,
    target: DeptStatus,
    *,
    allow_integration_rework: bool = False,
) -> None:
    if current is DeptStatus.PASSED:
        if target is DeptStatus.REWORK and allow_integration_rework:
            return
        raise AppException(
            ErrorCode.RUN_INVALID_TRANSITION,
            f"部门状态 {current.value} 不允许迁移至 {target.value}（仅总装归因打回可迁回 REWORK）",
            status_code=409,
        )
    if target not in ALLOWED_DEPT_TRANSITIONS[current]:
        raise AppException(
            ErrorCode.RUN_INVALID_TRANSITION,
            f"非法部门状态迁移：{current.value} → {target.value}",
            status_code=409,
        )


def _dump_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class FactoryStateStore:
    """工厂流水线持久化状态真值存储（兼容本地 PostgreSQL 行锁与 SQLite 单元测试）。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @contextmanager
    def _write_tx(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            if is_postgres(session):
                try:
                    yield session
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
            else:
                with _sqlite_lock_for(session):
                    try:
                        if not session.in_transaction():
                            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                        yield session
                        session.commit()
                    except Exception:
                        session.rollback()
                        raise
        finally:
            session.close()

    @contextmanager
    def _read_session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    def _require_locked_run(self, db: Session, run_id: int) -> Run:
        run = lock_row(db, Run, run_id)
        if run is None:
            raise AppException(
                ErrorCode.RUN_NOT_FOUND,
                f"run {run_id} 不存在",
                status_code=404,
            )
        return run

    # ------------------------------------------------------------------
    # 取消检查与事件记录
    # ------------------------------------------------------------------

    def is_cancel_requested(self, run_id: int) -> bool:
        """检查共享的 ``Run.cancel_requested`` 或终态 ``CANCELLED``。"""
        with self._read_session() as db:
            run = db.get(Run, run_id)
            if run is None:
                raise AppException(
                    ErrorCode.RUN_NOT_FOUND,
                    f"run {run_id} 不存在",
                    status_code=404,
                )
            return bool(run.cancel_requested or RunStatus(run.status) is RunStatus.CANCELLED)

    def raise_if_cancelled(self, run_id: int, *, boundary: str = "") -> None:
        """在部门小循环或总装大循环边界检查取消信号，已取消则立即抛出受控异常。"""
        if self.is_cancel_requested(run_id):
            suffix = f"（边界：{boundary}）" if boundary else ""
            raise FactoryCancelledError(f"run {run_id} 已请求取消{suffix}")

    def record_event(
        self,
        run_id: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> RunEvent:
        """原子记录单调递增的运行事件（在 PG 下按 Run 行锁串行化 sequence，防并发冲突）。"""
        if len(event_type) > 32:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"事件类型超长（上限 32 字符）：{event_type!r}",
                status_code=422,
            )
        with self._write_tx() as db:
            run = self._require_locked_run(db, run_id)
            event = append_event(db, run, event_type, _dump_json(payload))
            db.refresh(event)
            return event

    # ------------------------------------------------------------------
    # 蓝图持久化
    # ------------------------------------------------------------------

    def save_blueprint(
        self,
        run_id: int,
        phase: FactoryPhase | str,
        *,
        version: int,
        blueprint_json: str,
        blueprint_hash: str,
        status: BlueprintStatus | str = BlueprintStatus.DRAFT,
        record_event: bool = True,
    ) -> FactoryBlueprint:
        """保存阶段架构蓝图（按 ``(run_id, phase, version)`` 幂等且防内容篡改）。"""
        resolved_phase = _coerce_phase(phase)
        resolved_status = _coerce_blueprint_status(status)
        if version < 1:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"蓝图版本号必须 >= 1：{version}",
                status_code=422,
            )

        with self._write_tx() as db:
            run = self._require_locked_run(db, run_id)
            existing = db.scalars(
                select(FactoryBlueprint).where(
                    FactoryBlueprint.run_id == run_id,
                    FactoryBlueprint.phase == resolved_phase.value,
                    FactoryBlueprint.version == version,
                )
            ).first()
            now = _now()

            if existing is not None:
                if existing.blueprint_hash != blueprint_hash:
                    raise AppException(
                        ErrorCode.CONTRACT_CONFLICT,
                        f"蓝图版本 {resolved_phase.value}@v{version} 已存在且内容哈希冲突，禁止覆盖",
                        status_code=409,
                    )
                if existing.status == resolved_status.value:
                    return existing
                existing.status = resolved_status.value
                existing.updated_at = now
                row = existing
            else:
                row = FactoryBlueprint(
                    run_id=run_id,
                    phase=resolved_phase.value,
                    version=version,
                    blueprint_json=blueprint_json,
                    blueprint_hash=blueprint_hash,
                    status=resolved_status.value,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.flush()

            if resolved_status is BlueprintStatus.APPROVED_FOR_BUILD:
                previous_approved = db.scalars(
                    select(FactoryBlueprint).where(
                        FactoryBlueprint.run_id == run_id,
                        FactoryBlueprint.phase == resolved_phase.value,
                        FactoryBlueprint.version != version,
                        FactoryBlueprint.status == BlueprintStatus.APPROVED_FOR_BUILD.value,
                    )
                ).all()
                for prev in previous_approved:
                    prev.status = BlueprintStatus.SUPERSEDED.value
                    prev.updated_at = now

            if record_event:
                append_event(
                    db,
                    run,
                    "factory_blueprint",
                    _dump_json(
                        {
                            "phase": resolved_phase.value,
                            "version": version,
                            "status": resolved_status.value,
                            "blueprint_hash": blueprint_hash,
                        }
                    ),
                )
            db.flush()
            db.refresh(row)
            return row

    def get_latest_blueprint(
        self,
        run_id: int,
        phase: FactoryPhase | str,
        *,
        approved_only: bool = False,
    ) -> FactoryBlueprint | None:
        resolved_phase = _coerce_phase(phase)
        with self._read_session() as db:
            stmt = select(FactoryBlueprint).where(
                FactoryBlueprint.run_id == run_id,
                FactoryBlueprint.phase == resolved_phase.value,
            )
            if approved_only:
                stmt = stmt.where(
                    FactoryBlueprint.status == BlueprintStatus.APPROVED_FOR_BUILD.value
                )
            stmt = stmt.order_by(FactoryBlueprint.version.desc()).limit(1)
            return db.scalars(stmt).first()

    def list_blueprints(
        self,
        run_id: int,
        phase: FactoryPhase | str | None = None,
    ) -> list[FactoryBlueprint]:
        with self._read_session() as db:
            stmt = select(FactoryBlueprint).where(FactoryBlueprint.run_id == run_id)
            if phase is not None:
                stmt = stmt.where(FactoryBlueprint.phase == _coerce_phase(phase).value)
            stmt = stmt.order_by(FactoryBlueprint.phase, FactoryBlueprint.version)
            return list(db.scalars(stmt).all())

    # ------------------------------------------------------------------
    # 部门状态机与并发安全更新
    # ------------------------------------------------------------------

    def register_department(
        self,
        run_id: int,
        phase: FactoryPhase | str,
        dept_id: str,
        *,
        max_rounds: int = 3,
        staging_dir: str | None = None,
    ) -> DepartmentRun:
        """幂等登记部门运行行：若已存在则原样返回（绝不覆盖已 ``PASSED`` 状态）。"""
        resolved_phase = _coerce_phase(phase)
        clean_dept = dept_id.strip()
        if not clean_dept:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                "dept_id 不能为空",
                status_code=422,
            )
        if max_rounds < 1:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"max_rounds 必须 >= 1：{max_rounds}",
                status_code=422,
            )

        with self._write_tx() as db:
            self._require_locked_run(db, run_id)
            existing = db.scalars(
                select(DepartmentRun).where(
                    DepartmentRun.run_id == run_id,
                    DepartmentRun.phase == resolved_phase.value,
                    DepartmentRun.dept_id == clean_dept,
                )
            ).first()
            if existing is not None:
                return existing
            now = _now()
            row = DepartmentRun(
                run_id=run_id,
                phase=resolved_phase.value,
                dept_id=clean_dept,
                status=DeptStatus.QUEUED.value,
                current_round=0,
                max_rounds=max_rounds,
                staging_dir=staging_dir,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            db.flush()
            db.refresh(row)
            return row

    def register_departments(
        self,
        run_id: int,
        phase: FactoryPhase | str,
        dept_ids: Sequence[str],
        *,
        max_rounds: int = 3,
        staging_dirs: dict[str, str] | None = None,
    ) -> list[DepartmentRun]:
        """批量幂等登记某阶段的全部部门。"""
        resolved_phase = _coerce_phase(phase)
        if max_rounds < 1:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"max_rounds 必须 >= 1：{max_rounds}",
                status_code=422,
            )
        staging_map = staging_dirs or {}
        with self._write_tx() as db:
            self._require_locked_run(db, run_id)
            rows: list[DepartmentRun] = []
            now = _now()
            for raw_dept in dept_ids:
                clean_dept = raw_dept.strip()
                if not clean_dept:
                    raise AppException(
                        ErrorCode.VALIDATION_ERROR,
                        "dept_id 不能为空",
                        status_code=422,
                    )
                existing = db.scalars(
                    select(DepartmentRun).where(
                        DepartmentRun.run_id == run_id,
                        DepartmentRun.phase == resolved_phase.value,
                        DepartmentRun.dept_id == clean_dept,
                    )
                ).first()
                if existing is not None:
                    rows.append(existing)
                    continue
                row = DepartmentRun(
                    run_id=run_id,
                    phase=resolved_phase.value,
                    dept_id=clean_dept,
                    status=DeptStatus.QUEUED.value,
                    current_round=0,
                    max_rounds=max_rounds,
                    staging_dir=staging_map.get(clean_dept),
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.flush()
                db.refresh(row)
                rows.append(row)
            return rows

    def get_department(
        self,
        run_id: int,
        phase: FactoryPhase | str,
        dept_id: str,
    ) -> DepartmentRun | None:
        resolved_phase = _coerce_phase(phase)
        with self._read_session() as db:
            return db.scalars(
                select(DepartmentRun).where(
                    DepartmentRun.run_id == run_id,
                    DepartmentRun.phase == resolved_phase.value,
                    DepartmentRun.dept_id == dept_id,
                )
            ).first()

    def list_departments(
        self,
        run_id: int,
        phase: FactoryPhase | str | None = None,
    ) -> list[DepartmentRun]:
        with self._read_session() as db:
            stmt = select(DepartmentRun).where(DepartmentRun.run_id == run_id)
            if phase is not None:
                stmt = stmt.where(DepartmentRun.phase == _coerce_phase(phase).value)
            stmt = stmt.order_by(DepartmentRun.phase, DepartmentRun.dept_id)
            return list(db.scalars(stmt).all())

    def passed_departments(
        self,
        run_id: int,
        phase: FactoryPhase | str,
    ) -> list[DepartmentRun]:
        resolved_phase = _coerce_phase(phase)
        with self._read_session() as db:
            return list(
                db.scalars(
                    select(DepartmentRun)
                    .where(
                        DepartmentRun.run_id == run_id,
                        DepartmentRun.phase == resolved_phase.value,
                        DepartmentRun.status == DeptStatus.PASSED.value,
                    )
                    .order_by(DepartmentRun.dept_id)
                ).all()
            )

    def transition_department(
        self,
        run_id: int,
        phase: FactoryPhase | str,
        dept_id: str,
        new_status: DeptStatus | str,
        *,
        current_round: int | None = None,
        staging_dir: str | None = None,
        api_fragment_json: str | None = None,
        evidence_json: str | None = None,
        error_code: str | None = None,
        allow_integration_rework: bool = False,
        reason: str | None = None,
        check_cancel: bool = True,
    ) -> DepartmentRun:
        """原子校验并迁移部门状态，同时追加 ``RunEvent``（支持多部门并发安全调用）。"""
        resolved_phase = _coerce_phase(phase)
        target = _coerce_dept_status(new_status)

        with self._write_tx() as db:
            run = self._require_locked_run(db, run_id)
            if (
                check_cancel
                and target not in {DeptStatus.CANCELLED, DeptStatus.FAILED}
                and (run.cancel_requested or RunStatus(run.status) is RunStatus.CANCELLED)
            ):
                raise FactoryCancelledError(
                    f"run {run_id} 已请求取消，拒绝将部门 {dept_id} 推进至 {target.value}"
                )

            existing = db.scalars(
                select(DepartmentRun).where(
                    DepartmentRun.run_id == run_id,
                    DepartmentRun.phase == resolved_phase.value,
                    DepartmentRun.dept_id == dept_id,
                )
            ).first()
            if existing is None:
                raise AppException(
                    ErrorCode.NOT_FOUND,
                    f"部门运行记录不存在：run={run_id}, phase={resolved_phase.value}, dept={dept_id}",
                    status_code=404,
                )
            row = lock_row(db, DepartmentRun, existing.id)
            assert row is not None

            current = DeptStatus(row.status)
            _validate_dept_transition(
                current,
                target,
                allow_integration_rework=allow_integration_rework,
            )

            now = _now()
            row.status = target.value
            if current_round is not None:
                row.current_round = current_round
            if staging_dir is not None:
                row.staging_dir = staging_dir
            if api_fragment_json is not None:
                row.api_fragment_json = api_fragment_json
            if evidence_json is not None:
                row.evidence_json = evidence_json

            if target is DeptStatus.PLANNING and row.started_at is None:
                row.started_at = now
            if target in {DeptStatus.PASSED, DeptStatus.FAILED, DeptStatus.CANCELLED}:
                row.finished_at = now
            elif target is DeptStatus.REWORK:
                row.finished_at = None

            if target is DeptStatus.FAILED:
                row.error_code = error_code or row.error_code
            elif target is DeptStatus.PASSED:
                row.error_code = None
            elif error_code is not None:
                row.error_code = error_code

            row.updated_at = now

            event_payload: dict[str, Any] = {
                "phase": resolved_phase.value,
                "dept_id": dept_id,
                "from": current.value,
                "to": target.value,
                "round": row.current_round,
            }
            if allow_integration_rework and current is DeptStatus.PASSED:
                event_payload["integration_rework"] = True
            if error_code:
                event_payload["error_code"] = error_code
            if reason:
                event_payload["reason"] = reason

            append_event(db, run, "factory_dept_status", _dump_json(event_payload))
            db.flush()
            db.refresh(row)
            return row

    def cancel_incomplete_departments(
        self,
        run_id: int,
        phase: FactoryPhase | str | None = None,
        *,
        reason: str = "cancel_requested",
    ) -> list[str]:
        """将所有未完成（非 ``PASSED``/``FAILED``/``CANCELLED``）部门收敛为 ``CANCELLED``。"""
        phase_filter = _coerce_phase(phase).value if phase is not None else None
        with self._write_tx() as db:
            run = self._require_locked_run(db, run_id)
            stmt = select(DepartmentRun).where(DepartmentRun.run_id == run_id)
            if phase_filter is not None:
                stmt = stmt.where(DepartmentRun.phase == phase_filter)
            stmt = stmt.order_by(DepartmentRun.phase, DepartmentRun.dept_id)
            rows = list(db.scalars(stmt).all())

            cancelled_depts: list[str] = []
            now = _now()
            for item in rows:
                locked = lock_row(db, DepartmentRun, item.id)
                assert locked is not None
                st = DeptStatus(locked.status)
                if st in {DeptStatus.PASSED, DeptStatus.FAILED, DeptStatus.CANCELLED}:
                    continue
                locked.status = DeptStatus.CANCELLED.value
                locked.finished_at = now
                locked.updated_at = now
                cancelled_depts.append(locked.dept_id)

            if cancelled_depts:
                append_event(
                    db,
                    run,
                    "factory_cancelled",
                    _dump_json(
                        {
                            "phase": phase_filter or "all",
                            "cancelled_depts": cancelled_depts,
                            "reason": reason,
                        }
                    ),
                )
            db.flush()
            return cancelled_depts

    def scan_recovery_plan(
        self,
        run_id: int,
        phase: FactoryPhase | str,
        *,
        reset_inflight: bool = True,
    ) -> RecoveryPlan:
        """崩溃恢复扫描：跳过已 ``PASSED`` 部门，按需将中断在途部门重置为 ``QUEUED`` 以便续跑。"""
        resolved_phase = _coerce_phase(phase)
        with self._write_tx() as db:
            run = self._require_locked_run(db, run_id)
            rows = list(
                db.scalars(
                    select(DepartmentRun)
                    .where(
                        DepartmentRun.run_id == run_id,
                        DepartmentRun.phase == resolved_phase.value,
                    )
                    .order_by(DepartmentRun.dept_id)
                ).all()
            )
            skipped_passed: list[str] = []
            to_run: list[str] = []
            recovered_from_inflight: list[str] = []
            failed: list[str] = []
            cancelled: list[str] = []
            now = _now()

            for item in rows:
                locked = lock_row(db, DepartmentRun, item.id)
                assert locked is not None
                st = DeptStatus(locked.status)
                if st is DeptStatus.PASSED:
                    skipped_passed.append(locked.dept_id)
                elif st in IN_FLIGHT_DEPT_STATUSES:
                    recovered_from_inflight.append(locked.dept_id)
                    to_run.append(locked.dept_id)
                    if reset_inflight:
                        locked.status = DeptStatus.QUEUED.value
                        locked.finished_at = None
                        locked.updated_at = now
                        append_event(
                            db,
                            run,
                            "factory_dept_recovered",
                            _dump_json(
                                {
                                    "phase": resolved_phase.value,
                                    "dept_id": locked.dept_id,
                                    "from": st.value,
                                    "to": DeptStatus.QUEUED.value,
                                    "round": locked.current_round,
                                }
                            ),
                        )
                elif st in {DeptStatus.QUEUED, DeptStatus.REWORK}:
                    to_run.append(locked.dept_id)
                elif st is DeptStatus.FAILED:
                    failed.append(locked.dept_id)
                elif st is DeptStatus.CANCELLED:
                    cancelled.append(locked.dept_id)

            db.flush()
            return RecoveryPlan(
                run_id=run_id,
                phase=resolved_phase,
                skipped_passed=tuple(skipped_passed),
                to_run=tuple(to_run),
                recovered_from_inflight=tuple(recovered_from_inflight),
                failed=tuple(failed),
                cancelled=tuple(cancelled),
            )

    # ------------------------------------------------------------------
    # 缺陷单持久化
    # ------------------------------------------------------------------

    def open_defect_ticket(
        self,
        run_id: int,
        phase: FactoryPhase | str,
        dept_id: str,
        *,
        scope: str,
        round_num: int,
        error_class: str,
        payload: dict[str, Any],
    ) -> DefectTicketRow:
        resolved_phase = _coerce_phase(phase)
        with self._write_tx() as db:
            run = self._require_locked_run(db, run_id)
            row = DefectTicketRow(
                run_id=run_id,
                phase=resolved_phase.value,
                dept_id=dept_id,
                scope=scope,
                round=round_num,
                error_class=error_class,
                payload_json=_dump_json(payload),
                resolved=False,
                created_at=_now(),
            )
            db.add(row)
            db.flush()
            append_event(
                db,
                run,
                "factory_defect_opened",
                _dump_json(
                    {
                        "ticket_id": row.id,
                        "phase": resolved_phase.value,
                        "dept_id": dept_id,
                        "scope": scope,
                        "round": round_num,
                        "error_class": error_class,
                    }
                ),
            )
            db.refresh(row)
            return row

    def resolve_defect_ticket(self, ticket_id: int) -> DefectTicketRow:
        with self._write_tx() as db:
            ticket = db.get(DefectTicketRow, ticket_id)
            if ticket is None:
                raise AppException(
                    ErrorCode.NOT_FOUND,
                    f"缺陷单 {ticket_id} 不存在",
                    status_code=404,
                )
            run = self._require_locked_run(db, ticket.run_id)
            locked = lock_row(db, DefectTicketRow, ticket_id)
            assert locked is not None
            if not locked.resolved:
                locked.resolved = True
                locked.resolved_at = _now()
                append_event(
                    db,
                    run,
                    "factory_defect_resolved",
                    _dump_json(
                        {
                            "ticket_id": locked.id,
                            "phase": locked.phase,
                            "dept_id": locked.dept_id,
                        }
                    ),
                )
                db.flush()
            db.refresh(locked)
            return locked

    def resolve_department_tickets(
        self,
        run_id: int,
        phase: FactoryPhase | str,
        dept_id: str,
        *,
        scope: str | None = None,
    ) -> int:
        resolved_phase = _coerce_phase(phase)
        with self._write_tx() as db:
            run = self._require_locked_run(db, run_id)
            stmt = select(DefectTicketRow).where(
                DefectTicketRow.run_id == run_id,
                DefectTicketRow.phase == resolved_phase.value,
                DefectTicketRow.dept_id == dept_id,
                DefectTicketRow.resolved.is_(False),
            )
            if scope is not None:
                stmt = stmt.where(DefectTicketRow.scope == scope)
            tickets = list(db.scalars(stmt).all())
            now = _now()
            resolved_ids: list[int] = []
            for item in tickets:
                locked = lock_row(db, DefectTicketRow, item.id)
                assert locked is not None
                if not locked.resolved:
                    locked.resolved = True
                    locked.resolved_at = now
                    resolved_ids.append(locked.id)
            if resolved_ids:
                append_event(
                    db,
                    run,
                    "factory_defect_resolved",
                    _dump_json(
                        {
                            "phase": resolved_phase.value,
                            "dept_id": dept_id,
                            "ticket_ids": resolved_ids,
                        }
                    ),
                )
                db.flush()
            return len(resolved_ids)

    def list_defect_tickets(
        self,
        run_id: int,
        *,
        phase: FactoryPhase | str | None = None,
        dept_id: str | None = None,
        resolved: bool | None = None,
    ) -> list[DefectTicketRow]:
        with self._read_session() as db:
            stmt = select(DefectTicketRow).where(DefectTicketRow.run_id == run_id)
            if phase is not None:
                stmt = stmt.where(DefectTicketRow.phase == _coerce_phase(phase).value)
            if dept_id is not None:
                stmt = stmt.where(DefectTicketRow.dept_id == dept_id)
            if resolved is not None:
                stmt = stmt.where(DefectTicketRow.resolved.is_(resolved))
            stmt = stmt.order_by(DefectTicketRow.id)
            return list(db.scalars(stmt).all())


__all__ = [
    "ALLOWED_DEPT_TRANSITIONS",
    "IN_FLIGHT_DEPT_STATUSES",
    "TERMINAL_DEPT_STATUSES",
    "BlueprintStatus",
    "DeptStatus",
    "FactoryCancelledError",
    "FactoryPhase",
    "FactoryStateStore",
    "RecoveryPlan",
]
