# AgentForge｜文档二：现有底座改造与适配（A01—A09）｜分步开发 Prompt

> **文档性质：** 面向 AI 编码助手的**增量改造实施流程**。本文件专门处理“**把现有底座改造成支撑本地 PostgreSQL 开源、逐文件 PG 资产库、用户自定义模型路由与异步工厂流水线**”所需的改造项，不实现工厂业务编排本身（编排见《文档三》）。
>
> **前置依据：** 《文档一：AgentForge_01_已实现能力与接口基线.md》（基准 `HEAD = faa7427`，后端 772 passed / 前端 41 passed / Runner 30 passed）。
>
> **已锁定的 5 项设计决策（本文件全部对齐）：**
> 1. **本地 PG 资产存储：** `Git 源码种子 + PG 逐文件表（asset_release + asset_file）+ 本地只读物化缓存`。
> 2. **模块规模上限：** 工厂模式支持可配置软上限，测试阶段默认 **≤ 8 个业务模块**（`AGENTFORGE_FACTORY_MAX_MODULES=8`）。
> 3. **第三方支付/外部服务：** 默认走 **Port + Adapter + 本地沙箱/Mock 模拟**，不接真实支付与扣款。
> 4. **分层测试沙箱：** 本地开发态允许部门小循环使用**轻量沙箱 / 受限子进程**快跑单模块编译与单测；总验收必须强制使用**独立 Docker Runner**。
> 5. **模型路由：** 面向开源改为**用户可自定义角色路由**（支持按角色配置任意 OpenAI 兼容端点、模型名与可选价目表），同时保持对原有 DeepSeek / DashScope 默认配置的向后兼容。

---

## 0. 依赖图与总控 Prompt

### 0.1 改造步骤依赖图

```text
B01—B04（文档一基线核对完成）
  └─ A01 平台元数据库适配本地 PostgreSQL（保留 SQLite 单测兼容）
       ├─ A02 PG 逐文件资产表（asset_release + asset_file）+ tsvector 索引 + seed_assets
       │    └─ A03 本地只读物化缓存 + 面向子 Agent 的资产检索/读取工具
       ├─ A07 并发预算池与事务账本升级（PG FOR UPDATE 行锁 + dept_id 维度）
       └─ A04 用户可自定义模型路由与多 Provider（OpenAI 兼容）网关适配
  ├─ A05 工厂模式范围校验策略（8 模块软上限 + 第三方强制沙箱/Mock 策略）
  ├─ A06 模块与接口契约底座升级（APPROVED_FOR_BUILD 态 + SQL 号段 + Facade Stub 生成）
  ├─ A08 Runner 单模块快检端点 + 本地开发轻量沙箱/受限进程适配
  └─ A09 开源仓库卫生、Docker 资产打包修复与 README 进度同步
```

### 0.2 总控 Prompt（每一步开始前附上）

```text
你是 AgentForge 底座改造阶段的增量实施型编码助手。本次只执行指定的一个 Axx 步骤，严禁越界实施后续步骤或提前编写《文档三》的工厂编排代码。

【执行铁律】
1. 先核对当前 git status、文档一中的真实接口签名与本步涉及的实际源码文件，再动手修改。
2. 严禁修改、删除或放宽既有 772 项后端测试、41 项前端测试、30 项 Runner 测试的业务断言；严禁修改已发布的 templates/、capability-packs/、methods/、integration-packs/ 源文件与 schemas/asset-lock.*.sample.json。
3. 所有新能力必须通过默认关闭的 feature flag 或显式配置启用，确保关闭时既有 SQLite 测试、旧 asset-lock 重放、原串行 generation_graph 行为 100% 零回归。
4. 严禁把真实 API Key、数据库密码、私钥写入代码、测试样例、日志或提交记录；所有外部/付费模型调用与未启动的 Docker/PG 测试在无环境时如实标 NOT_RUN，禁止伪造 PASS。
5. 本步完成后，必须运行 `./scripts/ci_gate.sh`（以及本步新增的专项测试），编写 `docs/Axx_<简名>记录.md`（套用文档一 §4 模板），并以清晰的常规提交信息（如 `feat(a01): ...`）独立提交 git。
```

