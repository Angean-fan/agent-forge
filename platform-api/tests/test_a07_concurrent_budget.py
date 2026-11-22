"""A07：并发预算池与事务账本升级（PG 行锁防双扣 + 部门维度记账）测试。"""

from __future__ import annotations

import asyncio
import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from agentforge_platform.budget_pg import (
    STATUS_RELEASED,
    STATUS_RESERVED,
    STATUS_SETTLED,
    STATUS_UNKNOWN,
    UNASSIGNED_DEPT,
    UNASSIGNED_PHASE,
    PgBudgetLedger,
    cost_by_department,
)
from agentforge_platform.clients import Usage
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
from agentforge_platform.model_gateway import Price
from agentforge_platform.models import ModelCall, Project
from agentforge_platform.packaging import build_cost_report

PLATFORM_API_DIR = Path(__file__).resolve().parents[1]
PG_SAMPLE_URL = "postgresql+psycopg://angean:placeholder@127.0.0.1:15432/agentforge"


def _alembic_config(url: str) -> Config:
    config = Config(str(PLATFORM_API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_API_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def _seed_project(factory) -> int:
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="ecommerce-factory", requirement_text="shop")
        session.add(project)
        session.flush()
        return project.id


def test_migration_0017_sqlite_upgrade_downgrade_and_pg_ddl(tmp_path: Path) -> None:
    # 1. SQLite 实库：0001..0017 升级 → 列与索引就位 → 降级至 0016 → 再升级至 head
    db_file = tmp_path / "a07_mig.db"
    url = f"sqlite+pysqlite:///{db_file}"
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")

    engine = make_engine(url)
    try:
        insp = sa.inspect(engine)
        cols = {c["name"]: c for c in insp.get_columns("model_call")}
        assert {"dept_id", "phase", "round"}.issubset(cols)
        assert cols["dept_id"]["nullable"] is True
        assert cols["phase"]["nullable"] is True
        assert cols["round"]["nullable"] is True
        idx_names = {idx["name"] for idx in insp.get_indexes("model_call")}
        assert "ix_model_call_run_dept_phase" in idx_names
    finally:
        engine.dispose()

    command.downgrade(cfg, "0016")
    engine = make_engine(url)
    try:
        cols_after_down = {c["name"] for c in sa.inspect(engine).get_columns("model_call")}
        assert "dept_id" not in cols_after_down
        assert "phase" not in cols_after_down
        assert "round" not in cols_after_down
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")

    # 2. PostgreSQL 方言离线 DDL 验证
    pg_cfg = _alembic_config(PG_SAMPLE_URL)
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(pg_cfg, "0016:0017", sql=True)
    sql = buf.getvalue()
    assert "ADD COLUMN dept_id VARCHAR(64)" in sql
    assert "ADD COLUMN phase VARCHAR(16)" in sql
    assert "ADD COLUMN round INTEGER" in sql
    assert "ix_model_call_run_dept_phase" in sql


def test_10_concurrent_coroutines_race_for_3_slot_budget_and_idempotent_settle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 项目总预算 = 3_000_000 微元（3 元），单次预留 = 1_000_000 微元（1 元）→ 仅够 3 次调用
    monkeypatch.setenv("AGENTFORGE_PROJECT_BUDGET_MICRO", "3000000")
    db_file = tmp_path / "a07_concurrent.db"
    url = f"sqlite+pysqlite:///{db_file}"
    command.upgrade(_alembic_config(url), "head")

    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        project_id = _seed_project(factory)
        ledger = PgBudgetLedger(factory)
        depts = ["auth", "product", "cart", "order", "payment"]
        price = Price(
            version="test-v1",
            input_micro_per_million=2_000_000,
            output_micro_per_million=4_000_000,
            currency="CNY",
        )

        async def _race() -> list[tuple[str, str]]:
            async def _worker(idx: int) -> tuple[str, str]:
                call_id = f"race-call-{idx:02d}"
                try:
                    await asyncio.to_thread(
                        ledger.reserve_atomic,
                        call_id=call_id,
                        project_id=project_id,
                        run_id=101,
                        model_role="dept_coder",
                        provider="deepseek_official",
                        model="deepseek-chat",
                        reserve_micro=1_000_000,
                        dept_id=depts[idx % len(depts)],
                        phase="backend",
                        round_num=1,
                        price=price,
                    )
                    return call_id, "OK"
                except AppException as exc:
                    assert exc.code == ErrorCode.BUDGET_EXCEEDED
                    assert exc.status_code == 402
                    return call_id, exc.code.value

            return list(await asyncio.gather(*(_worker(i) for i in range(10))))

        outcomes = asyncio.run(_race())
        succeeded = [cid for cid, status in outcomes if status == "OK"]
        blocked = [cid for cid, status in outcomes if status == ErrorCode.BUDGET_EXCEEDED.value]
        assert len(succeeded) == 3
        assert len(blocked) == 7

        snap = ledger.balance(project_id)
        assert snap.effective_budget_micro == 3_000_000
        assert snap.settled_micro == 0
        assert snap.reserved_micro == 3_000_000
        assert snap.available_micro == 0

        # 同 call_id 重复 reserve_atomic 幂等不双扣
        dup = ledger.reserve_atomic(
            call_id=succeeded[0],
            project_id=project_id,
            run_id=101,
            model_role="dept_coder",
            provider="deepseek_official",
            model="deepseek-chat",
            reserve_micro=1_000_000,
            dept_id="auth",
            phase="backend",
            round_num=1,
            price=price,
        )
        assert dup.call_id == succeeded[0]
        assert dup.status == STATUS_RESERVED
        assert ledger.balance(project_id).reserved_micro == 3_000_000

        # 并发重复结算同一 call_id（5 线程同时结算）→ 绝对幂等、零双扣
        # 用量：100_000 in * 2元/M + 100_000 out * 4元/M = 200_000 + 400_000 = 600_000 微元
        usage_0 = Usage(input_tokens=100_000, output_tokens=100_000)
        with ThreadPoolExecutor(max_workers=5) as pool:
            settled_rows = list(
                pool.map(
                    lambda _: ledger.settle_idempotent(
                        succeeded[0], usage=usage_0, request_id="req-0"
                    ),
                    range(5),
                )
            )
        assert all(r.status == STATUS_SETTLED and r.cost_micro == 600_000 for r in settled_rows)

        # 结算另外 2 笔：分别 700_000 和 500_000
        ledger.settle_idempotent(
            succeeded[1],
            usage=Usage(input_tokens=150_000, output_tokens=100_000),
            request_id="req-1",
        )
        ledger.settle_idempotent(
            succeeded[2],
            usage=Usage(input_tokens=50_000, output_tokens=100_000),
            request_id="req-2",
        )

        # 总账精确吻合：已结算 600_000 + 700_000 + 500_000 = 1_800_000，在途预留 0，剩余 1_200_000
        final_snap = ledger.balance(project_id)
        assert final_snap.settled_micro == 1_800_000
        assert final_snap.reserved_micro == 0
        assert final_snap.available_micro == 1_200_000

        # 释放出 1_200_000 余额后，可再精确放行 1 笔 1_000_000 预留，第 2 笔被拦截
        ledger.reserve_atomic(
            call_id="after-settle-ok",
            project_id=project_id,
            run_id=101,
            model_role="dept_tester",
            provider="deepseek_official",
            model="deepseek-chat",
            reserve_micro=1_000_000,
            dept_id="auth",
            phase="backend",
            round_num=2,
            price=price,
        )
        with pytest.raises(AppException) as exc_info:
            ledger.reserve_atomic(
                call_id="after-settle-blocked",
                project_id=project_id,
                run_id=101,
                model_role="dept_tester",
                provider="deepseek_official",
                model="deepseek-chat",
                reserve_micro=1_000_000,
                dept_id="auth",
                phase="backend",
                round_num=2,
                price=price,
            )
        assert exc_info.value.code == ErrorCode.BUDGET_EXCEEDED
    finally:
        engine.dispose()


def test_unknown_usage_and_unpriced_keep_reservation_while_release_only_frees_unsent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTFORGE_PROJECT_BUDGET_MICRO", "5000000")
    db_file = tmp_path / "a07_unknown_release.db"
    url = f"sqlite+pysqlite:///{db_file}"
    command.upgrade(_alembic_config(url), "head")

    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        project_id = _seed_project(factory)
        ledger = PgBudgetLedger(factory)
        price = Price(version="v1", input_micro_per_million=1_000_000, output_micro_per_million=1_000_000)

        # 1. usage 缺失 → 记 UNKNOWN，严禁记零，必须保留预留
        ledger.reserve_atomic(
            call_id="c-no-usage",
            project_id=project_id,
            run_id=202,
            model_role="dept_coder",
            provider="openai_compat",
            model="custom-coder",
            reserve_micro=1_000_000,
            dept_id="order",
            phase="backend",
            round_num=1,
            price=price,
        )
        row_unknown = ledger.settle_idempotent(
            "c-no-usage", usage=None, error_class="ProviderTimeoutError"
        )
        assert row_unknown.status == STATUS_UNKNOWN
        assert row_unknown.cost_micro is None
        assert row_unknown.reserved_micro == 1_000_000

        # 对 UNKNOWN 调用 release_atomic 不得归零预留
        still_unknown = ledger.release_atomic("c-no-usage", reason="should_not_clear")
        assert still_unknown.status == STATUS_UNKNOWN
        assert still_unknown.reserved_micro == 1_000_000

        # 2. 未配单价（price=None）即使有 usage 也必须记 UNKNOWN 且保留预留，严禁记零
        ledger.reserve_atomic(
            call_id="c-unpriced",
            project_id=project_id,
            run_id=202,
            model_role="dept_coder",
            provider="ollama_local",
            model="qwen-local-unknown",
            reserve_micro=800_000,
            dept_id="payment",
            phase="backend",
            round_num=1,
            price=None,
        )
        row_unpriced = ledger.settle_idempotent(
            "c-unpriced", usage=Usage(input_tokens=500, output_tokens=200)
        )
        assert row_unpriced.status == STATUS_UNKNOWN
        assert row_unpriced.cost_micro is None
        assert row_unpriced.reserved_micro == 800_000
        assert row_unpriced.input_tokens == 500
        assert row_unpriced.output_tokens == 200

        # 3. 未发出的 RESERVED 调用可以通过 release_atomic 原子释放
        ledger.reserve_atomic(
            call_id="c-preflight-cancel",
            project_id=project_id,
            run_id=202,
            model_role="dept_coder",
            provider="openai_compat",
            model="custom-coder",
            reserve_micro=1_200_000,
            dept_id="cart",
            phase="backend",
            round_num=1,
            price=price,
        )
        assert ledger.balance(project_id).reserved_micro == 3_000_000
        released = ledger.release_atomic("c-preflight-cancel", reason="PreflightAborted")
        assert released.status == STATUS_RELEASED
        assert released.reserved_micro == 0
        # 重复 release 幂等
        released_again = ledger.release_atomic("c-preflight-cancel", reason="PreflightAborted")
        assert released_again.status == STATUS_RELEASED
        assert ledger.balance(project_id).reserved_micro == 1_800_000
    finally:
        engine.dispose()


def test_cost_by_department_and_build_cost_report_compatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTFORGE_PROJECT_BUDGET_MICRO", "20000000")
    db_file = tmp_path / "a07_dept_report.db"
    url = f"sqlite+pysqlite:///{db_file}"
    command.upgrade(_alembic_config(url), "head")

    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        project_id = _seed_project(factory)
        run_id = 303
        ledger = PgBudgetLedger(factory)
        price = Price(
            version="v1",
            input_micro_per_million=1_000_000,
            output_micro_per_million=2_000_000,
            currency="CNY",
        )

        # backend / auth 第 1 轮 & 第 2 轮
        ledger.reserve_atomic(
            call_id="b-auth-1",
            project_id=project_id,
            run_id=run_id,
            model_role="dept_coder",
            provider="deepseek_official",
            model="deepseek-chat",
            reserve_micro=500_000,
            dept_id="auth",
            phase="backend",
            round_num=1,
            price=price,
        )
        ledger.settle_idempotent("b-auth-1", usage=Usage(input_tokens=100_000, output_tokens=50_000))

        ledger.reserve_atomic(
            call_id="b-auth-2",
            project_id=project_id,
            run_id=run_id,
            model_role="dept_tester",
            provider="deepseek_official",
            model="deepseek-chat",
            reserve_micro=500_000,
            dept_id="auth",
            phase="backend",
            round_num=2,
            price=price,
        )
        ledger.settle_idempotent("b-auth-2", usage=Usage(input_tokens=50_000, output_tokens=25_000))

        # backend / order：1 笔 UNKNOWN + 1 笔 RELEASED
        ledger.reserve_atomic(
            call_id="b-order-unknown",
            project_id=project_id,
            run_id=run_id,
            model_role="dept_coder",
            provider="dashscope",
            model="qwen3-coder-flash",
            reserve_micro=600_000,
            dept_id="order",
            phase="backend",
            round_num=1,
            price=price,
        )
        ledger.settle_idempotent("b-order-unknown", usage=None, error_class="ReadTimeout")

        ledger.reserve_atomic(
            call_id="b-order-released",
            project_id=project_id,
            run_id=run_id,
            model_role="dept_coder",
            provider="dashscope",
            model="qwen3-coder-flash",
            reserve_micro=400_000,
            dept_id="order",
            phase="backend",
            round_num=1,
            price=price,
        )
        ledger.release_atomic("b-order-released", reason="Cancelled")

        # frontend / product 第 1 轮
        ledger.reserve_atomic(
            call_id="f-product-1",
            project_id=project_id,
            run_id=run_id,
            model_role="dept_coder",
            provider="deepseek_official",
            model="deepseek-chat",
            reserve_micro=500_000,
            dept_id="product",
            phase="frontend",
            round_num=1,
            price=price,
        )
        ledger.settle_idempotent(
            "f-product-1", usage=Usage(input_tokens=200_000, output_tokens=100_000)
        )

        # 旧格式行（dept_id=None, phase=None）确保向后兼容归入 unassigned
        with session_scope(factory) as session:
            session.add(
                ModelCall(
                    call_id="legacy-run-call",
                    model_role="technical_planning",
                    provider="dashscope",
                    model="qwen3.7-plus",
                    project_id=project_id,
                    run_id=run_id,
                    status=STATUS_SETTLED,
                    input_tokens=10_000,
                    output_tokens=5_000,
                    cost_micro=50_000,
                    reserved_micro=100_000,
                )
            )

        with session_scope(factory) as session:
            dept_report = cost_by_department(session, run_id)
            full_report = build_cost_report(session, project_id=project_id, run_id=run_id)

        # 精确核对按阶段与部门拆分结果
        # b-auth-1: 100k*1 + 50k*2 = 200_000; b-auth-2: 50k*1 + 25k*2 = 100_000 -> auth=300_000
        # f-product-1: 200k*1 + 100k*2 = 400_000
        # legacy: 50_000
        assert dept_report["totals"]["calls"] == 5  # 不计 RELEASED
        assert dept_report["totals"]["settled_calls"] == 4
        assert dept_report["totals"]["unknown_calls"] == 1
        assert dept_report["totals"]["released_calls"] == 1
        assert dept_report["totals"]["cost_micro"] == 750_000
        assert dept_report["totals"]["reserved_micro"] == 600_000
        assert dept_report["totals"]["input_tokens"] == 360_000
        assert dept_report["totals"]["output_tokens"] == 180_000

        backend_auth = dept_report["by_phase"]["backend"]["departments"]["auth"]
        assert backend_auth["calls"] == 2
        assert backend_auth["cost_micro"] == 300_000
        assert backend_auth["by_round"]["1"]["cost_micro"] == 200_000
        assert backend_auth["by_round"]["2"]["cost_micro"] == 100_000

        backend_order = dept_report["by_phase"]["backend"]["departments"]["order"]
        assert backend_order["calls"] == 1
        assert backend_order["unknown_calls"] == 1
        assert backend_order["released_calls"] == 1
        assert backend_order["reserved_micro"] == 600_000

        frontend_product = dept_report["by_phase"]["frontend"]["departments"]["product"]
        assert frontend_product["calls"] == 1
        assert frontend_product["cost_micro"] == 400_000

        assert dept_report["by_phase"][UNASSIGNED_PHASE]["departments"][UNASSIGNED_DEPT]["cost_micro"] == 50_000

        # build_cost_report 保持全部旧字段兼容并附带 by_department
        assert full_report["generation_phase"]["cost_micro"] == 750_000
        assert full_report["generation_phase"]["unknown_calls"] == 1
        assert full_report["by_department"]["totals"]["cost_micro"] == 750_000
    finally:
        engine.dispose()


def test_live_postgres_migration_0017_and_10_way_row_lock_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实连本地/隧道 PostgreSQL：在隔离 Schema 中验证 0017 迁移与真实 10 路 SELECT ... FOR UPDATE 并发争抢。"""
    pg_url = (
        read_local_env_var("AGENTFORGE_TEST_DATABASE_URL")
        or read_local_env_var("AGENTFORGE_DATABASE_URL")
    )
    if not pg_url or not postgres_reachable(pg_url):
        pytest.skip("NOT_RUN: 本地 PostgreSQL 或云端隧道未连接，跳过实库 PG 行锁压测")

    monkeypatch.setenv("AGENTFORGE_PROJECT_BUDGET_MICRO", "3000000")
    ensure_postgres_database(pg_url)
    schema = f"test_a07_{uuid.uuid4().hex[:8]}"
    ensure_postgres_schema(pg_url, schema)
    scoped_url = isolated_schema_url(pg_url, schema)

    try:
        command.upgrade(_alembic_config(scoped_url), "head")
        engine = make_engine(scoped_url)
        factory = make_session_factory(engine)
        try:
            project_id = _seed_project(factory)
            ledger = PgBudgetLedger(factory)
            price = Price(
                version="pg-v1",
                input_micro_per_million=2_000_000,
                output_micro_per_million=4_000_000,
            )

            async def _pg_race() -> list[tuple[str, str]]:
                async def _worker(idx: int) -> tuple[str, str]:
                    call_id = f"pg-race-{idx:02d}"
                    try:
                        await asyncio.to_thread(
                            ledger.reserve_atomic,
                            call_id=call_id,
                            project_id=project_id,
                            run_id=707,
                            model_role="dept_coder",
                            provider="deepseek_official",
                            model="deepseek-chat",
                            reserve_micro=1_000_000,
                            dept_id=f"dept-{idx % 3}",
                            phase="backend",
                            round_num=1,
                            price=price,
                        )
                        return call_id, "OK"
                    except AppException as exc:
                        assert exc.code == ErrorCode.BUDGET_EXCEEDED
                        return call_id, exc.code.value

                return list(await asyncio.gather(*(_worker(i) for i in range(10))))

            results = asyncio.run(_pg_race())
            ok_ids = [cid for cid, st in results if st == "OK"]
            blocked_ids = [cid for cid, st in results if st == ErrorCode.BUDGET_EXCEEDED.value]
            assert len(ok_ids) == 3
            assert len(blocked_ids) == 7

            snap = ledger.balance(project_id)
            assert snap.reserved_micro == 3_000_000
            assert snap.available_micro == 0

            # 并发幂等结算 + 部门归集核对
            usage = Usage(input_tokens=100_000, output_tokens=100_000)  # 600_000
            for cid in ok_ids:
                ledger.settle_idempotent(cid, usage=usage)
                ledger.settle_idempotent(cid, usage=usage)  # 重复结算不双扣

            final_snap = ledger.balance(project_id)
            assert final_snap.settled_micro == 1_800_000
            assert final_snap.reserved_micro == 0

            dept_summary = ledger.cost_by_department(707)
            assert dept_summary["totals"]["settled_calls"] == 3
            assert dept_summary["totals"]["cost_micro"] == 1_800_000
        finally:
            engine.dispose()
    finally:
        drop_postgres_schema(pg_url, schema)
