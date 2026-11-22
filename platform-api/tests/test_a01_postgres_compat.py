"""A01：平台元数据库适配本地 PostgreSQL 与双引擎（SQLite / PostgreSQL）兼容测试。"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql
from sqlalchemy.event import contains

from agentforge_platform.db import (
    _apply_sqlite_pragmas,
    drop_postgres_schema,
    ensure_postgres_database,
    ensure_postgres_schema,
    get_database_url,
    is_postgres,
    isolated_schema_url,
    lock_row,
    make_engine,
    make_session_factory,
    read_local_env_var,
    session_scope,
)
from agentforge_platform.models import (
    Base,
    ModelCall,
    Project,
    Run,
    SpecVersion,
    Worker,
)

PLATFORM_API_DIR = Path(__file__).resolve().parents[1]
PG_SAMPLE_URL = "postgresql+psycopg://agentforge:placeholder_only@127.0.0.1:5432/agentforge"


def _alembic_config(url: str) -> Config:
    config = Config(str(PLATFORM_API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_API_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def test_is_postgres_detects_urls_engines_and_sessions(
    sqlite_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert is_postgres(sqlite_url) is False
    assert is_postgres("sqlite+pysqlite:///:memory:") is False
    assert is_postgres("") is False

    assert is_postgres(PG_SAMPLE_URL) is True
    assert is_postgres("postgresql://user:pass@localhost:5432/db") is True
    assert is_postgres("postgres://user:pass@localhost:5432/db") is True

    monkeypatch.setenv("AGENTFORGE_DATABASE_URL", PG_SAMPLE_URL)
    assert get_database_url() == PG_SAMPLE_URL
    assert is_postgres() is True

    monkeypatch.setenv("AGENTFORGE_DATABASE_URL", sqlite_url)
    assert is_postgres() is False

    sqlite_engine = make_engine(sqlite_url)
    pg_engine = make_engine(PG_SAMPLE_URL)
    try:
        assert is_postgres(sqlite_engine) is False
        assert is_postgres(pg_engine) is True
        with session_scope(make_session_factory(sqlite_engine)) as session:
            assert is_postgres(session) is False
    finally:
        sqlite_engine.dispose()
        pg_engine.dispose()


def test_make_engine_separates_sqlite_pragmas_from_postgres_pool(sqlite_url: str) -> None:
    sqlite_engine = make_engine(sqlite_url)
    pg_engine = make_engine(PG_SAMPLE_URL)
    try:
        assert contains(sqlite_engine, "connect", _apply_sqlite_pragmas) is True
        assert contains(pg_engine, "connect", _apply_sqlite_pragmas) is False
        assert getattr(pg_engine.pool, "_pre_ping", False) is True
    finally:
        sqlite_engine.dispose()
        pg_engine.dispose()


def test_lock_row_sqlite_fallback_and_primary_key_resolution(sqlite_url: str) -> None:
    engine = make_engine(sqlite_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            proj = Project(owner_id=1, name="pg-compat", requirement_text="测试双引擎兼容")
            session.add(proj)
            session.flush()
            project_id = proj.id

            worker = Worker(
                worker_id="w-a01",
                hostname="local-host",
                last_heartbeat=datetime.now(UTC),
            )
            session.add(worker)

            call = ModelCall(
                call_id="call-a01",
                model_role="user_interaction",
                provider="deepseek",
                model="deepseek-chat",
                project_id=project_id,
                reserved_micro=1000,
                status="RESERVED",
            )
            session.add(call)

        with session_scope(factory) as session:
            locked_project = lock_row(session, Project, project_id)
            assert locked_project is not None
            assert locked_project.name == "pg-compat"

            locked_worker = lock_row(session, Worker, "w-a01")
            assert locked_worker is not None
            assert locked_worker.hostname == "local-host"

            locked_call = lock_row(session, ModelCall, "call-a01")
            assert locked_call is not None
            assert locked_call.reserved_micro == 1000

            assert lock_row(session, Project, 999999) is None
    finally:
        engine.dispose()


def test_lock_row_emits_for_update_on_postgresql_session() -> None:
    captured_statements: list[str] = []

    class _FakeScalarResult:
        def first(self) -> Any:
            return None

    class _FakePgBind:
        dialect = postgresql.dialect()

    class _FakePgSession(sa.orm.Session):
        def get_bind(self, **_kwargs: Any) -> Any:  # type: ignore[override]
            return _FakePgBind()

        def scalars(self, statement: Any, *_args: Any, **_kwargs: Any) -> Any:  # type: ignore[override]
            compiled = str(
                statement.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
            captured_statements.append(compiled)
            return _FakeScalarResult()

    fake_session = _FakePgSession()
    assert is_postgres(fake_session) is True
    assert lock_row(fake_session, Run, 42) is None
    assert len(captured_statements) == 1
    assert "FOR UPDATE" in captured_statements[0]
    assert "WHERE run.id = 42" in captured_statements[0]


def test_all_15_migrations_compile_cleanly_for_postgresql_dialect() -> None:
    config = _alembic_config(PG_SAMPLE_URL)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)
    sql_output = buffer.getvalue()

    assert "CREATE TABLE project" in sql_output
    assert "CREATE TABLE spec_version" in sql_output
    assert "CREATE TABLE platform_session" in sql_output
    assert "CREATE TABLE run" in sql_output
    assert "CREATE TABLE model_call" in sql_output
    assert "CREATE TABLE budget_adjustment" in sql_output
    assert "BOOLEAN DEFAULT false NOT NULL" in sql_output
    assert "PRAGMA" not in sql_output.upper()


def test_read_local_env_var_and_database_name_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTFORGE_TEST_PG_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        'AGENTFORGE_TEST_PG_URL="postgresql+psycopg://postgres:<fill_password>@127.0.0.1:15432/agentforge_test"\n',
        encoding="utf-8",
    )
    # 含 <...> 占位符时视为未配置，防止误连失败
    assert read_local_env_var("AGENTFORGE_TEST_PG_URL", env_file=env_file) is None

    env_file.write_text(
        'AGENTFORGE_TEST_PG_URL="postgresql+psycopg://postgres:demo_placeholder@127.0.0.1:15432/agentforge_test"\n',
        encoding="utf-8",
    )
    assert (
        read_local_env_var("AGENTFORGE_TEST_PG_URL", env_file=env_file)
        == "postgresql+psycopg://postgres:demo_placeholder@127.0.0.1:15432/agentforge_test"
    )

    assert ensure_postgres_database("sqlite+pysqlite:///:memory:") is False
    with pytest.raises(ValueError, match="非法 PostgreSQL 数据库名"):
        ensure_postgres_database("postgresql+psycopg://postgres:x@127.0.0.1:15432/bad-name;drop")

    isolated = isolated_schema_url(PG_SAMPLE_URL, "agentforge_test_a01")
    assert "options=-csearch_path=agentforge_test_a01" in isolated
    assert isolated_schema_url("sqlite+pysqlite:///:memory:", "agentforge_test_a01") == "sqlite+pysqlite:///:memory:"
    with pytest.raises(ValueError, match="禁止删除非测试 Schema"):
        drop_postgres_schema(PG_SAMPLE_URL, "public")


def test_live_postgres_migrations_crud_and_isolation() -> None:
    pg_url = read_local_env_var("AGENTFORGE_TEST_PG_URL") or ""
    if not pg_url:
        pytest.skip("NOT_RUN: 未配置 AGENTFORGE_TEST_PG_URL 本地/隧道 PostgreSQL 测试环境")

    assert is_postgres(pg_url) is True
    # 1. 若云服务器 15432 上尚未创建测试库（如 agentforge_test），自动连接默认维护库创建
    ensure_postgres_database(pg_url)
    # 2. 使用独立测试 Schema 隔离（即使与正式库共享同一个 dbname，也不会触碰 public 表）
    test_schema = "agentforge_test_a01"
    ensure_postgres_schema(pg_url, test_schema)
    scoped_url = isolated_schema_url(pg_url, test_schema)
    config = _alembic_config(scoped_url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    engine = make_engine(scoped_url)
    factory = make_session_factory(engine)
    try:
        tables = set(sa.inspect(engine).get_table_names())
        assert {
            "project",
            "spec_version",
            "platform_session",
            "run",
            "task",
            "event",
            "artifact",
            "worker",
            "budget_adjustment",
            "model_call",
        } <= tables

        with session_scope(factory) as session:
            proj = Project(owner_id=7, name="live-pg", requirement_text="真实 PG 迁移与 CRUD")
            session.add(proj)
            session.flush()
            spec = SpecVersion(
                project_id=proj.id,
                version=1,
                draft_revision=1,
                body_json="{}",
                hash="h" * 64,
            )
            session.add(spec)
            session.flush()
            run = Run(project_id=proj.id, spec_id=spec.id, status="QUEUED")
            session.add(run)
            session.flush()
            run_id = run.id
            project_id = proj.id

        with session_scope(factory) as session:
            locked = lock_row(session, Run, run_id)
            assert locked is not None
            assert locked.status == "QUEUED"
            locked.status = "RUNNING"

        # 验证异常回滚与事务隔离
        with pytest.raises(RuntimeError), session_scope(factory) as session:
            locked = lock_row(session, Project, project_id)
            assert locked is not None
            locked.name = "rolled-back-name"
            raise RuntimeError("trigger rollback")

        with session_scope(factory) as session:
            persisted = lock_row(session, Project, project_id)
            assert persisted is not None
            assert persisted.name == "live-pg"
    finally:
        engine.dispose()
        drop_postgres_schema(pg_url, test_schema)