---

## 1. 分步详细实施流程（A01—A09）

### A01｜平台元数据库适配本地 PostgreSQL 与双引擎兼容

- **接口选用：**
  - **复用并扩展：** `platform-api/src/agentforge_platform/db.py`（`get_database_url()`, `make_engine()`, `make_session_factory()`, `session_scope()`, `get_db()`）、`platform-api/migrations/env.py`、`platform-api/pyproject.toml`
  - **新增接口：** `db.is_postgres(engine_or_url) -> bool`、`db.lock_row(db, model, row_id)`（PG 下自动加 `WITH FOR UPDATE`，SQLite 下优雅退化为短事务）
- **Goal：** 让平台后端原生支持连接本地 PostgreSQL（`postgresql+psycopg://...`）运行全量 15 个 Alembic 迁移与全部 ORM 读写操作，同时保留内存/文件 SQLite 对现有单元测试的无缝兼容。
- **Context：**
  - 当前 `db.py` 默认使用 `sqlite+pysqlite:///./agentforge.db` 并在连接事件中硬编码了 `PRAGMA journal_mode=WAL` 与 `PRAGMA foreign_keys=ON`；若直接传入 PostgreSQL URL，执行 SQLite PRAGMA 会报错。
  - 现有 15 个迁移脚本（`migrations/versions/0001..0015`）及 `models.py` 需核对所有字段类型、索引、布尔默认值与自增主键在 PostgreSQL 方言下完全合法。
  - `platform-api/pyproject.toml` 需新增锁定版本的 `psycopg[binary]` 驱动依赖，并更新 `uv.lock`。
- **Constraints：**
  - 禁止修改既有 15 个历史迁移文件的语义导致旧 SQLite 库无法升级；如确有 PG 方言不兼容处，仅做方言条件分支（`bind.dialect.name == "postgresql"`）。
  - 禁止硬编码任何数据库密码，连接串仅通过 `AGENTFORGE_DATABASE_URL` 环境变量读取，提供 `platform-api/.env.example` 与本地 `docker compose` PG 示例。
  - 不修改现有业务路由与既有测试断言；步骤完成并通过门禁后单独提交 git（`feat(a01): ...`）。
- **WhenDone：**
  - `make_engine` 仅在 SQLite 方言下执行 `PRAGMA`，在 PostgreSQL 下启用合理连接池（`pool_pre_ping=True`）；
  - 新增 `tests/test_a01_postgres_compat.py`：覆盖 URL 方言判定、锁 helper、以及在配置 `AGENTFORGE_TEST_PG_URL` 时对真实本地 PG 执行 `alembic upgrade head` + 核心表 CRUD + 事务隔离（无 PG 环境时真实 PG 用例明确标 `skipped/NOT_RUN` 并在实施记录说明）；
  - `./scripts/ci_gate.sh` 全绿，旧 SQLite 测试 772 passed 零回归；记录写入 `docs/A01_本地PG底座适配记录.md` 并独立 git commit。

---

### A02｜本地 PG 逐文件资产表（`asset_release` + `asset_file`）与 `seed_assets` 同步器

- **接口选用：**
  - **复用：** `asset_registry.py`（`DEFAULT_ROOTS`, `DEFAULT_INTEGRATION_ROOT`, `AssetManifest`, `MethodUnitManifest`, `IntegrationPackManifest`, `compute_checksum()`, `is_safe_owned_path()`）、`method_index.py`（`DEFAULT_METHODS_ROOT`, `scan_methods()`）
  - **新增迁移：** `migrations/versions/0016_asset_release_and_file.py`
  - **新增模型与服务：** `models.py`（`AssetRelease`, `AssetFile`）、`asset_store_pg.py`（`PgAssetStore`: `sync_from_filesystem()`, `get_release()`, `list_releases()`, `search_releases()`, `get_file()`, `list_files()`, `search_files()`）、CLI 入口 `python -m agentforge_platform.seed_assets`
