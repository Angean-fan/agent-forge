"""测试夹具：临时 SQLite 文件上的真实 Alembic 迁移。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from agentforge_platform.accounts import get_accounts
from agentforge_platform.db import get_db, make_engine, make_session_factory, session_scope
from agentforge_platform.main import create_app
from agentforge_platform.passwords import hash_password

PLATFORM_API_DIR = Path(__file__).resolve().parents[1]

ALICE = {"id": 1, "username": "alice", "password": "alice-secret"}
BOB = {"id": 2, "username": "bob", "password": "bob-secret"}


def apply_migrations(url: str) -> None:
    config = Config(str(PLATFORM_API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_API_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'agentforge-test.db'}"


@pytest.fixture
def migrated_sqlite_url(sqlite_url: str) -> str:
    apply_migrations(sqlite_url)
    return sqlite_url


@pytest.fixture
def accounts_file(tmp_path: Path) -> Path:
    payload = {
        "accounts": [
            {
                "id": ALICE["id"],
                "username": ALICE["username"],
                "password_hash": hash_password(ALICE["password"]),
            },
            {
                "id": BOB["id"],
                "username": BOB["username"],
                "password_hash": hash_password(BOB["password"]),
            },
        ]
    }
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def platform_app(
    migrated_sqlite_url: str, accounts_file: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[FastAPI, object]]:
    monkeypatch.setenv("AGENTFORGE_ACCOUNTS_FILE", str(accounts_file))
    monkeypatch.delenv("AGENTFORGE_COOKIE_SECURE", raising=False)
    get_accounts.cache_clear()

    app = create_app()
    engine = make_engine(migrated_sqlite_url)
    factory = make_session_factory(engine)

    def _override_db() -> Iterator[object]:
        with session_scope(factory) as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    try:
        yield app, factory
    finally:
        engine.dispose()
        get_accounts.cache_clear()
