"""测试夹具：临时 SQLite 文件上的真实 Alembic 迁移。"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

PLATFORM_API_DIR = Path(__file__).resolve().parents[1]


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
