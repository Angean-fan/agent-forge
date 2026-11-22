"""A02：PostgreSQL 逐文件资产表（asset_release + asset_file）与 seed_assets 同步器测试。"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from agentforge_platform.asset_registry import (
    ALL_ROOTS,
    REPO_ROOT,
    AssetRegistry,
    compute_checksum,
)
from agentforge_platform.asset_store_pg import (
    PgAssetStore,
    extract_symbol_summary,
)
from agentforge_platform.db import (
    drop_postgres_schema,
    ensure_postgres_database,
    ensure_postgres_schema,
    is_postgres,
    isolated_schema_url,
    make_engine,
    make_session_factory,
    read_local_env_var,
    session_scope,
)
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.method_index import DEFAULT_METHODS_ROOT, scan_methods
from agentforge_platform.models import Base
from agentforge_platform.seed_assets import main as seed_main
from agentforge_platform.seed_assets import run_seed_assets

PLATFORM_API_DIR = Path(__file__).resolve().parents[1]
PG_SAMPLE_URL = "postgresql+psycopg://angean:placeholder@127.0.0.1:15432/agentforge"


def _alembic_config(url: str) -> Config:
    config = Config(str(PLATFORM_API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_API_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def test_migration_0016_compiles_gin_tsvector_indexes_for_postgresql() -> None:
    config = _alembic_config(PG_SAMPLE_URL)
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(config, "0015:0016", sql=True)
    sql = buf.getvalue()
    assert "CREATE TABLE asset_release" in sql
    assert "CREATE TABLE asset_file" in sql
    assert "ix_asset_release_search_fts" in sql
    assert "ix_asset_file_search_fts" in sql
    assert "USING gin (to_tsvector('simple', search_text))" in sql


def test_seed_assets_full_repository_checksums_and_idempotency(sqlite_url: str) -> None:
    # 1. 首次同步全仓库 5 模板、4 族能力包（17 版本）、3 方法单元、1 集成包（3 版本）= 28 个版本
    first = run_seed_assets(database_url=sqlite_url)
    assert first.inserted_releases == 28
    assert first.unchanged_releases == 0
    assert first.inserted_files == 444
    assert first.skipped_files == []

    # 2. 重复执行 seed_assets 必须 0 变更（严格幂等）
    second = run_seed_assets(database_url=sqlite_url)
    assert second.inserted_releases == 0
    assert second.unchanged_releases == 28
    assert second.inserted_files == 0

    # 3. 核对入库 checksum 与文件系统真值及全部 7 份旧 asset-lock.*.sample.json 100% 一致
    engine = make_engine(sqlite_url)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            store = PgAssetStore(session)
            releases = store.list_releases()
            assert len(releases) == 28
            assert len(store.list_releases(kind="template")) == 5
            assert len(store.list_releases(kind="capability-pack")) == 17
            assert len(store.list_releases(kind="method-unit")) == 3
            assert len(store.list_releases(kind="integration-pack")) == 3

            reg = AssetRegistry(list(ALL_ROOTS))
            for manifest, directory in reg.all_with_paths():
                rel = store.get_release(manifest.kind, manifest.id, manifest.version)
                assert rel is not None
                assert rel.checksum == compute_checksum(directory)
                assert rel.checksum == manifest.checksum

            for method in scan_methods(DEFAULT_METHODS_ROOT):
                rel = store.get_release("method-unit", method.id, method.version)
                assert rel is not None
                assert rel.checksum == method.checksum

            for lock_path in sorted((REPO_ROOT / "schemas").glob("asset-lock*.sample.json")):
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
                for item in payload.get("assets", []):
                    rel = store.get_release(item["kind"], item["id"], item["version"])
                    assert rel is not None, f"{lock_path.name} missing {item}"
                    assert rel.checksum == item["checksum"]
                for item in payload.get("integration_packs", []):
                    rel = store.get_release("integration-pack", item["id"], item["version"])
                    assert rel is not None
                    assert rel.checksum == item["checksum"]
    finally:
        engine.dispose()


def test_immutable_version_protection_rejects_tampered_reimport(
    sqlite_url: str, tmp_path: Path
) -> None:
    asset_dir = tmp_path / "methods" / "demo-unit" / "1.0.0"
    src_dir = asset_dir / "src"
    src_dir.mkdir(parents=True)
    code_file = src_dir / "DemoHelper.java"
    code_file.write_text(
        "package com.example;\npublic class DemoHelper { public String ping() { return \"ok\"; } }\n",
        encoding="utf-8",
    )

    def _write_manifest() -> None:
        manifest = {
            "id": "demo-unit",
            "version": "1.0.0",
            "kind": "method-unit",
            "symbol": "com.example.DemoHelper.ping",
            "signature": "String ping()",
            "language": "java",
            "framework": "spring-boot",
            "layer": "util",
            "description": "演示方法单元",
            "compatible_profiles": ["demo-ts-mp"],
            "requires": [],
            "provides": ["util:demo-ping"],
            "conflicts": [],
            "parameter_schema": {},
            "extension_points": [],
            "owned_paths": ["src/DemoHelper.java"],
            "checks": [],
            "target_dir": "backend",
            "tags": ["demo", "ping"],
            "acceptance_ids": ["AC-1"],
            "source_provenance": "unit-test",
            "license": "Apache-2.0",
            "checksum": compute_checksum(asset_dir),
        }
        (asset_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    _write_manifest()
    first = run_seed_assets(database_url=sqlite_url, roots=[tmp_path / "methods"])
    assert first.inserted_releases == 1

    # 篡改源码并重算 manifest.checksum，企图以相同版本号 1.0.0 重新导入 → 必须触发不可变版本保护拒绝
    code_file.write_text(
        "package com.example;\npublic class DemoHelper { public String ping() { return \"tampered\"; } }\n",
        encoding="utf-8",
    )
    _write_manifest()

    with pytest.raises(AppException) as exc_info:
        run_seed_assets(database_url=sqlite_url, roots=[tmp_path / "methods"])
    assert exc_info.value.code == ErrorCode.ASSET_CONFLICT
    assert "不可变版本保护" in exc_info.value.message


def test_security_filters_reject_symlinks_binary_and_oversize_files(
    sqlite_url: str, tmp_path: Path
) -> None:
    outside_secret = tmp_path / "host_secret.txt"
    outside_secret.write_text("TOP_SECRET_DO_NOT_IMPORT", encoding="utf-8")

    pack_dir = tmp_path / " packs" / "sec-pack" / "0.1.0"
    tpl_dir = pack_dir / "templates"
    tpl_dir.mkdir(parents=True)

    normal_file = tpl_dir / "SafeService.java"
    normal_file.write_text(
        "package com.demo;\npublic class SafeService { public void executeOrder() {} }\n",
        encoding="utf-8",
    )
    binary_file = tpl_dir / "logo.bin"
    binary_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

    oversize_file = tpl_dir / "huge.txt"
    oversize_file.write_text("A" * 2048, encoding="utf-8")

    symlink_file = tpl_dir / "leak.txt"
    symlink_file.symlink_to(outside_secret)

    manifest = {
        "id": "sec-pack",
        "version": "0.1.0",
        "kind": "capability-pack",
        "compatible_profiles": ["demo-ts-mp"],
        "requires": [],
        "provides": ["sec:demo"],
        "conflicts": [],
        "parameter_schema": {},
        "extension_points": [],
        "owned_paths": ["templates/SafeService.java"],
        "checks": [],
        "license": "Apache-2.0",
        "checksum": compute_checksum(pack_dir),
    }
    (pack_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    engine = make_engine(sqlite_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            store = PgAssetStore(session)
            res = store.sync_from_filesystem([tmp_path / " packs"], max_file_bytes=1024)
            assert res.inserted_releases == 1
            assert res.inserted_files == 1
            reasons = {item.rel_path: item.reason for item in res.skipped_files}
            assert reasons["templates/leak.txt"] == "symlink_rejected"
            assert reasons["templates/logo.bin"] == "binary_file"
            assert reasons["templates/huge.txt"] == "oversize_gt_1024_bytes"

            rel = store.get_release("capability-pack", "sec-pack", "0.1.0")
            assert rel is not None
            files = store.list_files(rel)
            assert [f.rel_path for f in files] == ["templates/SafeService.java"]
            assert "SafeService" in files[0].symbol_summary
            assert "executeOrder" in files[0].symbol_summary
    finally:
        engine.dispose()


def test_search_releases_and_files_and_cli_entry(
    sqlite_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "TaskView" in extract_symbol_summary(
        "src/views/TaskView.vue",
        "export const fetchTasks = async () => {};\nexport interface TaskItem { id: number }",
    )

    rc = seed_main(["--database-url", sqlite_url])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total_releases"] == 28

    engine = make_engine(sqlite_url)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            store = PgAssetStore(session)
            weather_hits = store.search_releases("weather-lookup", kind="integration-pack")
            assert len(weather_hits) == 3

            ts_mp_templates = store.search_releases(
                "base-template", kind="template", profile="demo-ts-mp"
            )
            assert {r.version for r in ts_mp_templates} == {"demo-0.1", "demo-0.2"}

            method_files = store.search_files("DateNormalizer", kind="method-unit")
            assert len(method_files) >= 1
            first_file = store.get_file(method_files[0].release_id, method_files[0].rel_path)
            assert first_file is not None
            assert first_file.sha256 == method_files[0].sha256
    finally:
        engine.dispose()


def test_live_postgres_migration_0016_seed_and_fts_search() -> None:
    pg_url = read_local_env_var("AGENTFORGE_TEST_PG_URL") or ""
    if not pg_url:
        pytest.skip("NOT_RUN: 未配置 AGENTFORGE_TEST_PG_URL 本地/隧道 PostgreSQL 测试环境")

    assert is_postgres(pg_url) is True
    ensure_postgres_database(pg_url)
    test_schema = "agentforge_test_a02"
    ensure_postgres_schema(pg_url, test_schema)
    scoped_url = isolated_schema_url(pg_url, test_schema)
    config = _alembic_config(scoped_url)

    engine = make_engine(scoped_url)
    factory = make_session_factory(engine)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")

        # 验证 PG GIN tsvector 索引已创建
        with engine.connect() as conn:
            idx_names = {
                row[0]
                for row in conn.execute(
                    sa.text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = :schema AND tablename IN ('asset_release', 'asset_file')"
                    ),
                    {"schema": test_schema},
                )
            }
        assert "ix_asset_release_search_fts" in idx_names
        assert "ix_asset_file_search_fts" in idx_names

        # 全量 seed_assets 入真实 PG + 幂等复跑 + to_tsvector('simple', ...) 全文检索
        first = run_seed_assets(database_url=scoped_url)
        assert first.inserted_releases == 28
        assert first.inserted_files == 444

        second = run_seed_assets(database_url=scoped_url)
        assert second.inserted_releases == 0
        assert second.unchanged_releases == 28

        with session_scope(factory) as session:
            store = PgAssetStore(session)
            hits_en = store.search_releases("validator", kind="method-unit")
            assert any(r.asset_id == "field-validator" for r in hits_en)

            file_hits = store.search_files("StatusGuard", kind="method-unit")
            assert len(file_hits) >= 1

        # 验证 0016 -> 0015 可干净回滚
        command.downgrade(config, "0015")
        tables_after = set(sa.inspect(engine).get_table_names())
        assert "asset_release" not in tables_after
        assert "asset_file" not in tables_after
    finally:
        engine.dispose()
        drop_postgres_schema(pg_url, test_schema)