- **Goal：** 在本地 PostgreSQL 建立 `asset_release` 与 `asset_file` 两张核心表及 `tsvector` + GIN 全文索引，并实现幂等同步工具 `seed_assets`，将 Git 仓库中的 `templates/`、`capability-packs/`、`methods/`、`integration-packs/` 一键同步入库。
- **Context：**
  - 架构已锁定采用 **Git 源码种子 + PG 逐文件表 + 本地只读缓存**。
  - `asset_release` 存：`id`, `kind`, `asset_id`, `version`, `profile_tags_json`, `provides_json`, `requires_json`, `description`, `manifest_json`, `checksum`, `search_text`（PG 下配 `tsvector` + GIN 索引）。
  - `asset_file` 存：`id`, `release_id`（外键级联）, `rel_path`, `content_text`, `sha256`, `size_bytes`, `is_binary`, `symbol_summary`（提取 Java 类/方法名或 TS/Vue 导出符号）, `search_text`（PG 下配 `tsvector` + GIN 索引）；`(release_id, rel_path)` 唯一约束。
  - `AssetRelease.checksum` 必须与现有 `asset_registry.compute_checksum(asset_dir)` 字节级一致，确保与所有旧 `asset-lock` 完全兼容。
- **Constraints：**
  - **绝不修改** Git 仓库内任何既有模板/能力包/方法单元/集成包源码文件；
  - `seed_assets` 必须**严格幂等**：同一 `(kind, asset_id, version)` 若 `checksum` 相同则跳过；若试图用不同内容覆盖已发布版本必须直接拒绝报错（不可变版本保护）；
  - 二进制文件或超大文件（> 2 MiB）禁止直接存纯文本，按安全策略拒绝或跳过并记录原因；严禁跟随符号链接导入宿主机文件；
  - 保留 SQLite 测试回退查询（SQLite 下可用 `LIKE` / 既有 FTS5 降级，PG 下走 `to_tsvector/plainto_tsquery`）；完成后提交 git（`feat(a02): ...`）。
- **WhenDone：**
  - 迁移 `0016` 在 SQLite 与本地 PG 均可干净升级与回滚；
  - 运行 `uv run python -m agentforge_platform.seed_assets` 可一次性把仓库全部 5 模板、4 族能力包、3 方法单元、1 集成包完整同步入库，且每条 `checksum` 与 `B02` 核对值 100% 一致；
  - 重复执行 `seed_assets` 为 0 变更（幂等）；篡改内容同版本号重导入被拒；
  - 新增 `tests/test_a02_pg_asset_store.py` 全绿；`./scripts/ci_gate.sh` 全绿；记录写入 `docs/A02_PG逐文件资产表记录.md` 并独立 git commit。

---

### A03｜本地只读物化缓存与面向子 Agent 的资产检索/读取工具

- **接口选用：**
  - **复用：** `asset_store_pg.py`（`PgAssetStore`）、`asset_registry.py`（`AssetRegistry`, `compute_checksum()`）、`patch_engine.py`（`RESTRICTED_TOOLS`, `audit_tool_table()`）、`agent_files.py`（`FileBackend`）
  - **新增模块：** `asset_cache.py`（`AssetMaterializer`: `materialize_release(kind, asset_id, version) -> Path`, `verify_cached_release()`）、`agent_asset_tools.py`（只读工具集：`search_assets(query, kind, profile, limit)`、`read_asset_file(asset_ref, rel_path, start_line, end_line)`、`apply_asset_to_staging(asset_ref, target_subdir, file_backend)`）
- **Goal：** 打通两条高速路径——① 整包装配走 `PG → 本地只读缓存目录 .cache/assets/<kind>/<id>@<version>/` 毫秒级 COPY；② 部门编码子 Agent 通过受限只读工具直接按文件/行号从 PG `asset_file` 精准读取参考代码或安全套用资产到自己的暂存区。
- **Context：**
  - 现有 `assembler.py` 依赖本地 `Path` 做 COPY/Jinja2 渲染；通过 `AssetMaterializer` 按 `checksum` 校验并物化到只读缓存目录后，现有 `assembler.py` 无需重写即可消费来自 PG 的资产。
  - 现有 `patch_engine.py` 只暴露了 6 个文件工具（`ls/read_file/write_file/edit_file/glob/grep`）并严格审计工具白名单（`FORBIDDEN_TOOL_NAMES = {"execute", "task"}`）。新增资产工具必须作为**显式只读扩展白名单**接入，仅在工厂模式下启用，且绝不放开 `execute`/`task`。
