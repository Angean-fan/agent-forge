"""FP01：工厂运行数据模型、持久化状态机与取消/恢复语义测试。"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from agentforge_platform.asset_registry import default_registry
from agentforge_platform.db import (
    drop_postgres_schema,
    ensure_postgres_database,
    ensure_postgres_schema,
    isolated_schema_url,
    make_engine,
    make_session_factory,
    postgres_reachable,
    read_local_env_var,
    session_scope,
)
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.factory_pipeline import (
    FACTORY_PIPELINE_ENV,
    factory_pipeline_enabled,
    run_factory_pipeline,
)
from agentforge_platform.factory_state import (
    BlueprintStatus,
    DeptStatus,
    FactoryCancelledError,
    FactoryPhase,
    FactoryStateStore,
)
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.models import ModelCall, Project, Run, RunEvent
from agentforge_platform.runs import RunStatus, create_run, request_cancel

PLATFORM_API_DIR = Path(__file__).resolve().parents[1]
PG_SAMPLE_URL = "postgresql+psycopg://angean:placeholder@127.0.0.1:15432/agentforge"


class _DummyRunner:
    def verify(self, artifact_dir: str, checks: list[str] | None = None) -> dict[str, Any]:
        return {"ok": True, "checks": checks or []}

    def accept(self, artifact_dir: str, scenarios: list[str] | None = None) -> dict[str, Any]:
        return {"ok": True, "scenarios": scenarios or []}


def _alembic_config(url: str) -> Config:
    config = Config(str(PLATFORM_API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_API_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def _seed_run(factory, *, status: RunStatus = RunStatus.QUEUED) -> tuple[int, int]:
    with session_scope(factory) as db:
        project = Project(owner_id=1, name="ecommerce-demo", requirement_text="shop")
        db.add(project)
        db.flush()
        run = create_run(db, project, spec_id=1, spec_hash="a" * 64)
        if status is not RunStatus.QUEUED:
            run.status = status.value
            db.flush()
        return project.id, run.id


def test_migration_0018_sqlite_upgrade_downgrade_and_pg_ddl(tmp_path: Path) -> None:
    db_file = tmp_path / "fp01_mig.db"
    url = f"sqlite+pysqlite:///{db_file}"
    cfg = _alembic_config(url)

    # 先升到 0017 记录既有 run / task 列集合，确保 0018 零侵入
    command.upgrade(cfg, "0017")
    engine = make_engine(url)
    try:
        insp_before = sa.inspect(engine)
        run_cols_before = {c["name"] for c in insp_before.get_columns("run")}
        task_cols_before = {c["name"] for c in insp_before.get_columns("task")}
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")
    engine = make_engine(url)
    try:
        insp = sa.inspect(engine)
        tables = set(insp.get_table_names())
        assert {"factory_blueprint", "department_run", "defect_ticket"} <= tables

        # 确认既有 run / task 表零改动
        assert {c["name"] for c in insp.get_columns("run")} == run_cols_before
        assert {c["name"] for c in insp.get_columns("task")} == task_cols_before

        bp_cols = {c["name"] for c in insp.get_columns("factory_blueprint")}
        assert {
            "id",
            "run_id",
            "phase",
            "version",
            "blueprint_json",
            "blueprint_hash",
            "status",
        } <= bp_cols

        dept_cols = {c["name"] for c in insp.get_columns("department_run")}
        assert {
            "id",
            "run_id",
            "phase",
            "dept_id",
            "status",
            "current_round",
            "max_rounds",
            "staging_dir",
            "api_fragment_json",
            "evidence_json",
            "error_code",
            "started_at",
            "finished_at",
        } <= dept_cols

        ticket_cols = {c["name"] for c in insp.get_columns("defect_ticket")}
        assert {
            "id",
            "run_id",
            "phase",
            "dept_id",
            "scope",
            "round",
            "error_class",
            "payload_json",
            "resolved",
        } <= ticket_cols
    finally:
        engine.dispose()

    # 回滚到 0017 → 三张新表 cleanly 移除
    command.downgrade(cfg, "0017")
    engine = make_engine(url)
    try:
        tables_after_down = set(sa.inspect(engine).get_table_names())
        assert not ({"factory_blueprint", "department_run", "defect_ticket"} & tables_after_down)
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")

    # PostgreSQL 方言离线 DDL 校验
    pg_cfg = _alembic_config(PG_SAMPLE_URL)
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(pg_cfg, "0017:0018", sql=True)
    ddl = buf.getvalue()
    assert "CREATE TABLE factory_blueprint" in ddl
    assert "CREATE TABLE department_run" in ddl
    assert "CREATE TABLE defect_ticket" in ddl
    assert "uq_factory_blueprint_run_phase_version" in ddl
    assert "uq_department_run_run_phase_dept" in ddl


def test_department_state_whitelist_and_integration_rework_guard(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'fp01_fsm.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        _, run_id = _seed_run(factory)
        store = FactoryStateStore(factory)
        dept = store.register_department(
            run_id, FactoryPhase.BACKEND, "order", max_rounds=3, staging_dir="staging/backend/order"
        )
        assert dept.status == DeptStatus.QUEUED.value
        assert dept.current_round == 0

        # 非法跳步：QUEUED → CODING / QUEUED → PASSED 必须被白名单拒绝
        for illegal in (DeptStatus.CODING, DeptStatus.TESTING, DeptStatus.PASSED, DeptStatus.REWORK):
            with pytest.raises(AppException) as exc:
                store.transition_department(run_id, FactoryPhase.BACKEND, "order", illegal)
            assert exc.value.code == ErrorCode.RUN_INVALID_TRANSITION
            assert exc.value.status_code == 409

        # 正常小循环：QUEUED → PLANNING → CODING → TESTING → REWORK → PLANNING → CODING → TESTING → PASSED
        d1 = store.transition_department(
            run_id, FactoryPhase.BACKEND, "order", DeptStatus.PLANNING, current_round=1
        )
        assert d1.started_at is not None
        assert d1.finished_at is None
        store.transition_department(run_id, FactoryPhase.BACKEND, "order", DeptStatus.CODING)
        store.transition_department(run_id, FactoryPhase.BACKEND, "order", DeptStatus.TESTING)
        store.transition_department(
            run_id,
            FactoryPhase.BACKEND,
            "order",
            DeptStatus.REWORK,
            reason="unit_test_failed",
        )
        store.transition_department(
            run_id, FactoryPhase.BACKEND, "order", DeptStatus.PLANNING, current_round=2
        )
        store.transition_department(run_id, FactoryPhase.BACKEND, "order", DeptStatus.CODING)
        store.transition_department(run_id, FactoryPhase.BACKEND, "order", DeptStatus.TESTING)
        passed = store.transition_department(
            run_id,
            FactoryPhase.BACKEND,
            "order",
            DeptStatus.PASSED,
            api_fragment_json='{"paths":{"/orders":{}}}',
            evidence_json='{"exit_code":0}',
        )
        assert passed.status == DeptStatus.PASSED.value
        assert passed.finished_at is not None

        # PASSED 不可被外部非法覆盖，包括不带 allow_integration_rework 的 REWORK
        for forbidden in (
            DeptStatus.QUEUED,
            DeptStatus.PLANNING,
            DeptStatus.CODING,
            DeptStatus.TESTING,
            DeptStatus.REWORK,
            DeptStatus.FAILED,
            DeptStatus.CANCELLED,
        ):
            with pytest.raises(AppException) as exc:
                store.transition_department(run_id, FactoryPhase.BACKEND, "order", forbidden)
            assert exc.value.code == ErrorCode.RUN_INVALID_TRANSITION

        # 仅在总装归因显式打回（allow_integration_rework=True）时允许 PASSED → REWORK
        reworked = store.transition_department(
            run_id,
            FactoryPhase.BACKEND,
            "order",
            DeptStatus.REWORK,
            allow_integration_rework=True,
            reason="integration_fk_mismatch",
        )
        assert reworked.status == DeptStatus.REWORK.value
        assert reworked.finished_at is None

        # 失败终态不可逆
        store.register_department(run_id, FactoryPhase.BACKEND, "payment")
        store.transition_department(
            run_id, FactoryPhase.BACKEND, "payment", DeptStatus.FAILED, error_code="MAX_ROUNDS"
        )
        with pytest.raises(AppException) as exc:
            store.transition_department(run_id, FactoryPhase.BACKEND, "payment", DeptStatus.QUEUED)
        assert exc.value.code == ErrorCode.RUN_INVALID_TRANSITION
    finally:
        engine.dispose()


def test_blueprint_versioning_and_defect_ticket_lifecycle(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'fp01_bp_tickets.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        _, run_id = _seed_run(factory)
        store = FactoryStateStore(factory)

        # 保存蓝图 v1 DRAFT → 升级为 APPROVED_FOR_BUILD
        bp1 = store.save_blueprint(
            run_id,
            FactoryPhase.BACKEND,
            version=1,
            blueprint_json='{"modules":["auth","order"]}',
            blueprint_hash="h" * 64,
            status=BlueprintStatus.DRAFT,
        )
        assert bp1.status == BlueprintStatus.DRAFT.value

        bp1_approved = store.save_blueprint(
            run_id,
            FactoryPhase.BACKEND,
            version=1,
            blueprint_json='{"modules":["auth","order"]}',
            blueprint_hash="h" * 64,
            status=BlueprintStatus.APPROVED_FOR_BUILD,
        )
        assert bp1_approved.status == BlueprintStatus.APPROVED_FOR_BUILD.value

        # 同版本不同哈希禁止覆盖
        with pytest.raises(AppException) as exc:
            store.save_blueprint(
                run_id,
                FactoryPhase.BACKEND,
                version=1,
                blueprint_json='{"modules":["tampered"]}',
                blueprint_hash="x" * 64,
                status=BlueprintStatus.APPROVED_FOR_BUILD,
            )
        assert exc.value.code == ErrorCode.CONTRACT_CONFLICT

        # v2 APPROVED_FOR_BUILD 自动将 v1 标记为 SUPERSEDED
        bp2 = store.save_blueprint(
            run_id,
            FactoryPhase.BACKEND,
            version=2,
            blueprint_json='{"modules":["auth","order","cart"]}',
            blueprint_hash="2" * 64,
            status=BlueprintStatus.APPROVED_FOR_BUILD,
        )
        assert bp2.version == 2
        all_bps = store.list_blueprints(run_id, FactoryPhase.BACKEND)
        assert [b.status for b in all_bps] == [
            BlueprintStatus.SUPERSEDED.value,
            BlueprintStatus.APPROVED_FOR_BUILD.value,
        ]
        latest = store.get_latest_blueprint(run_id, FactoryPhase.BACKEND, approved_only=True)
        assert latest is not None and latest.version == 2

        # 缺陷单登记、查询与按部门解决
        t1 = store.open_defect_ticket(
            run_id,
            FactoryPhase.BACKEND,
            "order",
            scope="department",
            round_num=1,
            error_class="UNIT_TEST_FAILURE",
            payload={"summary": "OrderServiceTest failed"},
        )
        t2 = store.open_defect_ticket(
            run_id,
            FactoryPhase.BACKEND,
            "order",
            scope="integration",
            round_num=1,
            error_class="FLYWAY_MIGRATION_ERROR",
            payload={"summary": "FK mismatch"},
        )
        assert len(store.list_defect_tickets(run_id, dept_id="order", resolved=False)) == 2
        resolved_t1 = store.resolve_defect_ticket(t1.id)
        assert resolved_t1.resolved is True
        assert resolved_t1.resolved_at is not None

        resolved_count = store.resolve_department_tickets(run_id, FactoryPhase.BACKEND, "order")
        assert resolved_count == 1
        assert store.list_defect_tickets(run_id, dept_id="order", resolved=False) == []
        assert len(store.list_defect_tickets(run_id, dept_id="order", resolved=True)) == 2
        assert t2.id in {t.id for t in store.list_defect_tickets(run_id, resolved=True)}
    finally:
        engine.dispose()


def test_concurrent_multi_department_updates_conflict_free(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'fp01_concurrent.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        _, run_id = _seed_run(factory)
        store = FactoryStateStore(factory)
        depts = ["auth", "product", "cart", "order", "payment"]
        store.register_departments(run_id, FactoryPhase.BACKEND, depts)

        def _drive_department(dept_id: str) -> str:
            store.transition_department(
                run_id, FactoryPhase.BACKEND, dept_id, DeptStatus.PLANNING, current_round=1
            )
            store.transition_department(run_id, FactoryPhase.BACKEND, dept_id, DeptStatus.CODING)
            store.transition_department(run_id, FactoryPhase.BACKEND, dept_id, DeptStatus.TESTING)
            if dept_id == "cart":
                store.open_defect_ticket(
                    run_id,
                    FactoryPhase.BACKEND,
                    dept_id,
                    scope="department",
                    round_num=1,
                    error_class="TEST_FAIL",
                    payload={"dept": dept_id},
                )
                store.transition_department(
                    run_id, FactoryPhase.BACKEND, dept_id, DeptStatus.REWORK
                )
                store.transition_department(
                    run_id, FactoryPhase.BACKEND, dept_id, DeptStatus.PLANNING, current_round=2
                )
                store.transition_department(
                    run_id, FactoryPhase.BACKEND, dept_id, DeptStatus.CODING
                )
                store.transition_department(
                    run_id, FactoryPhase.BACKEND, dept_id, DeptStatus.TESTING
                )
                store.resolve_department_tickets(run_id, FactoryPhase.BACKEND, dept_id)
            store.transition_department(
                run_id,
                FactoryPhase.BACKEND,
                dept_id,
                DeptStatus.PASSED,
                evidence_json=json.dumps({"dept": dept_id, "ok": True}),
            )
            return dept_id

        async def _run_all() -> list[str]:
            return list(await asyncio.gather(*(asyncio.to_thread(_drive_department, d) for d in depts)))

        done = asyncio.run(_run_all())
        assert sorted(done) == sorted(depts)
        rows = store.list_departments(run_id, FactoryPhase.BACKEND)
        assert all(r.status == DeptStatus.PASSED.value for r in rows)

        # 验证事件序列严格单调连续、无冲突丢事件
        with session_scope(factory) as db:
            seqs = list(
                db.scalars(
                    sa.select(RunEvent.sequence)
                    .where(RunEvent.run_id == run_id)
                    .order_by(RunEvent.sequence)
                ).all()
            )
        assert seqs == list(range(1, len(seqs) + 1))
        assert len(seqs) >= 24  # 4*4 + 8 + 2 defect events = 26
    finally:
        engine.dispose()


def test_cancellation_propagates_immediately_and_preserves_passed_departments(
    tmp_path: Path,
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'fp01_cancel.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        _, run_id = _seed_run(factory, status=RunStatus.PLANNING)
        store = FactoryStateStore(factory)
        store.register_departments(run_id, FactoryPhase.BACKEND, ["auth", "order", "payment"])

        # auth 已完成 PASSED；order 正在 CODING；payment 仍在 QUEUED
        store.transition_department(run_id, FactoryPhase.BACKEND, "auth", DeptStatus.PLANNING, current_round=1)
        store.transition_department(run_id, FactoryPhase.BACKEND, "auth", DeptStatus.CODING)
        store.transition_department(run_id, FactoryPhase.BACKEND, "auth", DeptStatus.TESTING)
        store.transition_department(run_id, FactoryPhase.BACKEND, "auth", DeptStatus.PASSED)

        store.transition_department(run_id, FactoryPhase.BACKEND, "order", DeptStatus.PLANNING, current_round=1)
        store.transition_department(run_id, FactoryPhase.BACKEND, "order", DeptStatus.CODING)

        # 经既有 runs.request_cancel 置位共享取消信号
        with session_scope(factory) as db:
            run = db.get(Run, run_id)
            assert run is not None
            request_cancel(db, run)

        assert store.is_cancel_requested(run_id) is True
        with pytest.raises(FactoryCancelledError):
            store.raise_if_cancelled(run_id, boundary="dept_round")

        # 取消后禁止继续推进在途部门
        with pytest.raises(FactoryCancelledError):
            store.transition_department(run_id, FactoryPhase.BACKEND, "order", DeptStatus.TESTING)

        # 收敛未完成部门为 CANCELLED，保留 PASSED 不被破坏
        cancelled = store.cancel_incomplete_departments(run_id, FactoryPhase.BACKEND)
        assert cancelled == ["order", "payment"]

        auth_row = store.get_department(run_id, FactoryPhase.BACKEND, "auth")
        order_row = store.get_department(run_id, FactoryPhase.BACKEND, "order")
        payment_row = store.get_department(run_id, FactoryPhase.BACKEND, "payment")
        assert auth_row is not None and auth_row.status == DeptStatus.PASSED.value
        assert order_row is not None and order_row.status == DeptStatus.CANCELLED.value
        assert payment_row is not None and payment_row.status == DeptStatus.CANCELLED.value
    finally:
        engine.dispose()


def test_factory_pipeline_mid_stage_cancellation_converges_run_and_departments(
    tmp_path: Path,
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'fp01_mid_cancel.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        _, run_id = _seed_run(factory)
        frontend_called = False

        def _backend_then_cancel(
            _ctx: GenerationContext,
            rid: int,
            st: FactoryStateStore,
            phase: FactoryPhase,
        ) -> dict[str, Any]:
            st.register_departments(rid, phase, ["auth", "order"])
            st.transition_department(rid, phase, "auth", DeptStatus.PLANNING, current_round=1)
            st.transition_department(rid, phase, "auth", DeptStatus.CODING)
            st.transition_department(rid, phase, "auth", DeptStatus.TESTING)
            st.transition_department(rid, phase, "auth", DeptStatus.PASSED)
            st.transition_department(rid, phase, "order", DeptStatus.PLANNING, current_round=1)
            with session_scope(factory) as db:
                run = db.get(Run, rid)
                assert run is not None
                request_cancel(db, run)
            # 下一步尝试推进 order 时立即触发 FactoryCancelledError
            st.transition_department(rid, phase, "order", DeptStatus.CODING)
            return {}

        def _frontend_should_not_run(*_args: Any) -> dict[str, Any]:
            nonlocal frontend_called
            frontend_called = True
            return {}

        ctx = GenerationContext(
            session_factory=factory,
            runner=_DummyRunner(),
            work_root=tmp_path / "runs",
            registry=default_registry(),
        )
        res = run_factory_pipeline(
            ctx,
            run_id,
            backend_runner=_backend_then_cancel,
            frontend_runner=_frontend_should_not_run,
        )
        assert res["ok"] is False
        assert res["error_code"] == "RUN_CANCELLED"
        assert res["cancelled_depts"] == ["order"]
        assert frontend_called is False

        store = FactoryStateStore(factory)
        assert store.get_department(run_id, FactoryPhase.BACKEND, "auth").status == "PASSED"
        assert store.get_department(run_id, FactoryPhase.BACKEND, "order").status == "CANCELLED"
        with session_scope(factory) as db:
            run = db.get(Run, run_id)
            assert run is not None and run.status == RunStatus.CANCELLED.value
    finally:
        engine.dispose()


def test_crash_recovery_skips_passed_departments_and_resumes_incomplete_only(
    tmp_path: Path,
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'fp01_recover.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        _, run_id = _seed_run(factory, status=RunStatus.GENERATING)
        store = FactoryStateStore(factory)
        store.register_departments(
            run_id, FactoryPhase.BACKEND, ["auth", "product", "cart", "order", "payment"]
        )

        # 崩溃前现场：auth & product 已 PASSED；cart 在 CODING；order 在 TESTING；payment 仍 QUEUED
        for passed_dept in ("auth", "product"):
            store.transition_department(
                run_id, FactoryPhase.BACKEND, passed_dept, DeptStatus.PLANNING, current_round=1
            )
            store.transition_department(run_id, FactoryPhase.BACKEND, passed_dept, DeptStatus.CODING)
            store.transition_department(run_id, FactoryPhase.BACKEND, passed_dept, DeptStatus.TESTING)
            store.transition_department(
                run_id,
                FactoryPhase.BACKEND,
                passed_dept,
                DeptStatus.PASSED,
                evidence_json='{"from":"before_crash"}',
            )

        store.transition_department(run_id, FactoryPhase.BACKEND, "cart", DeptStatus.PLANNING, current_round=1)
        store.transition_department(run_id, FactoryPhase.BACKEND, "cart", DeptStatus.CODING)

        store.transition_department(run_id, FactoryPhase.BACKEND, "order", DeptStatus.PLANNING, current_round=1)
        store.transition_department(run_id, FactoryPhase.BACKEND, "order", DeptStatus.CODING)
        store.transition_department(run_id, FactoryPhase.BACKEND, "order", DeptStatus.TESTING)

        auth_finished_before = store.get_department(run_id, FactoryPhase.BACKEND, "auth").finished_at

        # 模拟进程崩溃重启后通过顶层编排器恢复执行
        executed: list[str] = []

        def _fake_backend_stage(
            _ctx: GenerationContext,
            rid: int,
            st: FactoryStateStore,
            phase: FactoryPhase,
        ) -> dict[str, Any]:
            # 再次幂等注册不得覆盖已 PASSED 部门
            st.register_departments(rid, phase, ["auth", "product", "cart", "order", "payment"])
            plan = st.scan_recovery_plan(rid, phase, reset_inflight=False)
            assert plan.skipped_passed == ("auth", "product")
            assert plan.to_run == ("cart", "order", "payment")
            for dept_id in plan.to_run:
                executed.append(dept_id)
                st.transition_department(rid, phase, dept_id, DeptStatus.PLANNING, current_round=2)
                st.transition_department(rid, phase, dept_id, DeptStatus.CODING)
                st.transition_department(rid, phase, dept_id, DeptStatus.TESTING)
                st.transition_department(rid, phase, dept_id, DeptStatus.PASSED)
            return {"executed": list(executed)}

        ctx = GenerationContext(
            session_factory=factory,
            runner=_DummyRunner(),
            work_root=tmp_path / "runs",
            registry=default_registry(),
        )
        result = run_factory_pipeline(ctx, run_id, backend_runner=_fake_backend_stage)

        assert result["ok"] is True
        assert result["recovery"]["backend"]["skipped_passed"] == ["auth", "product"]
        assert result["recovery"]["backend"]["recovered_from_inflight"] == ["cart", "order"]
        assert result["recovery"]["backend"]["to_run"] == ["cart", "order", "payment"]
        assert executed == ["cart", "order", "payment"]

        # 已通过部门的时间戳与证据完全未动
        auth_after = store.get_department(run_id, FactoryPhase.BACKEND, "auth")
        assert auth_after is not None
        assert auth_after.finished_at == auth_finished_before
        assert auth_after.evidence_json == '{"from":"before_crash"}'
        assert len(store.passed_departments(run_id, FactoryPhase.BACKEND)) == 5
    finally:
        engine.dispose()


def test_crash_recovery_blocks_when_unsettled_model_calls_exist(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'fp01_unsettled.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        project_id, run_id = _seed_run(factory, status=RunStatus.GENERATING)
        with session_scope(factory) as db:
            db.add(
                ModelCall(
                    call_id="unsettled-fp01",
                    model_role="dept_coder",
                    provider="deepseek_official",
                    model="deepseek-chat",
                    project_id=project_id,
                    run_id=run_id,
                    dept_id="order",
                    phase="backend",
                    round=1,
                    reserved_micro=100_000,
                    status="UNKNOWN",
                )
            )
        ctx = GenerationContext(
            session_factory=factory,
            runner=_DummyRunner(),
            work_root=tmp_path / "runs",
            registry=default_registry(),
        )
        called = False

        def _should_not_run(*_args: Any) -> dict[str, Any]:
            nonlocal called
            called = True
            return {}

        res = run_factory_pipeline(ctx, run_id, backend_runner=_should_not_run)
        assert res["ok"] is False
        assert res["error_code"] == "NEEDS_RECONCILIATION"
        assert called is False
        with session_scope(factory) as db:
            run = db.get(Run, run_id)
            assert run is not None and run.status == RunStatus.FAILED.value
    finally:
        engine.dispose()


def test_feature_flag_default_off_and_explicit_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(FACTORY_PIPELINE_ENV, raising=False)
    assert factory_pipeline_enabled() is False

    for false_val in ("0", "false", "FALSE", "no", ""):
        monkeypatch.setenv(FACTORY_PIPELINE_ENV, false_val)
        assert factory_pipeline_enabled() is False

    # 关闭开关时，run_generation 绝对不触发 run_factory_pipeline
    monkeypatch.setenv(FACTORY_PIPELINE_ENV, "false")
    url = f"sqlite+pysqlite:///{tmp_path / 'fp01_flag.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        _, run_id = _seed_run(factory)
        with session_scope(factory) as db:
            run = db.get(Run, run_id)
            assert run is not None
            run.cancel_requested = True

        def _trap(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("factory_pipeline must not be called when feature flag is off")

        monkeypatch.setattr("agentforge_platform.factory_pipeline.run_factory_pipeline", _trap)
        ctx = GenerationContext(
            session_factory=factory,
            runner=_DummyRunner(),
            work_root=tmp_path / "runs",
            registry=default_registry(),
        )
        res_off = run_generation(ctx, run_id)
        assert res_off["error_code"] == "RUN_CANCELLED"
        assert "mode" not in res_off  # 来自原串行 generation_graph 路径

        # 显式开启开关时分发至 factory_pipeline（无真实阶段执行器时明确落 FAILED，绝不假成功）
        monkeypatch.undo()
        monkeypatch.setenv(FACTORY_PIPELINE_ENV, "true")
        assert factory_pipeline_enabled() is True

        _, run_id_2 = _seed_run(factory)
        res_on = run_generation(ctx, run_id_2)
        assert res_on["ok"] is False
        assert res_on["mode"] == "factory"
        assert res_on["error_code"] == "FACTORY_STAGE_NOT_IMPLEMENTED"
        with session_scope(factory) as db:
            run2 = db.get(Run, run_id_2)
            assert run2 is not None
            assert run2.status == RunStatus.FAILED.value
            assert run2.error_code == "FACTORY_STAGE_NOT_IMPLEMENTED"
    finally:
        engine.dispose()


def test_live_postgres_migration_0018_concurrent_state_and_recovery() -> None:
    """实连本地/隧道 PostgreSQL：在隔离 Schema 验证 0018 升降级、PG 行锁并发状态更新与崩溃恢复。"""
    pg_url = (
        read_local_env_var("AGENTFORGE_TEST_DATABASE_URL")
        or read_local_env_var("AGENTFORGE_DATABASE_URL")
    )
    if not pg_url or not postgres_reachable(pg_url):
        pytest.skip("NOT_RUN: 本地 PostgreSQL 或云端隧道未连接，跳过实库 PG FP01 验证")

    ensure_postgres_database(pg_url)
    schema = f"test_fp01_{uuid.uuid4().hex[:8]}"
    ensure_postgres_schema(pg_url, schema)
    scoped_url = isolated_schema_url(pg_url, schema)

    try:
        cfg = _alembic_config(scoped_url)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0017")
        command.upgrade(cfg, "head")

        engine = make_engine(scoped_url)
        factory = make_session_factory(engine)
        try:
            _, run_id = _seed_run(factory)
            store = FactoryStateStore(factory)
            depts = ["auth", "product", "cart", "order", "payment"]
            store.register_departments(run_id, FactoryPhase.BACKEND, depts)

            # 5 部门在真实 PG 行锁下并发推进，其中 3 部门跑完 PASSED，2 部门模拟中途崩溃
            def _worker(dept_id: str) -> None:
                store.transition_department(
                    run_id, FactoryPhase.BACKEND, dept_id, DeptStatus.PLANNING, current_round=1
                )
                store.transition_department(
                    run_id, FactoryPhase.BACKEND, dept_id, DeptStatus.CODING
                )
                if dept_id in {"auth", "product", "payment"}:
                    store.transition_department(
                        run_id, FactoryPhase.BACKEND, dept_id, DeptStatus.TESTING
                    )
                    store.transition_department(
                        run_id, FactoryPhase.BACKEND, dept_id, DeptStatus.PASSED
                    )

            async def _run_pg() -> None:
                await asyncio.gather(*(asyncio.to_thread(_worker, d) for d in depts))

            asyncio.run(_run_pg())

            plan = store.scan_recovery_plan(run_id, FactoryPhase.BACKEND, reset_inflight=True)
            assert plan.skipped_passed == ("auth", "payment", "product")
            assert plan.recovered_from_inflight == ("cart", "order")
            assert plan.to_run == ("cart", "order")

            # 恢复后只重跑 cart / order 即可全数 PASSED
            for dept_id in plan.to_run:
                store.transition_department(
                    run_id, FactoryPhase.BACKEND, dept_id, DeptStatus.PLANNING, current_round=2
                )
                store.transition_department(
                    run_id, FactoryPhase.BACKEND, dept_id, DeptStatus.CODING
                )
                store.transition_department(
                    run_id, FactoryPhase.BACKEND, dept_id, DeptStatus.TESTING
                )
                store.transition_department(
                    run_id, FactoryPhase.BACKEND, dept_id, DeptStatus.PASSED
                )

            assert len(store.passed_departments(run_id, FactoryPhase.BACKEND)) == 5
        finally:
            engine.dispose()
    finally:
        drop_postgres_schema(pg_url, schema)
