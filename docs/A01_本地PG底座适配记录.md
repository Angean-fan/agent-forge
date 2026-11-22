### 实施记录：A01｜平台元数据库适配本地 PostgreSQL 与双引擎兼容

- 基准 Commit / 结束 Commit：`bc19afb`（本步独立提交见 git log `feat(a01): ...`）
- 对应文档与步骤编号：《AgentForge_02_现有底座改造与适配_分步开发Prompt.md》`A01`
- 当前状态：**PASS**（已通过本机 `127.0.0.1:15432` 隧道在真实云端 PostgreSQL 实例上完成自动建库、`0001..0015` 全量迁移、核心表 CRUD、`SELECT ... FOR UPDATE` 行锁与事务回滚隔离实测，0 `NOT_RUN`）
- 接口选用（复用已有接口 / 本步新增接口）：
  - **复用并扩展**：
    - `platform-api/src/agentforge_platform/db.py`：`get_database_url()`、`make_engine()`、`make_session_factory()`、`session_scope()`、`get_db()`
    - `platform-api/migrations/env.py`：`run_migrations_offline()`、`run_migrations_online()`
    - `platform-api/pyproject.toml` & `platform-api/uv.lock`：新增锁定 `psycopg[binary]>=3.2.0`（解析为 `psycopg 3.3.9` / `psycopg-binary 3.3.9`）
  - **本步新增接口**：
    - `db.is_postgres(engine_or_url=None) -> bool`：支持对 URL 字符串、`Engine`、`Connection`、`Session` 及默认环境变量 `AGENTFORGE_DATABASE_URL` 判定是否为 PostgreSQL 方言。
    - `db.lock_row(db, model, row_id) -> T | None`：自动解析 ORM 模型主键列，在 PostgreSQL 会话下附加 `SELECT ... WITH FOR UPDATE` 行级锁，在 SQLite 会话下优雅退化为短事务普通主键查询。
    - `db.ensure_postgres_database(url, *, admin_db="postgres") -> bool`：针对本机 `127.0.0.1:15432` 转发云端 PG 且尚未提前创建业务库（`agentforge` / `agentforge_test`）的场景，优先尝试直连目标库，仅在目标库不存在（SQLSTATE `3D000`）时自动以 `AUTOCOMMIT` 连接维护库执行 `CREATE DATABASE`。
    - `db.isolated_schema_url(url, schema)` / `db.ensure_postgres_schema(url, schema)` / `db.drop_postgres_schema(url, schema)`：为 PostgreSQL 测试连接附加独立 `search_path` Schema 隔离（并硬拦截删除 `public`），确保云端单库共享时测试 `downgrade` 绝不误删 `public` 下的正式表与 `seed_assets` 数据。
    - `db.read_local_env_var(name, env_file=None) -> str | None`：自动读取环境变量或未入库的 `platform-api/.env`（并过滤未填写的 `<...>` 占位符），防止执行测试或 `alembic` 时因漏 `source .env` 断连。
- 实际新增或修改文件：
  - 修改 `platform-api/pyproject.toml`
  - 修改 `platform-api/uv.lock`
  - 修改 `platform-api/src/agentforge_platform/db.py`
  - 修改 `platform-api/migrations/env.py`
  - 修改 `platform-api/.env.example`
  - 新增 `deploy/compose.local-pg.yaml`
  - 新增 `platform-api/tests/test_a01_postgres_compat.py`
  - 新增 `docs/A01_本地PG底座适配记录.md`
- 兼容性说明（旧 SQLite 测试 / 旧 asset-lock / 原主生成图开关关闭回归）：
  - 默认 `DEFAULT_DATABASE_URL = "sqlite+pysqlite:///./agentforge.db"` 保持不变；
  - `make_engine()` 仅在 SQLite 方言下挂载 `PRAGMA journal_mode=WAL / foreign_keys=ON / synchronous=NORMAL / busy_timeout=5000` 与 `check_same_thread=False`，在 PostgreSQL 方言下启用 `pool_pre_ping=True` 且不执行任何 SQLite `PRAGMA`；
  - `migrations/env.py` 按方言动态设置 `render_as_batch=not is_postgres(...)`，既保留 SQLite 批量迁移回退能力，又避免 PostgreSQL 下不必要的表重建；
  - 既有 15 个迁移脚本（`0001..0015`）无需破坏性改动即可通过 PostgreSQL 方言离线 DDL 全量编译与 SQLite 升级/逐级回退往返测试；
  - 既有 772 项后端 SQLite 测试、41 项前端测试、31 项 Runner 测试 100% 零回归。
- 执行命令与真实测试结果（PASS / FAIL / NOT_RUN）：
  - `cd platform-api && uv run --locked pytest tests/test_a01_postgres_compat.py -v` → **7 passed in 12.77s**（含真实 `127.0.0.1:15432` 云端 PostgreSQL 自动创建 `agentforge_test` 数据库、独立 `agentforge_test_a01` Schema 隔离迁移、核心表 CRUD、`lock_row` 行锁与回滚隔离实测 **PASS**）
  - `cd platform-api && uv run --locked alembic upgrade head` → **PASS**（自动在 `127.0.0.1:15432` 创建正式库 `agentforge` 并完成 `0001 -> 0015` 全量迁移）
  - `./scripts/ci_gate.sh`：
    - `platform-api`：`uv run --locked pytest` → **779 passed**（原 772 项 SQLite 测试 + 新增 7 项含真实 PG 联机测试全绿）；`uv run --locked ruff check .` → **All checks passed!**
    - `platform-ui`：`npm ci && npm run type-check && npm run build && npm test` → **41 passed**
    - `runner`：`uv run --locked pytest && uv run --locked ruff check .` → **31 passed, 1 skipped**，ruff clean
- WhenDone 逐条判定与证据位置：
  1. `make_engine` 仅在 SQLite 方言下执行 `PRAGMA`，在 PostgreSQL 下启用合理连接池（`pool_pre_ping=True, pool_recycle=300, connect_timeout=5`）：**PASS**（见 `platform-api/src/agentforge_platform/db.py` `make_engine` 及 `test_make_engine_separates_sqlite_pragmas_from_postgres_pool`）。
  2. 新增 `tests/test_a01_postgres_compat.py` 覆盖 URL 方言判定、锁 helper、15 个迁移脚本 PG DDL 编译、以及对真实 PG（`127.0.0.1:15432`）执行 `alembic upgrade head` + 核心表 CRUD + 事务隔离：**PASS**（7/7 全绿，真实 PG 联机实测通过）。
  3. 提供 `platform-api/.env.example` 与本地 `docker compose` PG 示例且零硬编码密码：**PASS**（见 `platform-api/.env.example` 与 `deploy/compose.local-pg.yaml`）。
  4. `./scripts/ci_gate.sh` 全绿，旧 SQLite 测试 772 passed 零回归：**PASS**（总计 779 passed）。
- 遗留风险与下一步依赖：
  - 真实 PostgreSQL（`127.0.0.1:15432`）下的 `agentforge` 正式库与 `agentforge_test` 测试库均已自动创建并通过 `0001..0015` 迁移验证；
  - 下一步进入 `A02`（本地/隧道 PG 逐文件资产表 `asset_release` + `asset_file` 与 `seed_assets` 同步器）。