- **Constraints：**
  - 只读缓存目录必须做原子写入（先解包/写出到 `.tmp` 并校验 `compute_checksum == release.checksum`，再 `os.replace` 并设只读权限）；严禁任何 Agent 直接修改 `.cache/assets/`；
  - `read_asset_file` 单次返回行数/字节数受上限约束（默认 ≤ 300 行或 16 KB），防止子 Agent 一次塞爆上下文；
  - `apply_asset_to_staging` 必须经过 `FileBackend` 的 `allowed_paths` 与 `PROTECTED_PREFIXES` 校验，越界路径直接拒绝；
  - 默认关闭不改变现有 `PatchEngine` 的默认工具表（保证既有 `test_r16_patch_engine.py` 零改动全绿）；完成后提交 git（`feat(a03): ...`）。
- **WhenDone：**
  - 从 PG 物化到本地只读缓存的模板目录，交给 `assembler.assemble()` 装配出的项目与直接从 Git 根目录装配的 `manifest_hash` **逐字节完全一致**；
  - 子 Agent 可经 `search_assets` → `read_asset_file` 精准读取单文件片段，且对越界路径、未认证资产、受保护路径写入均有负例测试拦截；
  - 新增 `tests/test_a03_asset_cache_and_tools.py` 全绿；`./scripts/ci_gate.sh` 全绿；记录写入 `docs/A03_资产缓存与子Agent工具记录.md` 并独立 git commit。

---

### A04｜用户可自定义模型路由与多 Provider（OpenAI 兼容）网关适配

- **接口选用：**
  - **复用并扩展：** `model_gateway.py`（`Route`, `Price`, `resolve_route()`, `role_enabled()`, `lookup_price()`）、`clients.py`（`ModelClient`, `Usage`, `Completion`, `ProviderPreflightError`）、`production_routing.py`（`build_production_routing()`, `build_ledger_model()`, `RoleStatus`）
  - **新增接口：** `clients.OpenAICompatibleClient`、`model_gateway.load_custom_routes()`、`model_gateway.FACTORY_ROLES`（新增工厂角色：`chief_architect`, `dept_lead`, `dept_coder`, `dept_tester`，并允许回退映射到已有角色）、配置文件样例 `platform-api/config/models.example.json`
- **Goal：** 面向 GitHub 开源，将原来写死在代码中的“DeepSeek + 百炼 Qwen”路由改为**用户可自定义的多角色 OpenAI 兼容模型路由配置**，同时保留原有默认配置与全部既有路由断言的向后兼容。
- **Context：**
  - 当前 `model_gateway.py` 固定映射 `user_interaction → deepseek-flash`、`technical_planning → qwen3.7-plus`、`coding → qwen3-coder-flash`、`complex_repair → qwen3.8-max`。
  - 开源用户可能使用 DeepSeek、Qwen、OpenAI、Claude（经网关）、GLM、Kimi 或本地 Ollama/vLLM，只要兼容 OpenAI Chat Completions + Function Calling 协议，都应能按角色在 `config/models.json`（或环境变量 `AGENTFORGE_MODELS_CONFIG`）中自由指定 `provider`、`base_url`、`model`、`api_key_env`、`max_output_tokens`、`supports_tools` 与可选 `price_micro`。
  - **安全底线不变：** 配置文件只写 `api_key_env`（环境变量名），绝不把真实密钥写进 JSON 文件；每次调用仍经 `model_gateway` 记录 `provider / model / usage / price_version`（无价目表记 `UNKNOWN`，不伪算为 0）。
