"""平台元数据库引擎、事务会话与双引擎（SQLite / PostgreSQL）兼容层。

- 默认兼容 SQLite（开启 WAL，供既有单测与轻量运行）；
- 支持通过 ``AGENTFORGE_DATABASE_URL=postgresql+psycopg://...`` 切换至本地 PostgreSQL；
- 生成项目使用独立 MySQL，与这里的平台元数据引擎完全无关，不得复用。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DATABASE_URL = "sqlite+pysqlite:///./agentforge.db"
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
_SAFE_DB_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def read_local_env_var(name: str, env_file: Path | None = None) -> str | None:
    """从环境变量或本地未入库的 ``platform-api/.env`` 读取配置项（忽略未替换的 ``<...>`` 占位符）。"""
    val = os.environ.get(name, "").strip()
    if val and "<" not in val and ">" not in val:
        return val
    path = _ENV_FILE if env_file is None else env_file
    if not path.is_file():
        return None
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() != name:
            continue
        cleaned = v.strip().strip("\"'").strip()
        if cleaned and "<" not in cleaned and ">" not in cleaned:
            return cleaned
    return None


def get_database_url() -> str:
    """平台数据库连接串；可用 ``AGENTFORGE_DATABASE_URL`` 覆盖。"""
    return os.environ.get("AGENTFORGE_DATABASE_URL") or DEFAULT_DATABASE_URL


def ensure_postgres_database(url: str, *, admin_db: str = "postgres") -> bool:
    """若目标 PostgreSQL 数据库尚未创建，自动连接同实例维护库（默认 ``postgres``）创建它。

    优先尝试直连目标库（兼容无 ``postgres`` 维护库权限但目标库已存在的云端账号）；
    仅在目标库不存在（SQLSTATE ``3D000``）时连接维护库执行 ``CREATE DATABASE``。
    返回 ``True`` 表示本次新建了数据库，``False`` 表示目标数据库已存在。
    """
    if not is_postgres(url):
        return False
    parsed = make_url(url)
    target_db = (parsed.database or "").strip()
    if not target_db or not _SAFE_DB_IDENT.match(target_db):
        raise ValueError(f"非法 PostgreSQL 数据库名：{target_db!r}")

    probe_engine = create_engine(
        parsed,
        future=True,
        connect_args={"connect_timeout": 5},
    )
    try:
        with probe_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            return False
    except OperationalError as exc:
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None)
        msg = str(orig or exc).lower()
        if sqlstate != "3D000" and "does not exist" not in msg:
            raise
    finally:
        probe_engine.dispose()

    admin_url = parsed.set(database=admin_db)
    admin_engine = create_engine(
        admin_url,
        future=True,
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 5},
    )
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target_db},
            ).scalar()
            if exists:
                return False
            conn.execute(text(f'CREATE DATABASE "{target_db}"'))
            return True
    finally:
        admin_engine.dispose()


def isolated_schema_url(url: str, schema: str) -> str:
    """为 PostgreSQL 连接串附加 ``search_path`` 隔离 Schema，防止测试回滚误删 ``public`` 正式表。"""
    if not _SAFE_DB_IDENT.match(schema):
        raise ValueError(f"非法 PostgreSQL Schema 名：{schema!r}")
    if not is_postgres(url):
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}options=-csearch_path={schema}"


def ensure_postgres_schema(url: str, schema: str) -> None:
    """在目标 PostgreSQL 库中幂等创建隔离测试 Schema。"""
    if not _SAFE_DB_IDENT.match(schema):
        raise ValueError(f"非法 PostgreSQL Schema 名：{schema!r}")
    if not is_postgres(url):
        return
    eng = make_engine(url)
    try:
        with eng.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    finally:
        eng.dispose()


def drop_postgres_schema(url: str, schema: str) -> None:
    """清理隔离测试 Schema（绝不允许删除 ``public``）。"""
    if not _SAFE_DB_IDENT.match(schema) or schema.lower() == "public":
        raise ValueError(f"禁止删除非测试 Schema：{schema!r}")
    if not is_postgres(url):
        return
    eng = make_engine(url)
    try:
        with eng.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        eng.dispose()


def is_postgres(engine_or_url: Engine | Connection | Session | str | None = None) -> bool:
    """判定给定连接串、Engine 或 Session 是否为 PostgreSQL 方言。"""
    target = get_database_url() if engine_or_url is None else engine_or_url
    if isinstance(target, str):
        raw = target.strip()
        if not raw:
            return False
        try:
            backend = make_url(raw).get_backend_name()
            return backend in {"postgresql", "postgres"}
        except (ArgumentError, ValueError):
            lowered = raw.lower()
            return lowered.startswith(("postgresql://", "postgresql+", "postgres://", "postgres+"))
    if isinstance(target, Session):
        bind = target.get_bind()
        return getattr(bind.dialect, "name", "") == "postgresql"
    dialect = getattr(target, "dialect", None)
    return getattr(dialect, "name", "") == "postgresql"


def _is_sqlite_url(url: str) -> bool:
    try:
        return make_url(url).get_backend_name() == "sqlite"
    except (ArgumentError, ValueError):
        return url.strip().lower().startswith("sqlite")


def _apply_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def make_engine(url: str) -> Engine:
    if is_postgres(url):
        return create_engine(
            url,
            future=True,
            pool_pre_ping=True,
            pool_recycle=300,
            connect_args={"connect_timeout": 5},
        )
    sqlite = _is_sqlite_url(url)
    connect_args = {"check_same_thread": False} if sqlite else {}
    engine = create_engine(url, future=True, connect_args=connect_args)
    if sqlite:
        event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


def lock_row[T](db: Session, model: type[T], row_id: Any) -> T | None:
    """按主键查询单行并加锁：PostgreSQL 下启用 ``SELECT ... FOR UPDATE``，SQLite 下退化为短事务普通查询。"""
    mapper = inspect(model)
    pk_col = mapper.primary_key[0]
    stmt = select(model).where(pk_col == row_id)
    if is_postgres(db):
        stmt = stmt.with_for_update()
    return db.scalars(stmt).first()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """短事务上下文：成功提交，异常回滚，最后关闭。"""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


engine = make_engine(get_database_url())
SessionLocal = make_session_factory(engine)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个短事务会话，成功提交、异常回滚。"""
    with session_scope(SessionLocal) as session:
        yield session
