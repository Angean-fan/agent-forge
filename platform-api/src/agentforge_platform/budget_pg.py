"""A07：基于 PostgreSQL 行锁的并发安全预算池与部门维度事务账本（``budget_pg.py``）。

核心能力：
1. ``PgBudgetLedger``：
   - ``reserve_atomic()``：PostgreSQL 下通过 ``SELECT ... FOR UPDATE`` 锁定项目行（SQLite 下通过
     短事务写锁串行化），原子判定 ``settled + reserved + new_reserve <= effective_budget`` 并写入
     ``RESERVED`` 记录，保证高并发场景零超额、零双扣且按 ``call_id`` 幂等；
   - ``settle_idempotent()``：按 ``call_id`` 行锁幂等结算；重复结算绝不双扣；``usage`` 缺失或
     缺核定单价/汇率时严格记为 ``UNKNOWN`` 并保留预留额度，严禁记零；
   - ``release_atomic()``：仅释放尚未发出的 ``RESERVED`` 预留（置 ``RELEASED`` 且 ``reserved_micro=0``），
     绝不抹除已 ``SETTLED`` 账目或 ``UNKNOWN`` 保留预留；
   - ``cost_by_department()``：按阶段（``phase``）与部门（``dept_id``）汇总调用次数、Token、已结算
     费用与在途预留。
2. 顶级函数 ``cost_by_department(db, run_id)`` 供报表打包与 API 直接调用。
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform.budget import effective_budget_micro
from agentforge_platform.clients import Usage
from agentforge_platform.db import is_postgres, lock_row
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.model_gateway import (
    CNY,
    MICRO_PER_YUAN,
    UNKNOWN,
    Price,
    _ceil_div,
    _parse_dt,
    _reserve_micro,
    _totals,
    fx_rate_micro,
    lookup_price,
    resolve_route,
)
from agentforge_platform.models import ModelCall, Project

STATUS_RESERVED = "RESERVED"
STATUS_SETTLED = "SETTLED"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_RELEASED = "RELEASED"
STATUS_NOT_SENT = "NOT_SENT"

UNASSIGNED_PHASE = "unassigned"
UNASSIGNED_DEPT = "unassigned"

_SQLITE_LOCKS_GUARD = threading.Lock()
_SQLITE_URL_LOCKS: dict[str, threading.Lock] = {}
_SQLITE_ENGINE_LOCKS: weakref.WeakKeyDictionary[Any, threading.Lock] = weakref.WeakKeyDictionary()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


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


def _lock_project_for_budget(db: Session, project_id: int) -> None:
    """在 PostgreSQL 上获取行锁（``SELECT ... FOR UPDATE``）；缺项目行时退化为事务级建议锁。"""
    if is_postgres(db):
        project = lock_row(db, Project, project_id)
        if project is None:
            db.execute(text("SELECT pg_advisory_xact_lock(:pid)"), {"pid": int(project_id)})


@dataclass(frozen=True)
class BudgetSnapshot:
    project_id: int
    effective_budget_micro: int
    settled_micro: int
    reserved_micro: int
    available_micro: int


def _empty_bucket() -> dict[str, Any]:
    return {
        "calls": 0,
        "settled_calls": 0,
        "reserved_calls": 0,
        "unknown_calls": 0,
        "released_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_micro": 0,
        "reserved_micro": 0,
    }


def _accumulate_row(bucket: dict[str, Any], row: ModelCall) -> None:
    if row.status in {STATUS_RELEASED, STATUS_NOT_SENT}:
        bucket["released_calls"] += 1
        return
    bucket["calls"] += 1
    bucket["input_tokens"] += int(row.input_tokens or 0)
    bucket["output_tokens"] += int(row.output_tokens or 0)
    if row.status == STATUS_SETTLED:
        bucket["settled_calls"] += 1
        bucket["cost_micro"] += int(row.cost_micro or 0)
    elif row.status == STATUS_RESERVED:
        bucket["reserved_calls"] += 1
        bucket["reserved_micro"] += int(row.reserved_micro or 0)
    else:
        # UNKNOWN 及其他未决态保留预留额度，绝不记零
        bucket["unknown_calls"] += 1
        bucket["reserved_micro"] += int(row.reserved_micro or 0)


def cost_by_department(db: Session, run_id: int) -> dict[str, Any]:
    """A07：输出指定 ``run_id`` 按阶段（``phase``）与部门（``dept_id``）拆分的调用次数、Token 与费用汇总。"""
    rows = db.scalars(
        select(ModelCall)
        .where(ModelCall.run_id == run_id)
        .order_by(ModelCall.created_at, ModelCall.call_id)
    ).all()

    totals = _empty_bucket()
    by_phase: dict[str, dict[str, Any]] = {}
    departments: dict[str, dict[str, Any]] = {}

    for row in rows:
        phase_key = (row.phase or "").strip() or UNASSIGNED_PHASE
        dept_key = (row.dept_id or "").strip() or UNASSIGNED_DEPT

        _accumulate_row(totals, row)

        phase_entry = by_phase.setdefault(
            phase_key,
            {**_empty_bucket(), "departments": {}},
        )
        _accumulate_row(phase_entry, row)

        phase_dept = phase_entry["departments"].setdefault(
            dept_key,
            {**_empty_bucket(), "by_round": {}, "by_model": {}},
        )
        _accumulate_row(phase_dept, row)

        if row.round is not None:
            round_key = str(row.round)
            round_bucket = phase_dept["by_round"].setdefault(round_key, _empty_bucket())
            _accumulate_row(round_bucket, row)

        model_key = f"{row.provider}/{row.model}"
        model_bucket = phase_dept["by_model"].setdefault(model_key, _empty_bucket())
        _accumulate_row(model_bucket, row)

        flat_dept = departments.setdefault(
            dept_key,
            {**_empty_bucket(), "phases": []},
        )
        _accumulate_row(flat_dept, row)
        if phase_key not in flat_dept["phases"]:
            flat_dept["phases"].append(phase_key)

    return {
        "run_id": run_id,
        "currency_unit": "micro_yuan (1 元 = 1_000_000 微)",
        "totals": totals,
        "by_phase": by_phase,
        "departments": departments,
    }


class PgBudgetLedger:
    """并发安全的事务预算账本（PG 行锁 ``SELECT ... FOR UPDATE`` + 幂等结算 + 部门归集）。"""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._session_factory = session_factory

    @contextmanager
    def _write_tx(self, db: Session | None) -> Iterator[Session]:
        if db is not None:
            if is_postgres(db):
                yield db
            else:
                with _sqlite_lock_for(db):
                    yield db
            return

        if self._session_factory is None:
            raise AppException(
                ErrorCode.CONFIG_REQUIRED,
                "PgBudgetLedger 需要提供 session_factory 或显式传入 db Session",
                status_code=500,
            )

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
    def _read_session(self, db: Session | None) -> Iterator[Session]:
        if db is not None:
            yield db
            return
        if self._session_factory is None:
            raise AppException(
                ErrorCode.CONFIG_REQUIRED,
                "PgBudgetLedger 需要提供 session_factory 或显式传入 db Session",
                status_code=500,
            )
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    def reserve_atomic(
        self,
        *,
        call_id: str,
        project_id: int,
        model_role: str = "dept_coder",
        provider: str | None = None,
        model: str | None = None,
        reserve_micro: int | None = None,
        max_input_tokens: int = 0,
        max_output_tokens: int = 0,
        run_id: int | None = None,
        task_id: str | None = None,
        draft_id: int | None = None,
        dept_id: str | None = None,
        phase: str | None = None,
        round_num: int | None = None,
        price: Price | None = None,
        thinking_mode: str | None = None,
        reasoning_effort: str | None = None,
        budget_limit_micro: int | None = None,
        db: Session | None = None,
    ) -> ModelCall:
        """原子检查余额并写入 ``RESERVED`` 预留（按 ``call_id`` 幂等，PG 行锁防超扣/双扣）。"""
        if reserve_micro is not None and reserve_micro < 0:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"reserve_micro 不能为负数：{reserve_micro}",
                status_code=422,
            )

        resolved_provider = provider
        resolved_model = model
        resolved_price = price
        if not resolved_provider or not resolved_model:
            route = resolve_route(model_role)
            resolved_provider = resolved_provider or route.provider
            resolved_model = resolved_model or route.model
            resolved_price = resolved_price or route.price or lookup_price(route.model)
        elif resolved_price is None:
            resolved_price = lookup_price(resolved_model)

        amount = (
            int(reserve_micro)
            if reserve_micro is not None
            else _reserve_micro(resolved_price, max_input_tokens, max_output_tokens)
        )

        with self._write_tx(db) as session:
            _lock_project_for_budget(session, project_id)

            existing = session.get(ModelCall, call_id)
            if existing is not None:
                return existing

            limit = (
                int(budget_limit_micro)
                if budget_limit_micro is not None
                else effective_budget_micro(session, project_id)
            )
            settled, reserved = _totals(session, project_id)
            if settled + reserved + amount > limit:
                raise AppException(
                    ErrorCode.BUDGET_EXCEEDED,
                    f"预算不足，未调用模型（已结算 {settled} + 预留 {reserved} + 本次 {amount} > 上限 {limit}）。",
                    status_code=402,
                )

            row = ModelCall(
                call_id=call_id,
                model_role=model_role,
                provider=resolved_provider,
                model=resolved_model,
                project_id=project_id,
                draft_id=draft_id,
                run_id=run_id,
                task_id=task_id,
                dept_id=dept_id,
                phase=phase,
                round=round_num,
                thinking_mode=thinking_mode,
                reasoning_effort=reasoning_effort,
                price_version=resolved_price.version if resolved_price else UNKNOWN,
                price_currency=resolved_price.currency if resolved_price else None,
                input_micro_per_million=(
                    resolved_price.input_micro_per_million if resolved_price else None
                ),
                output_micro_per_million=(
                    resolved_price.output_micro_per_million if resolved_price else None
                ),
                reserved_micro=amount,
                status=STATUS_RESERVED,
            )
            session.add(row)
            session.flush()
            return row

    def settle_idempotent(
        self,
        call_id: str,
        *,
        usage: Usage | None = None,
        actual_cost_micro: int | None = None,
        request_id: str | None = None,
        error_class: str | None = None,
        db: Session | None = None,
    ) -> ModelCall:
        """按 ``call_id`` 行锁幂等结算；重复结算不双扣；缺 ``usage`` 或缺单价/汇率记 ``UNKNOWN`` 并保留预留。"""
        if actual_cost_micro is not None and actual_cost_micro < 0:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"actual_cost_micro 不能为负数：{actual_cost_micro}",
                status_code=422,
            )

        with self._write_tx(db) as session:
            row = lock_row(session, ModelCall, call_id)
            if row is None:
                raise AppException(
                    ErrorCode.RUN_NOT_FOUND, f"未知 call_id：{call_id}", status_code=404
                )
            if row.status in {STATUS_SETTLED, STATUS_RELEASED}:
                return row

            if request_id:
                row.request_id = request_id

            if usage is None:
                row.status = STATUS_UNKNOWN
                row.cost_micro = None
                row.error_class = error_class
                row.updated_at = _now()
                session.flush()
                return row

            row.input_tokens = usage.input_tokens
            row.output_tokens = usage.output_tokens

            if actual_cost_micro is not None:
                row.cost_micro = int(actual_cost_micro)
                row.status = STATUS_SETTLED
                row.updated_at = _now()
                session.flush()
                return row

            if row.input_micro_per_million is None or row.output_micro_per_million is None:
                row.status = STATUS_UNKNOWN
                row.cost_micro = None
                row.updated_at = _now()
                session.flush()
                return row

            foreign = _ceil_div(
                usage.input_tokens * row.input_micro_per_million, MICRO_PER_YUAN
            ) + _ceil_div(usage.output_tokens * row.output_micro_per_million, MICRO_PER_YUAN)
            fx = fx_rate_micro(row.price_currency or CNY)
            if fx is None:
                row.status = STATUS_UNKNOWN
                row.cost_micro = None
            else:
                rate_micro, captured_at = fx
                row.cost_micro = _ceil_div(foreign * rate_micro, MICRO_PER_YUAN)
                row.fx_rate_micro = rate_micro
                row.fx_captured_at = _parse_dt(captured_at)
                row.status = STATUS_SETTLED
            row.updated_at = _now()
            session.flush()
            return row

    def release_atomic(
        self,
        call_id: str,
        *,
        reason: str | None = None,
        db: Session | None = None,
    ) -> ModelCall:
        """原子释放未发出的 ``RESERVED`` 预留；已结算或 ``UNKNOWN`` 记录保持原样（防误归零）。"""
        with self._write_tx(db) as session:
            row = lock_row(session, ModelCall, call_id)
            if row is None:
                raise AppException(
                    ErrorCode.RUN_NOT_FOUND, f"未知 call_id：{call_id}", status_code=404
                )
            if row.status != STATUS_RESERVED:
                return row
            row.status = STATUS_RELEASED
            row.reserved_micro = 0
            if reason:
                row.error_class = reason
            row.updated_at = _now()
            session.flush()
            return row

    def balance(
        self,
        project_id: int,
        *,
        budget_limit_micro: int | None = None,
        db: Session | None = None,
    ) -> BudgetSnapshot:
        with self._read_session(db) as session:
            limit = (
                int(budget_limit_micro)
                if budget_limit_micro is not None
                else effective_budget_micro(session, project_id)
            )
            settled, reserved = _totals(session, project_id)
            return BudgetSnapshot(
                project_id=project_id,
                effective_budget_micro=limit,
                settled_micro=settled,
                reserved_micro=reserved,
                available_micro=limit - settled - reserved,
            )

    def cost_by_department(self, run_id: int, *, db: Session | None = None) -> dict[str, Any]:
        with self._read_session(db) as session:
            return cost_by_department(session, run_id)


__all__ = [
    "STATUS_NOT_SENT",
    "STATUS_RELEASED",
    "STATUS_RESERVED",
    "STATUS_SETTLED",
    "STATUS_UNKNOWN",
    "UNASSIGNED_DEPT",
    "UNASSIGNED_PHASE",
    "BudgetSnapshot",
    "PgBudgetLedger",
    "cost_by_department",
]
