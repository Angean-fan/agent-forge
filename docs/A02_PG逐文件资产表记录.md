### 实施记录：A02｜本地 PG 逐文件资产表（`asset_release` + `asset_file`）与 `seed_assets` 同步器

- 基准 Commit / 结束 Commit：`5823717`（本步独立提交见 git log `feat(a02): ...`）
- 对应文档与步骤编号：《AgentForge_02_现有底座改造与适配_分步开发Prompt.md》`A02`
- 当前状态：**PASS**（已在 SQLite 及本机 `127.0.0.1:15432` 云端 PostgreSQL 上完成迁移 `0016`、GIN `tsvector` 索引创建、28 个资产版本与 444 个源码文件入库、幂等重跑与全文检索实测，0 `NOT_RUN`）
- 接口选用（复用已有接口 / 本步新增接口）：
  - **复用**：
    - `asset_registry.py`：`DEFAULT_ROOTS`、`DEFAULT_INTEGRATION_ROOT`、`AssetManifest`、`MethodUnitManifest`、`IntegrationPackManifest`、`compute_checksum()`、`is_safe_owned_path()`
    - `method_index.py`：`DEFAULT_METHODS_ROOT`、`scan_methods()`
    - `db.py`：`is_postgres()`、`ensure_postgres_database()`、`read_local_env_var()`、`isolated_schema_url()`
  - **本步新增迁移、模型与服务**：
    - `platform-api/migrations/versions/0016_asset_release_and_file.py`：创建 `asset_release`（唯一约束 `(kind, asset_id, version)`）与 `asset_file`（外键级联删除、唯一约束 `(release_id, rel_path)`），并在 PostgreSQL 方言下创建 `ix_asset_release_search_fts` 与 `ix_asset_file_search_fts` 两项 `to_tsvector('simple', search_text)` GIN 全文索引。
    - `platform-api/src/agentforge_platform/models.py`：新增 `AssetRelease`、`AssetFile` ORM 模型。
    - `platform-api/src/agentforge_platform/asset_store_pg.py`：新增 `PgAssetStore`（`sync_from_filesystem()`、`get_release()`、`list_releases()`、`search_releases()`、`get_file()`、`list_files()`、`search_files()`）、`extract_symbol_summary()`、`SyncResult`、`SkippedFile`。
    - `platform-api/src/agentforge_platform/seed_assets.py`：新增 CLI 入口 `python -m agentforge_platform.seed_assets`（及 `run_seed_assets()`、`resolve_seed_database_url()`）。
- 实际新增或修改文件：
  - 新增 `platform-api/migrations/versions/0016_asset_release_and_file.py`
  - 修改 `platform-api/src/agentforge_platform/models.py`
  - 新增 `platform-api/src/agentforge_platform/asset_store_pg.py`
  - 新增 `platform-api/src/agentforge_platform/seed_assets.py`
  - 修改 `platform-api/tests/test_migrations.py`
  - 新增 `platform-api/tests/test_a02_pg_asset_store.py`
  - 新增 `docs/A02_PG逐文件资产表记录.md`
- 兼容性说明（旧 SQLite 测试 / 旧 asset-lock / 原主生成图开关关闭回归）：
  - 未修改 Git 仓库中任何已发布 `templates/`、`capability-packs/`、`methods/`、`integration-packs/` 源文件与 7 份 `schemas/asset-lock*.sample.json`；
  - `AssetRelease.checksum` 直接使用 `asset_registry.compute_checksum(asset_dir)` 计算并做 manifest 一致性校验，与全部 7 份旧 `asset-lock` 样例 100% 字节级一致；
  - 检索层在 PostgreSQL 下走 `to_tsvector('simple', search_text) @@ plainto_tsquery('simple', ...)` + `ILIKE` 兜底（兼容无第三方中文分词插件的标准云端 PG），在 SQLite 下自动退化为 `LIKE` 查询，既有 SQLite 测试 100% 零回归。
- 执行命令与真实测试结果（PASS / FAIL / NOT_RUN）：
  - `cd platform-api && uv run --locked pytest tests/test_migrations.py tests/test_a02_pg_asset_store.py -v` → **7 passed**（含真实 `127.0.0.1:15432` 云端 PostgreSQL 独立 Schema 下 `0016` 升级/GIN 索引核验/28 版本 444 文件入库/幂等复跑/全文检索/`0016 -> 0015` 回滚实测 **PASS**）
  - `cd platform-api && uv run --locked alembic upgrade head && uv run --locked python -m agentforge_platform.seed_assets` → **PASS**（首次入库 `inserted_releases=28, inserted_files=444`；再次执行 `inserted_releases=0, unchanged_releases=28, inserted_files=0`）
  - `./scripts/ci_gate.sh`：
    - `platform-api`：`uv run --locked pytest` → **785 passed**；`uv run --locked ruff check .` → **All checks passed!**
    - `platform-ui`：`npm ci && npm run type-check && npm run build && npm test` → **41 passed**
    - `runner`：`uv run --locked pytest && uv run --locked ruff check .` → **31 passed, 1 skipped**，ruff clean
- WhenDone 逐条判定与证据位置：
  1. 迁移 `0016` 在 SQLite 与本地/隧道 PG 均可干净升级与回滚：**PASS**（见 `test_migrations.py` 与 `test_live_postgres_migration_0016_seed_and_fts_search`）。
  2. 运行 `uv run python -m agentforge_platform.seed_assets` 一次性将仓库全部 5 模板、4 族能力包（17 版本）、3 方法单元、1 集成包（3 版本）共 28 个版本 444 个文件完整同步入库，且每条 `checksum` 与真值及 7 份 `asset-lock` 100% 一致：**PASS**（见 `test_seed_assets_full_repository_checksums_and_idempotency` 及真实 PG 入库输出）。
  3. 重复执行 `seed_assets` 为 0 变更（幂等）；篡改内容同版本号重导入被拒：**PASS**（见 `test_immutable_version_protection_rejects_tampered_reimport`）。
  4. 二进制文件、超大文件（> 2 MiB）与符号链接均按安全策略跳过/拦截并记录原因：**PASS**（见 `test_security_filters_reject_symlinks_binary_and_oversize_files`）。
  5. 新增 `tests/test_a02_pg_asset_store.py` 全绿、`./scripts/ci_gate.sh` 全绿：**PASS**。
- 遗留风险与下一步依赖：
  - 真实 PostgreSQL（`127.0.0.1:15432/agentforge`）中已就绪完整 28 个资产版本与 444 个源码文件；
  - 下一步进入 `A03`（本地只读物化缓存 `AssetMaterializer` 与面向子 Agent 的资产检索/读取工具 `agent_asset_tools.py`）。