- **Constraints：**
  - 未配置自定义 `AGENTFORGE_MODELS_CONFIG` 时，`resolve_route()` 必须返回与原先完全一致的默认路由，确保既有 `test_c01_routing.py`、`test_c06_*`、`test_r27_*` 全部无需修改即通过；
  - 严禁打印或落库任何 API Key 明文；禁止新增未经过账本的裸客户端；
  - 对非法配置（缺 `base_url`、无效 URL、直接写入疑似密钥而非环境变量名、声明 `supports_tools=false` 却绑定到编码角色）在启动预检时明确拒绝（`CONFIG_REQUIRED / VALIDATION_ERROR`）；
  - 完成后独立提交 git（`feat(a04): ...`）。
- **WhenDone：**
  - 提供 `platform-api/config/models.example.json`（含 DeepSeek / Qwen / OpenAI / 本地 Ollama 示例片段）与校验加载器；
  - 任一工厂角色（`chief_architect`/`dept_lead`/`dept_coder`/`dept_tester`）与旧角色均可按用户配置动态解析到 `OpenAICompatibleClient` 与 `LedgerChatModel`；
  - 新增 `tests/test_a04_custom_model_routing.py`（覆盖默认回退、自定义多 Provider、密钥直写拦截、缺 Key 报 `CONFIG_REQUIRED`、账本精确记录自定义 provider/model/UNKNOWN）；
  - `./scripts/ci_gate.sh` 全绿；记录写入 `docs/A04_自定义模型路由记录.md` 并独立 git commit。

---

### A05｜工厂模式范围校验策略与第三方沙箱强制约定

- **接口选用：**
  - **复用并扩展：** `spec.py`（`SoftwareSpecification`, `Entity`, `BusinessPage`, `business_roles()`）、`interview.py`、`interview_generation.py`、`external_requirements.py`
  - **新增模块/接口：** `scope_policy.py`（`ScopeMode = "demo_mvp" | "factory"`、`ScopeLimits(max_modules=8, max_entities=12, max_pages=24, allow_sandbox_integrations=True)`、`validate_scope_for_mode(spec, mode)`、`enforce_sandbox_third_party(spec)`）
- **Goal：** 在不破坏既有 Demo P0 严格范围校验（1–3 实体、2 角色、8 页面、禁支付）的前提下，为工厂模式新增独立的范围策略：模块软上限默认 **≤ 8**（环境变量 `AGENTFORGE_FACTORY_MAX_MODULES=8` 可配），并允许电商类需求携带“支付/短信/存储等第三方能力”进入系统，但**强制锁定为本地沙箱 / Mock 模拟模式**。
- **Context：**
  - 当前 `spec.py` 与 R01 范围校验器会直接拒绝 >3 实体或包含“支付”的需求；直接改掉现有校验会破坏 R01 与 Demo P0 回归测试。
  - 因此必须按模式隔离：`demo_mvp` 模式保持原样零改动；`factory` 模式下校验模块数 ≤ `max_modules`（默认 8），超出则给出清晰缩减建议并阻止继续；遇到支付/外部回调类需求时自动挂载 `sandbox_only=True` 与 `Port/Adapter/Mock` 约束声明，严禁引入真实扣款或要求用户提供生产商户私钥。
- **Constraints：**
  - 默认模式保持 `demo_mvp`，确保既有 R01–R27 测试零改动通过；
  - `factory` 模式下即使允许“购买支付”，仍**硬禁止**真实生产支付凭证进入 spec/Prompt/源码，并明确拦截明显超边界的高危请求（如任意公网爬虫、分布式微服务编排、宿主机命令执行）；
  - 完成后独立提交 git（`feat(a05): ...`）。
- **WhenDone：**
  - 以“电商需求（登录注册、首页展示、加入购物车、创建订单、购买支付）”输入 `validate_scope_for_mode(..., mode="factory")` → 校验通过且支付自动标记为 `sandbox_only=True`；
  - 输入 10 模块超大需求（超默认 8 模块上限）→ 被拒绝并返回明确缩减建议；
  - `demo_mvp` 模式行为 100% 不变；新增 `tests/test_a05_scope_policy.py` 全绿；`./scripts/ci_gate.sh` 全绿；记录写入 `docs/A05_工厂范围与沙箱策略记录.md` 并独立 git commit。

