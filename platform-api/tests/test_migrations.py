"""M08A 迁移回退测试：upgrade head → downgrade 逐级 → downgrade base → upgrade head。"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from agentforge_platform.db import make_engine, make_session_factory, session_scope
from agentforge_platform.models import Project

PLATFORM_API_DIR = Path(__file__).resolve().parents[1]
MANAGED_TABLES = {
    "project", "spec_version", "platform_session",
    "run", "task", "event", "artifact", "worker", "model_call",
}


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
    run_tables = {"run", "task", "event", "artifact"}

    # upgrade head：全部受管表与 0003 新增列存在
    command.upgrade(config, "head")
    assert _revision(sqlite_url) == "0009"
    assert MANAGED_TABLES <= _tables(sqlite_url)
    assert "requirement_text" in _columns(sqlite_url, "project")
    assert {"summary", "missing_fields_json", "questions_json", "draft_revision"} <= _columns(sqlite_url, "spec_version")
    assert "reasoning_effort" in _columns(sqlite_url, "model_call")

    # 写入数据，用于验证回退会删除对应版本新增的数据
    engine = make_engine(sqlite_url)
    with session_scope(make_session_factory(engine)) as session:
        session.add(Project(owner_id=1, name="demo", requirement_text="需求文本"))
    engine.dispose()

    # downgrade 一步（0009 → 0008）：思考强度列被删除
    command.downgrade(config, "0008")
    assert _revision(sqlite_url) == "0008"
    assert "reasoning_effort" not in _columns(sqlite_url, "model_call")

    # downgrade 一步（0008 → 0007）：并发修订和价格快照列被删除
    command.downgrade(config, "0007")
    assert _revision(sqlite_url) == "0007"
    assert "draft_revision" not in _columns(sqlite_url, "spec_version")
    assert "input_micro_per_million" not in _columns(sqlite_url, "model_call")

    # downgrade 一步（0007 → 0006）：提取提示列被删除，账本仍保留
    command.downgrade(config, "0006")
    assert _revision(sqlite_url) == "0006"
    assert "summary" not in _columns(sqlite_url, "spec_version")
    assert "model_call" in _tables(sqlite_url)

    # downgrade（0006 → 0005）：model_call 表被删除
    command.downgrade(config, "0005")
    assert _revision(sqlite_url) == "0005"
    assert "model_call" not in _tables(sqlite_url)
    assert "worker" in _tables(sqlite_url)

    # downgrade（0005 → 0004）：worker 表与 run 租约/幂等列被删除
    command.downgrade(config, "0004")
    assert _revision(sqlite_url) == "0004"
    assert "worker" not in _tables(sqlite_url)
    assert "lease_until" not in _columns(sqlite_url, "run")
    assert run_tables <= _tables(sqlite_url)

    # downgrade（0004 → 0003）：run/task/event/artifact 表被删除，其余保留
    command.downgrade(config, "0003")
    assert _revision(sqlite_url) == "0003"
    assert not (run_tables & _tables(sqlite_url))
    assert "requirement_text" in _columns(sqlite_url, "project")

    # downgrade（0003 → 0002）：requirement_text 列及其数据被删除
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
    assert _revision(sqlite_url) == "0009"
    assert MANAGED_TABLES <= _tables(sqlite_url)
    engine = make_engine(sqlite_url)
    with session_scope(make_session_factory(engine)) as session:
        assert session.scalars(sa.select(Project)).all() == []
    engine.dispose()
