"""M08A 迁移回退测试：upgrade head → downgrade 逐级 → downgrade base → upgrade head。"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from agentforge_platform.db import make_engine, make_session_factory, session_scope
from agentforge_platform.models import Project

PLATFORM_API_DIR = Path(__file__).resolve().parents[1]
MANAGED_TABLES = {"project", "spec_version", "platform_session"}


def _config(url: str) -> Config:
    config = Config(str(PLATFORM_API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_API_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _tables(url: str) -> set[str]:
    engine = make_engine(url)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _columns(url: str, table: str) -> set[str]:
    engine = make_engine(url)
    try:
        return {column["name"] for column in sa.inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _revision(url: str) -> str | None:
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(sa.text("select version_num from alembic_version")).scalar()
    finally:
        engine.dispose()


def test_migration_upgrade_downgrade_roundtrip(sqlite_url: str) -> None:
    config = _config(sqlite_url)

    # upgrade head：三张表与 0003 新增列存在
    command.upgrade(config, "head")
    assert _revision(sqlite_url) == "0003"
    assert MANAGED_TABLES <= _tables(sqlite_url)
    assert "requirement_text" in _columns(sqlite_url, "project")

    # 写入数据，用于验证回退会删除对应版本新增的数据
    engine = make_engine(sqlite_url)
    with session_scope(make_session_factory(engine)) as session:
        session.add(Project(owner_id=1, name="demo", requirement_text="需求文本"))
    engine.dispose()

    # downgrade 一步（0003 → 0002）：requirement_text 列及其数据被删除，其余表保留
    command.downgrade(config, "0002")
    assert _revision(sqlite_url) == "0002"
    assert "requirement_text" not in _columns(sqlite_url, "project")
    assert {"project", "spec_version", "platform_session"} <= _tables(sqlite_url)

    # downgrade 到 base：受管表全部被删除
    command.downgrade(config, "base")
    assert _revision(sqlite_url) in (None, "")
    assert not (MANAGED_TABLES & _tables(sqlite_url))

    # 再次 upgrade head：表重建，但之前的行已随回退丢失
    command.upgrade(config, "head")
    assert _revision(sqlite_url) == "0003"
    assert MANAGED_TABLES <= _tables(sqlite_url)
    engine = make_engine(sqlite_url)
    with session_scope(make_session_factory(engine)) as session:
        assert session.scalars(sa.select(Project)).all() == []
    engine.dispose()