---

### A06｜模块与接口契约底座升级（`APPROVED_FOR_BUILD`、SQL 号段与 `Facade Stub` 生成）

- **接口选用：**
  - **复用并扩展：** `module_registry.py`（`Module`, `ModuleRegistry`, `registry_issues()`, `effective_write_scope()`）、`interface_registry.py`（`PublicCapability`, `InterfaceRegistry`, `STATUS_DRAFT`, `STATUS_VERIFIED`, `STATUS_DEPRECATED`, `registry_issues()`）
  - **新增接口：**
    - `interface_registry.STATUS_APPROVED_FOR_BUILD = "APPROVED_FOR_BUILD"`（开工前冻结态）
    - `module_registry.Module` 新增可选字段 `migration_range: tuple[int, int] | None = None`、`test_paths: list[str] = []`、`owned_package: str | None = None`
    - `blueprint_contracts.py`（`ArchitectureBlueprint`, `validate_blueprint(blueprint, spec) -> list[str]`, `generate_java_facade_stubs(blueprint, target_dir) -> list[str]`）
- **Goal：** 升级 F01/F03 契约底座，支持项目总管在开工前冻结跨模块公共契约（`APPROVED_FOR_BUILD`）、校验各部门目录/SQL 号段零交集，并由程序自动生成只读的 Java `modules/<dept_id>/api/*Facade.java` 与公共 DTO 骨架，使有依赖关系的部门也能在第 0 秒同时异步开工。
- **Context：**
  - 现有 `module_registry.py` 的 `Module` 与 `interface_registry.py` 的 `InterfaceRegistry` 均使用 `ConfigDict(extra="forbid")`，且当前接口只在“实现后”从 `DRAFT → VERIFIED`，缺少“开工前冻结态”与 SQL 号段冲突校验。
  - 现有 `module_parallel.py` 把所有 `/db/migration/` 粗粒度视为全局互斥资源（导致只要写 SQL 就被迫串行）；引入互不重叠的 `migration_range`（如 `auth: 100–199`、`product: 200–299`、`order: 400–499`，跨表外键放 `V900__relations.sql`）后，各部门才能安全并行编写各自建表脚本。
- **Constraints：**
  - 新增字段必须提供向后兼容默认值，严禁破坏现有 `test_f01_module_registry.py`、`test_f03_interface_registry.py`、`test_f06_module_integration.py`、`test_f08_module_parallel.py`；
  - `validate_blueprint` 必须执行 5 项硬校验：目录零交集、SQL 号段零交集、依赖契约全存在且无模糊签名、模块/接口无环、覆盖全部冻结业务需求；
  - 生成的 `Facade Stub` 文件必须纳入只读受保护路径，禁止单个部门私自修改跨模块接口签名；
  - 完成后独立提交 git（`feat(a06): ...`）。
- **WhenDone：**
  - 给定一份 5 部门电商 `ArchitectureBlueprint`，`validate_blueprint` 通过并能确定性生成合法可编译的 `modules/<dept_id>/api/*Facade.java` 骨架；
  - 覆盖 5 类负例（目录重叠、SQL 号段重叠、模糊签名 `TODO/*`、依赖环、未声明的跨模块依赖）全部被确定性拒绝；
  - 新增 `tests/test_a06_blueprint_contracts.py` 全绿；`./scripts/ci_gate.sh` 全绿；记录写入 `docs/A06_契约冻结与Facade骨架记录.md` 并独立 git commit。

---

### A07｜并发预算池与事务账本升级（PG 行锁防双扣 + 部门维度记账）

