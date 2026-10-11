"""M03 数据层测试：迁移、唯一约束、短事务与持久化。"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from agentforge_platform.db import make_engine, make_session_factory, session_scope
from agentforge_platform.models import Project, SpecVersion


def test_migration_creates_schema_on_empty_sqlite(migrated_sqlite_url: str) -> None:
    engine = make_engine(migrated_sqlite_url)
    try:
        inspector = sa.inspect(engine)
        assert {"project", "spec_version"} <= set(inspector.get_table_names())

        unique_constraints = inspector.get_unique_constraints("spec_version")
        assert any(
            set(uc["column_names"]) == {"project_id", "version"} for uc in unique_constraints
        )
    finally:
        engine.dispose()


def test_sqlite_wal_mode_is_enabled(sqlite_url: str) -> None:
    engine = make_engine(sqlite_url)
    try:
        with engine.connect() as connection:
            mode = connection.execute(sa.text("PRAGMA journal_mode")).scalar()
        assert str(mode).lower() == "wal"
    finally:
        engine.dispose()


def test_spec_version_project_and_version_are_unique(migrated_sqlite_url: str) -> None:
    engine = make_engine(migrated_sqlite_url)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            project = Project(owner_id=1, name="demo")
            session.add(project)
            session.flush()
            session.add(SpecVersion(project_id=project.id, version=1, body_json="{}", hash="h1"))

        with pytest.raises(IntegrityError), session_scope(factory) as session:
            session.add(
                SpecVersion(
                    project_id=project.id, version=1, body_json="{}", hash="h2"
                )
            )
    finally:
        engine.dispose()


def test_data_persists_across_engine_restart(migrated_sqlite_url: str) -> None:
    first_engine = make_engine(migrated_sqlite_url)
    with session_scope(make_session_factory(first_engine)) as session:
        session.add(Project(owner_id=7, name="persisted"))
    first_engine.dispose()

    second_engine = make_engine(migrated_sqlite_url)
    try:
        with session_scope(make_session_factory(second_engine)) as session:
            names = session.scalars(sa.select(Project.name)).all()
        assert names == ["persisted"]
    finally:
        second_engine.dispose()


def test_session_scope_rolls_back_on_error(migrated_sqlite_url: str) -> None:
    engine = make_engine(migrated_sqlite_url)
    factory = make_session_factory(engine)
    try:
        with pytest.raises(RuntimeError), session_scope(factory) as session:
            session.add(Project(owner_id=9, name="rolled-back"))
            raise RuntimeError("boom")

        with session_scope(factory) as session:
            assert session.scalars(sa.select(Project.name)).all() == []
    finally:
        engine.dispose()
