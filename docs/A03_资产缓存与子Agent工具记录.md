### 实施记录：A03｜本地只读物化缓存与面向子 Agent 的资产检索/读取工具

- 基准 Commit / 结束 Commit：`1900fc0`（本步独立提交见 git log `feat(a03): ...`）
- 对应文档与步骤编号：《AgentForge_02_现有底座改造与适配_分步开发Prompt.md》`A03`
- 当前状态：**PASS**（已在 SQLite 及本机 `127.0.0.1:15432` 真实云端 PostgreSQL 上完成只读物化缓存装配逐字节 `manifest_hash` 比对、子 Agent 检索/按行读取/安全套用暂存及 `PatchEngine` 工具表审计实测，0 `NOT_RUN`）
- 接口选用（复用已有接口 / 本步新增接口）：
  - **复用**：
    - `asset_store_pg.py`：`PgAssetStore`
    - `asset_registry.py`：`AssetRegistry`、`compute_checksum()`、`is_safe_owned_path()`
    - `patch_engine.py`：`RESTRICTED_TOOLS`、`FORBIDDEN_TOOL_NAMES`、`audit_tool_table()`、`assert_restricted_tools()`、`build_patch_engine()`
    - `agent_files.py`：`FileBackend`
  - **本步新增模块与接口**：
    - `platform-api/src/agentforge_platform/asset_cache.py`：
      - `AssetMaterializer`：`materialize_release(kind, asset_id, version) -> Path`、`verify_cached_release(kind, asset_id, version) -> bool`、`materialize_lock(lock) -> AssetRegistry`、`materialize_all() -> AssetRegistry`、`as_registry()`、`clear_cache()`；
      - `make_writable_tree()`、`remove_readonly_tree()`。
    - `platform-api/src/agentforge_platform/agent_asset_tools.py`：
      - `search_assets(query, kind=None, profile=None, limit=10, ...)`
      - `read_asset_file(asset_ref, rel_path, start_line=1, end_line=None, ...)`（强制默认上限 `DEFAULT_MAX_READ_LINES = 300`、`DEFAULT_MAX_READ_BYTES = 16384`）
      - `apply_asset_to_staging(asset_ref, target_subdir, file_backend, ...)`（严格经 `FileBackend.write()` 校验 `allowed_paths` 与 `PROTECTED_PREFIXES`，硬拦截向 `.cache/assets` 写入）
      - `build_agent_asset_tools(file_backend=..., ...)`
    - `platform-api/src/agentforge_platform/patch_engine.py`：
      - 新增显式只读扩展白名单 `ASSET_READONLY_TOOLS = ("search_assets", "read_asset_file", "apply_asset_to_staging")`；
      - `assert_restricted_tools(..., allow_asset_tools=False)` 与 `build_patch_engine(..., allow_asset_tools=False)` 默认关闭扩展白名单，仅在工厂模式下显式传入 `allow_asset_tools=True` 时放行该 3 个只读工具，且始终硬禁止 `execute` 与 `task`。
- 实际新增或修改文件：
  - 修改 `.gitignore`（补充 `.cache/`）
  - 新增 `platform-api/src/agentforge_platform/asset_cache.py`
  - 新增 `platform-api/src/agentforge_platform/agent_asset_tools.py`
  - 修改 `platform-api/src/agentforge_platform/patch_engine.py`
  - 新增 `platform-api/tests/test_a03_asset_cache_and_tools.py`
  - 新增 `docs/A03_资产缓存与子Agent工具记录.md`
- 兼容性说明（旧 SQLite 测试 / 旧 asset-lock / 原主生成图开关关闭回归）：
  - `PatchEngine` 默认 `allow_asset_tools=False`，默认工具表仍严格限制在 6 个文件工具（`ls/read_file/write_file/edit_file/glob/grep`），既有 `test_r16_patch_engine.py` 11 项单测零修改全绿；
  - 从 PostgreSQL 物化到本地只读缓存 `.cache/assets/<kind>/<id>@<version>/` 后构造的 `AssetRegistry`，直接传给现有 `assembler.assemble()` 即可零改动完成项目装配，产出的 `manifest_hash` 与直接从 Git 根目录装配逐字节 100% 一致。
- 执行命令与真实测试结果（PASS / FAIL / NOT_RUN）：
  - `cd platform-api && uv run --locked ruff check . && uv run --locked pytest tests/test_r16_patch_engine.py tests/test_a03_asset_cache_and_tools.py -v` → **16 passed**（含 `test_live_postgres_materialize_assemble_and_agent_tools` 连接真实 `127.0.0.1:15432/agentforge` 云端 PostgreSQL 完成物化装配与工具检索读取实测 **PASS**）
  - `./scripts/ci_gate.sh`：
    - `platform-api`：`uv run --locked pytest` → **790 passed**；`uv run --locked ruff check .` → **All checks passed!**
    - `platform-ui`：`npm ci && npm run type-check && npm run build && npm test` → **41 passed**
    - `runner`：`uv run --locked pytest && uv run --locked ruff check .` → **31 passed, 1 skipped**，ruff clean
- WhenDone 逐条判定与证据位置：
  1. 从 PG 物化到本地只读缓存的模板与能力包目录，交给 `assembler.assemble()` 装配出的项目与直接从 Git 根目录装配的 `manifest_hash` 逐字节完全一致：**PASS**（见 `test_materialize_from_db_matches_git_assembly_manifest_hash_byte_for_byte` 与 `test_live_postgres_materialize_assemble_and_agent_tools`）。
  2. 只读缓存目录采用 `.tmp` + `compute_checksum` 核验 + `os.replace` 原子发布 + 递归只读权限（`0444`/`0555`），损坏后可自动检测并修复：**PASS**。
  3. 子 Agent 可经 `search_assets` → `read_asset_file` 精准读取单文件片段（受 ≤ 300 行 / 16 KB 上限保护），且对越界路径、未认证资产、`allowed_paths` 外路径、`PROTECTED_PREFIXES` 受保护路径与 `.cache/assets` 写入均有负例测试拦截：**PASS**（见 `test_search_and_read_asset_file_with_bounds_and_negative_cases` 与 `test_apply_asset_to_staging_enforces_allowed_and_protected_paths`）。
  4. 新增 `tests/test_a03_asset_cache_and_tools.py` 全绿、`./scripts/ci_gate.sh` 全绿：**PASS**。
- 遗留风险与下一步依赖：
  - PG 逐文件资产表、本地只读物化缓存与面向子 Agent 的只读资产工具链已全部打通；
  - 下一步进入 `A04`（用户可自定义模型路由与多 Provider OpenAI 兼容网关适配）。