- **接口选用：**
  - **复用并扩展：** `models.py`（`ModelCall`）、`budget.py`（`effective_budget_micro()`）、`model_gateway.py`（预留/结算逻辑）、`module_parallel.py`（`BudgetPool`）、`packaging.py`（`build_cost_report()`）
  - **新增迁移：** `migrations/versions/0017_model_call_dept_dimension.py`（`model_call` 新增可空字段 `dept_id: String(64)`、`phase: String(16)`、`round: Integer`）
  - **新增接口：** `budget_pg.py`（`PgBudgetLedger`: `reserve_atomic()`, `settle_idempotent()`, `release_atomic()`, `cost_by_department()`）
- **Goal：** 将预算预留与幂等结算升级为基于 PostgreSQL 行锁（`SELECT ... FOR UPDATE`）的并发安全事务账本，并增加按部门（`dept_id`）和阶段（`backend/frontend`）的精细费用归集能力。
- **Context：**
  - 现有 `module_parallel.BudgetPool` 只是单进程内存 `threading.Lock`，不具备跨重启持久化与数据库一致性；现有 `ModelCall` 仅有 `run_id` 和 `task_id`，无法直观区分某个部门小循环花了多少 Token 和费用。
  - 在多部门 `asyncio` 并发调用大模型时，多个协程会同时尝试预留同一笔 `run` 预算，必须依靠 PG 短事务行锁确保 `spent + reserved + new_reserve <= effective_budget`，严禁超扣或双扣。
- **Constraints：**
  - 新增数据库列全部可空（`nullable=True`），旧数据与现有 `build_cost_report()` 保持 100% 向后兼容；
  - 任何 `usage` 缺失或未配单价的调用仍必须保留预留并记 `UNKNOWN`，严禁记零；
  - 必须在并发压测（多线程/多协程同时抢占有限预算）下证明零超额、零双扣、幂等 `call_id` 重复结算不重复扣费；
  - 完成后独立提交 git（`feat(a07): ...`）。
- **WhenDone：**
  - 迁移 `0017` 在 SQLite 与 PG 均正常执行；
  - 10 路并发协程争抢仅够 3 次调用的预算池，精确放行 3 次、拦截 7 次（`BUDGET_EXCEEDED`），最终账本总额 100% 吻合；
  - `cost_by_department(db, run_id)` 可输出按阶段与部门拆分的调用次数、Token 与费用汇总；
  - 新增 `tests/test_a07_concurrent_budget.py` 全绿；`./scripts/ci_gate.sh` 全绿；记录写入 `docs/A07_并发预算与部门账本记录.md` 并独立 git commit。

---

### A08｜Runner 单模块快检端点与本地轻量沙箱/受限进程适配

- **接口选用：**
  - **复用并扩展：** `runner/src/agentforge_runner/main.py`（现有 `/health`, `/run`, `/verify`, `/accept`）、`platform-api/src/agentforge_platform/runner_client.py`（`RunnerClient`, `HttpRunnerClient`）
  - **新增接口：**
    - Runner 新增端点：`POST /verify-module`（输入 `{artifact_dir, module_id, stack: "java"|"frontend", test_selector}`，仅执行单模块编译与定向单测如 `mvn -B test -Dtest=...` 或 `npx vue-tsc --noEmit && npx vitest run ...`，返回结构化 `ModuleVerificationResult`）
    - 平台新增模块：`module_verifier.py`（统一抽象 `ModuleVerifier` 协议 + 两种实现：① `DockerRunnerModuleVerifier` 调 Runner `/verify-module`；② `LocalProcessModuleVerifier` 仅在本地开发显式开启 `AGENTFORGE_LOCAL_DEPT_TEST_RUNNER=1` 时使用受限子进程快跑单模块单测，带超时、工作目录隔离、最小环境变量白名单与日志截断脱敏）
- **Goal：** 落地已确认的“**部门小循环支持轻量沙箱/受限进程快跑 + 后端/全栈总验收强制 Docker Runner**”分层测试架构，把单部门测试反馈时间从几十秒压缩到数秒级，同时守住总验收的强隔离底线。
- **Context：**
  - 当前 `runner` 只有全量 `/verify` 和 `/accept`，每次都会完整跑整个前后端构建与 MySQL 容器启动，放在每个部门的每次小循环里会极慢且吃满本机内存。
  - 同时，总验收（后端总装验收、全栈最终验收）**绝对不允许**降级为本地子进程，必须硬编码强制走 `HttpRunnerClient.verify()` / `accept()`（独立 Docker Runner）。
