"""A02：幂等资产种子同步 CLI（``python -m agentforge_platform.seed_assets``）。

将仓库中已发布的 ``templates/``、``capability-packs/``、``methods/``、``integration-packs/``
一键同步到 PostgreSQL（或 SQLite）的 ``asset_release`` 与 ``asset_file`` 表中。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentforge_platform.asset_store_pg import DEFAULT_SEED_ROOTS, PgAssetStore, SyncResult
from agentforge_platform.db import (
    DEFAULT_DATABASE_URL,
    ensure_postgres_database,
    get_database_url,
    is_postgres,
    make_engine,
    make_session_factory,
    read_local_env_var,
    session_scope,
)
from agentforge_platform.models import Base


def resolve_seed_database_url(explicit_url: str | None = None) -> str:
    """解析资产同步目标数据库连接串（优先命令行参数 -> 环境变量 -> platform-api/.env -> 默认 SQLite）。"""
    if explicit_url and explicit_url.strip():
        return explicit_url.strip()
    url = get_database_url()
    if url == DEFAULT_DATABASE_URL:
        url = read_local_env_var("AGENTFORGE_DATABASE_URL") or url
    return url


def run_seed_assets(
    *,
    database_url: str | None = None,
    roots: list[str | Path] | None = None,
) -> SyncResult:
    """执行一次幂等资产入库同步。"""
    url = resolve_seed_database_url(database_url)
    if is_postgres(url):
        ensure_postgres_database(url)
    engine = make_engine(url)
    try:
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        with session_scope(factory) as session:
            store = PgAssetStore(session)
            return store.sync_from_filesystem(roots=roots)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A02：将 Git 仓库资产（模板/能力包/方法单元/集成包）幂等同步至数据库。"
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="覆盖目标数据库连接串（默认读取 AGENTFORGE_DATABASE_URL 或 platform-api/.env）",
    )
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        default=None,
        help="指定要扫描的资产根目录（可多次指定；默认扫描 templates/capability-packs/methods/integration-packs）",
    )
    args = parser.parse_args(argv)
    target_roots = [Path(r) for r in args.roots] if args.roots else list(DEFAULT_SEED_ROOTS)
    result = run_seed_assets(database_url=args.database_url, roots=target_roots)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
