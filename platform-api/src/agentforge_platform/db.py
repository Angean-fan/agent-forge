"""SQLite 引擎、事务会话与 PRAGMA 配置。

- 平台元数据使用 SQLite 并开启 WAL，短事务；
- 生成项目使用独立 MySQL，与这里的引擎完全无关，不得复用。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DATABASE_URL = "sqlite+pysqlite:///./agentforge.db"


def get_database_url() -> str:
    """平台数据库连接串；可用 ``AGENTFORGE_DATABASE_URL`` 覆盖。"""
    return os.environ.get("AGENTFORGE_DATABASE_URL", DEFAULT_DATABASE_URL)


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
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, future=True, connect_args=connect_args)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


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