- **Constraints：**
  - `LocalProcessModuleVerifier` **绝不可用于总验收**，代码中必须用类型与运行时断言双重阻止；
  - `LocalProcessModuleVerifier` 启动子进程时必须清除敏感环境变量（绝不透传 `DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`AGENTFORGE_DATABASE_URL` 等），严格禁止 `shell=True`，仅允许固定白名单命令（`mvn` / `npm` / `npx`），设置硬超时（默认 120s）并强制杀进程树；
  - Runner 现有 `/verify` 与 `/accept` 契约零改动；完成后独立提交 git（`feat(a08): ...`）。
- **WhenDone：**
  - Runner 新增 `/verify-module` 端点并通过单测（覆盖合法测试选择器、非法参数注入拦截、超时清理）；
  - `LocalProcessModuleVerifier` 通过环境变量隔离、命令白名单、超时强杀、脱敏日志截断测试；
  - 尝试在总验收入口使用本地进程验证器直接抛出安全异常拒绝；
  - 新增 `tests/test_a08_layered_verifier.py` 与 `runner/tests/test_verify_module.py` 全绿；`./scripts/ci_gate.sh` 全绿；记录写入 `docs/A08_分层验证与单模块快检记录.md` 并独立 git commit。

---

### A09｜开源仓库卫生、Docker 资产打包修复与 `README.md` 同步

- **接口选用：** `deploy/api.Dockerfile`、`deploy/compose.yaml`、`README.md`、`.gitignore`、`scripts/ci_gate.sh`
- **Goal：** 补齐开源发布前的工程短板：修复 `deploy/api.Dockerfile` 未打包资产目录的缺陷、提供本地 PostgreSQL 一键启动编排、清理并审计仓库隐私安全、更新滞后的 `README.md` 使其准确反映当前架构与一键本地开源启动步骤。
- **Context：**
  - 当前 `deploy/api.Dockerfile` 只拷贝了 `platform-api/`，未包含 `templates/`、`capability-packs/`、`methods/`、`integration-packs/`、`profiles/`，容器内无法完成模板装配或 `seed_assets`。
  - 当前 `README.md` 仍停留在 R27 NOT_RUN 状态，且缺少本地 PostgreSQL 启动、`seed_assets` 初始化与自定义模型配置说明。
- **Constraints：**
  - 不删改任何业务代码；确保 `.env`、`config/accounts.json`、`config/models.json`、`*.db`、`out/`、`.cache/` 均在 `.gitignore` 中且未混入任何真实密钥；
  - 保持现有 `./scripts/ci_gate.sh` 口径稳定；完成后独立提交 git（`chore(a09): ...`）。
- **WhenDone：**
  - `deploy/compose.yaml` 新增本地 `postgres:16` 服务样例（或提供独立的 `deploy/compose.local-pg.yaml`）；`deploy/api.Dockerfile` 正确包含只读资产种子目录；
  - 完成全仓库密钥模式扫描（无真实 Key/Token/私钥残留）；
  - `README.md` 同步更新为当前真实能力、三份新版文档导航、本地 PG 启动与开源快速上手指南；
  - `./scripts/ci_gate.sh` 全绿；记录写入 `docs/A09_开源卫生与部署修复记录.md` 并独立 git commit。

---

## 2. 本阶段完成门槛（进入《文档三》的前提）

满足以下全部条件后，方可进入《文档三：异步工厂流水线与开源交付》：
1. `A01—A09` 全部提交并各有 `docs/Axx_*.md` 实施记录；
2. 本地 PostgreSQL 上跑通 `alembic upgrade head` + `seed_assets` + 逐文件检索 + 只读缓存装配 + 并发预算行锁压测；
3. 原有 772 后端 / 41 前端 / 30 Runner 测试与 7 份旧 `asset-lock` 重放全部零回归。
