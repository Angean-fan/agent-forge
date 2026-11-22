"""Alembic 迁移环境。

连接串优先取 alembic.ini/调用方显式设置的值，否则回落到
``agentforge_platform.db.get_database_url()``（读取 ``AGENTFORGE_DATABASE_URL``）。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from agentforge_platform.db import (
    DEFAULT_DATABASE_URL,
    ensure_postgres_database,
    get_database_url,
    is_postgres,
    read_local_env_var,
)
from agentforge_platform.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

if not config.get_main_option("sqlalchemy.url"):
    configured_url = get_database_url()
    if configured_url == DEFAULT_DATABASE_URL:
        configured_url = read_local_env_var("AGENTFORGE_DATABASE_URL") or configured_url
    config.set_main_option("sqlalchemy.url", configured_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=not is_postgres(url),
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url") or ""
    if is_postgres(url):
        ensure_postgres_database(url)
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=not is_postgres(connection),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
