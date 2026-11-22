# AgentForge｜文档一：已实现能力、接口基线与复核流程

> **文档性质：** 项目截至 `HEAD = faa7427`（93 commits）的**真实已实现能力总览、接口目录与基线复核流程**。本文件只陈述经代码与验收记录证实的事实；凡仅完成确定性/模拟测试而未跑真实模型或真实 Runner 的项，一律如实标注 `NOT_RUN / 未接入主生成图`。
>
> **归档溯源：** 历史四份旧流程文档已归档至 `旧实现流程文档/`（`AgentForge_01_MVP_分步开发.md`、`AgentForge_02_迭代增强_分步开发Prompt_方法索引版.md`、`AgentForge_03_遗留闭环与模块自治_分步开发Prompt.md`、`AgentForge_04_自主交付与无人开发_分步开发Prompt.md`）；需求与技术参考见 `技术文档/Demo需求分析文档.docx`（v0.1）与 `技术文档/Demo技术文档_修订版_v0.3.docx`（v0.3）。
>
> **使用方式：** 在启动《文档二：现有底座改造与适配》或《文档三：异步工厂流水线与开源交付》前，先按本文件核对已有接口与基线回归（`B01—B04`），确保后续改造全部基于真实模块签名、绝不重复造轮子或破坏既有能力。

---

## 0. 已实现能力全景与真实状态总表

| 历史阶段 | 覆盖编号 | 实现与验证状态 | 核心交付与真实证据 | 遗留 `NOT_RUN` / 偏差 |
|---|---|---|---|---|
| **MVP 闭环** | `M01—M28`（含 `M08A/M15A`） | ✅ **已完成**（真实端到端跑通） | FastAPI + SQLite + Vue 3 UI + 模板装配 + 独立 Docker Runner `/verify` & `/accept`（16 项 HTTP 用例）+ ZIP 解压复现构建 + 备份恢复/限流/会话清理（见 `docs/MVP_ACCEPTANCE.md`） | Gitee Go CI `NOT_RUN`（本地 `scripts/ci_gate.sh` 替代）；执行环境为 Docker Desktop 共享 VM；`AGENTFORGE_PRICE_FILE` 未配置致费用记 `UNKNOWN` |
| **Demo P0 迭代** | `R01—R27`（含 `R06A—R06D`） | ✅ **已完成**（9/9 真实 Runner 达标） | 动态访谈 `interrupt/resume` + UI 问答卡片；3 个已发布 Method Unit + SQLite/FTS5 索引 + Asset Finder；4 组合 Profile（JS/TS × MyBatis/MyBatis-Plus）；校园活动/个人任务/图书借阅 9/9 离线 Runner 全绿（见 `docs/DEMO_P0_ACCEPTANCE.md`） | 9 次评估均由确定性能力包覆盖，Qwen 调用数为 0、Method Unit 复用为 0 |
| **扩展能力** | `E01—E10` | ⚠️ **确定性层完成**（134 passed） | Integration Pack 只读清单（`weather-lookup`）、Port/Adapter/Mock 契约、JSON OpenAPI Client 生成、文档规则提取、外部配置指南、预览鉴权/生命周期、增量迭代与资产提炼人审（见 `docs/E_MILESTONE_ACCEPTANCE.md`） | `E05` 真实 Live 沙箱 `NOT_RUN`；`E08` 真实隔离预览 `NOT_RUN`；`E06` 仅支持 JSON（无 YAML） |
| **遗留闭环** | `C00—C06` | ⚠️ **代码与受控测试完成** | 生产模型路由注入（`production_routing.py` → `worker`/`default_context`）；集成包显式开关装配与 `external_readiness` 落库（迁移 `0015`）；3 个 Method Unit 本机真实 Maven+JUnit 编译认证 3/3 `CERTIFIED`；E 范围跨厂商路由断言（见 `docs/PHASE3_BASELINE.md`、`docs/C01..C06`） | 真实 Qwen 付费工具调用探针、真实模型缺口 Patch、真实 Runner 集成包 Java 契约测试、C04/C05 真实 DeepSeek/Qwen 调用均为 `NOT_RUN`；C04 未接主生成图 |
| **模块自治** | `F01—F08` | ⚠️ **机制层与门禁全绿（SIMULATED）** | `ModuleRegistry`、`ModuleMemoryStore`、`InterfaceRegistry`、`ModuleExecutor`、`ModuleCoordinator`、`CommitStateMachine`、`module_evaluation`、`module_parallel`（分波并行 + `BudgetPool` + `ModuleStaging`）（见 `docs/MODULE_AGENT_ACCEPTANCE.md`、`docs/F01..F08`） | **全部未接入主生成图 `generation_graph.py`**；默认关闭；对照评估为模拟（`SIMULATED`）；真实多 worker 并发扩容未测 |

**当前自动化门禁基线（`./scripts/ci_gate.sh`）：**
- `platform-api`：`uv run --locked pytest` → **772 passed**；`ruff check .` → clean
- `platform-ui`：`npm ci && npm run type-check && npm run build && npm test` → **41 passed**
- `runner`：`uv run --locked pytest` → **30 passed, 2 skipped**；`ruff check .` → clean

---

## 1. 已有核心目录与资产清单

| 路径 | 权威内容 | 当前存储方式 |
|---|---|---|
| `platform-api/src/agentforge_platform/` | 79 个后端 Python 模块（FastAPI / SQLAlchemy / LangChain / LangGraph / DeepAgents） | Git 源码 |
| `platform-api/migrations/versions/` | 15 个 Alembic 数据库迁移（`0001` 至 `0015`） | 默认 SQLite（WAL，`agentforge.db`） |
| `platform-ui/src/` | 7 个 Vue 3 页面（`LoginView`、`ProjectListView`、`ProjectCreateView`、`ProjectDetailView`、`InterviewView`、`RunDetailView`、`ProfilesView`） | Vite + Element Plus |
| `runner/src/agentforge_runner/` | 独立受控沙箱执行服务（依赖 Docker Engine，默认 `container_network=none`） | 独立 FastAPI 服务 |
| `profiles/` | 8 份锁定 Profile（`certified.json`、`demo-{js,ts}-{mp,mb}-0.1`、`demo-ts-mp-0.2/0.3/0.4`） | 本地只读 JSON |
| `templates/` | 5 个版本化全栈基础工程模板（`demo-0.1`、`demo-0.2`、`demo-0.2-js`、`demo-0.2-mb`、`demo-0.2-js-mb`） | 本地只读目录 |
| `capability-packs/` | 4 个能力包族多版本（`task-crud-backend`、`task-crud-backend-mb`、`task-crud-frontend`、`task-crud-frontend-js`） | 本地只读目录 + Jinja2 |
| `methods/` | 3 个已认证 Method Unit（`field-validator`、`date-normalizer`、`status-guard` @ `1.0.0`） | 本地只读目录 + 派生 `methods/.index/method-index.sqlite`（FTS5） |
| `integration-packs/` | 1 个 Mock-only 集成包（`weather-lookup` @ `0.1.0/0.2.0/0.3.0`） | 本地只读目录（默认不进 `DEFAULT_ROOTS`） |
| `schemas/` | 冻结规格/契约与 7 份不可变 `asset-lock.*.sample.json` | 本地只读 JSON |

---

## 2. 已有数据库表与模型接口目录（供改造与新流水线复用）

### 2.1 现有数据库表（`platform-api/src/agentforge_platform/models.py`，迁移至 `0015`）
- `project`：`id`, `owner_id`, `name`, `requirement_text`, `active_spec_id`, `created_at`
- `spec_version`：`id`, `project_id`, `version`, `draft_revision`, `body_json`, `hash`, `confirmed_at`, `summary`, `missing_fields_json`, `questions_json`
- `platform_session`：`token_hash`, `account_id`, `csrf_token`, `created_at`, `expires_at`
- `run`：`id`, `project_id`, `spec_id`, `spec_hash`, `status`, `error_code`, `idempotency_key`, `cancel_requested`, `external_readiness`, `lease_owner`, `lease_until`, `created_at`, `updated_at`
- `task`：`id`, `run_id`, `task_id`, `status`, `attempt`, `output_hash`, `created_at`
- `event`：`id`, `run_id`, `sequence`, `type`, `payload`, `created_at`
- `artifact`：`id`, `run_id`, `path`, `sha256`, `kind`, `verified`, `created_at`
- `worker`：`worker_id`, `hostname`, `last_heartbeat`, `started_at`
- `budget_adjustment`：`id`, `project_id`, `run_id`, `delta_micro`, `reason`, `actor_id`, `created_at`
- `model_call`：`call_id`, `model_role`, `provider`, `model`, `project_id`, `draft_id`, `run_id`, `task_id`, `thinking_mode`, `reasoning_effort`, `request_id`, `price_version`, `price_currency`, `fx_rate_micro`, `fx_captured_at`, `input_micro_per_million`, `output_micro_per_million`, `reserved_micro`, `cost_micro`, `input_tokens`, `output_tokens`, `status`, `error_class`, `created_at`, `updated_at`

### 2.2 现有 HTTP API 接口清单

| 模块文件 | 路由前缀与端点 | 用途 |
|---|---|---|
| `routes.py` | `GET /health` | 平台健康检查 |
| `routes_auth.py` | `POST /api/auth/login`、`GET /api/auth/me`、`POST /api/auth/logout` | 预置账号登录（限流 + HttpOnly Cookie + CSRF） |
| `routes_projects.py` | `POST /api/projects`、`GET /api/projects`、`GET /api/projects/{project_id}` | 项目创建与归属查询 |
| `routes_interview.py` | `GET /api/projects/{project_id}/interview`、`POST /api/projects/{project_id}/interview` | 多轮动态访谈（LangGraph interrupt/resume） |
| `routes_spec.py` | `GET/PUT /api/projects/{project_id}/spec`、`POST .../extract`、`POST .../confirm`、`POST .../summary`、`GET .../spec/diff` | 规格提取、乐观锁编辑、冻结与版本 diff |
| `routes_profiles.py` | `GET /api/profiles`、`POST /api/profiles/explain` | 4 组合 Profile 查询与推荐理由解释 |
| `routes_runs.py` | `POST/GET /api/projects/{project_id}/runs`、`GET /api/runs/{run_id}`、`GET .../events`、`GET .../cost`、`POST .../cancel`、`POST .../resume`、`GET .../artifact`、`GET .../artifacts/{artifact_id}`、`POST .../iterate`、`POST/GET/DELETE .../preview`、`GET /api/preview/{token}`、`GET /api/workers` | 运行触发、事件流轮询、取消恢复、下载、迭代与预览状态管理 |
| `runner/main.py` | `GET /health`、`POST /run`、`POST /verify`、`POST /accept`（需 `X-Runner-Token`） | 独立 Docker 沙箱执行、构建校验与固定 HTTP 业务验收 |

### 2.3 现有关键 Python 内部接口（按职责分组）

| 领域 | 模块路径 | 关键公开类 / 函数 |
|---|---|---|
| **数据库与会话** | `db.py` | `get_database_url()`, `make_engine(url)`, `make_session_factory(engine)`, `session_scope(factory)`, `get_db()` |
| **模型网关与路由** | `model_gateway.py` / `clients.py` / `production_routing.py` | `Route`, `Price`, `role_enabled()`, `resolve_route()`, `lookup_price()`, `ModelClient`, `DeepSeekClient`, `DashScopeClient`, `build_production_routing()`, `build_ledger_model()`, `CodingRouting` |
| **预算与限额** | `budget.py` / `limits.py` | `effective_budget_micro()`, `record_adjustment()`, `RuntimeLimits`, `runtime_limits()`, `model_timeout_seconds()` |
| **规格与契约** | `spec.py` / `contract.py` / `interview.py` | `SoftwareSpecification`, `critical_pending_fields()`, `is_supported_profile()`, `BusinessContract`, `compile_from_spec()`, `validate_submission()` |
| **资产注册与索引** | `asset_registry.py` / `method_index.py` / `asset_finder.py` / `resolver.py` | `AssetRegistry`, `AssetManifest`, `MethodUnitManifest`, `IntegrationPackManifest`, `compute_checksum()`, `build_index()`, `search()`, `find_assets()`, `resolve_assets()`, `verify_lock_hash()` |
| **装配与打包** | `assembler.py` / `packaging.py` | `assemble()`, `manifest_hash_of()`, `package_run()`, `verification_passed()`, `build_verification()`, `build_cost_report()` |
| **受限 Agent 编码** | `agent_files.py` / `patch_engine.py` / `gap_patch.py` / `repair.py` | `FileBackend`, `FileChangeSet`, `StagingBackend`, `LedgerChatModel`, `PatchEngine`, `assert_restricted_tools()`, `apply_gap_patch()`, `classify_repair_failure()`, `run_repair_loop()` |
| **生成图与运行** | `generation_graph.py` / `worker.py` / `runner_client.py` | `GenerationContext`, `build_graph()`, `run_generation()`, `default_context()`, `build_generation_handler()`, `RunnerClient`, `HttpRunnerClient` |
| **模块自治（未接线）** | `module_registry.py` / `module_memory.py` / `interface_registry.py` / `module_agent.py` / `module_coordinator.py` / `module_integration.py` / `module_parallel.py` | `derive_modules()`, `effective_write_scope()`, `ModuleMemoryStore`, `InterfaceRegistry`, `PublicCapability`, `CapabilityEvidence`, `ModuleAgentFactory`, `ModuleCoordinator`, `CommitStateMachine`, `plan_waves()`, `BudgetPool`, `ModuleStaging`, `run_wave()` |

---

## 3. 改造前基线复核流程（B01—B04）

> **目的：** 每次换机、换人或在正式执行《文档二》与《文档三》之前，按以下 4 个只读核对步骤确认基线健康，严禁带着未知回归进入重构。

### 总控 Prompt（复核前附上）

```text
你是 AgentForge 的基线核查助手。只执行本次指定的 B01/B02/B03/B04 一步；本阶段为只读核对，禁止修改任何业务源码、Profile、模板、锁样例或测试断言。
对所有无法在本机运行的外部项（如真实 Docker Runner / 付费模型调用）必须如实标注 NOT_RUN，禁止伪造 PASS。
```

### B01｜仓库干净状态、归档结构与自动化门禁核对

- **接口选用：** `./scripts/ci_gate.sh`、`git status`、`git log --oneline -n 10`
- **Goal：** 确认旧文档归档与三份新文档就位后，现有后端/前端/Runner 全量自动化测试与静态检查仍 100% 通过，建立重构前的零回归基准点。
- **Context：** 历史基线提交为 `faa7427`，已有 `platform-api` 772 项测试、`platform-ui` 41 项测试、`runner` 30 项测试（2 skipped）。`docs/PHASE3_BASELINE.md` 记录了早期旧锁重放基线；旧流程文档现已归档至 `旧实现流程文档/`。
- **Constraints：** 不得修改任何源码、迁移或既有测试断言；不得删除或覆盖 `schemas/asset-lock.*.sample.json`；本步不发起外部网络请求或模型调用；如实记录本机 Python/Node/PostgreSQL/Docker 可用版本。
- **WhenDone：** `./scripts/ci_gate.sh` 返回 `gate passed`（后端 772 passed / ruff clean / 前端 41 passed / Runner 30 passed）；产出或更新基线核对记录（含 commit hash、环境版本及 `NOT_RUN` 项）。

### B02｜旧资产锁重放与本地资产目录完整性核对

- **接口选用：** `asset_registry.default_registry()`、`asset_registry.compute_checksum()`、`resolver.verify_lock_hash()`、`method_index.scan_methods()`、`method_index.build_index()`
- **Goal：** 验证当前 5 个模板、4 族能力包、3 个方法单元、1 个集成包以及 7 份 `schemas/asset-lock.*.sample.json` 的哈希与校验和全部可重放，作为迁入本地 PostgreSQL `asset_release` + `asset_file` 的权威真值对照。
- **Context：** `asset_registry.py` 通过 `DEFAULT_ROOTS`（`templates/`、`capability-packs/`）与 `DEFAULT_INTEGRATION_ROOT`（`integration-packs/`）扫描资产；`method_index.py` 以 `methods/` 为权威源码生成 FTS5 索引（当前 `index_revision` 见 `docs/PHASE3_BASELINE.md`）。
- **Constraints：** 只读扫描，不得修改任何已发布资产的 `manifest.json` 或源码文件；禁止重新生成或覆盖已提交的 `asset-lock.*.sample.json`。
- **WhenDone：** 7 份旧锁样例 `verify_lock_hash` 全部返回 `True`；所有模板/能力包/方法单元/集成包 checksum 校验通过且无损坏项；记录各资产清单与 checksum 对照表供 `A02` 校验。

### B03｜本机 PostgreSQL、Java/Maven、Node 与 Docker Runner 运行环境核对

- **接口选用：** `psql --version`、`java -version`、`mvn -version`、`node -v`、`docker info`、`runner/src/agentforge_runner/main.py` (`GET /health`)
- **Goal：** 明确本机开源开发与测试环境已就绪的工具链与缺失项，为后续《文档二》切换本地 PG 与《文档三》分层沙箱测试扫清环境盲区。
- **Context：** 后续架构已锁定：本地 PostgreSQL 存储元数据与逐文件资产表；部门小循环允许本地轻量沙箱/受限进程跑 `mvn test` / `vitest`，但后端总验收与全栈总验收必须强制走独立 Docker Runner。
- **Constraints：** 只做环境探针，不自动安装未经确认的系统软件，不擅自修改本机数据库密码或系统配置；未启动的服务如实标记为 `NOT_RUN / ENV_MISSING`。
- **WhenDone：** 明确记录本地 PostgreSQL 版本与连接串可用性、JDK 17/21 与 Maven 本地缓存可用性、Node 22 可用性、Docker Daemon 与 Runner 镜像状态，以及尚需安装的项。

### B04｜未接线模块与现有 API 边界盘点

- **接口选用：** `generation_graph.py`、`worker.py`、`module_registry.py`、`interface_registry.py`、`module_parallel.py`、`production_routing.py`
- **Goal：** 核实“已实现但未接入生产主生成图”的模块清单与现有公开 API 契约，避免《文档二》和《文档三》在接线时出现重复抽象或接口漂移。
- **Context：** `F01—F08` 模块均独立通过单测但未挂入 `generation_graph.py`；`deploy/api.Dockerfile` 与 `deploy/compose.yaml` 当前尚未包含 `templates/` 等资产目录；`README.md` 进度描述停留在旧版 R27。
- **Constraints：** 本步仅盘点核对并记录接口映射，不改动业务代码；任何与文档描述不符的签名以仓库实际代码为准。
- **WhenDone：** 确认所有《文档二》与《文档三》引用到的现有类/函数签名均与仓库代码一致，形成可执行的改造起点（进入《文档二》`A01`）。

---

## 4. 统一验收记录模板（供所有阶段复用）

```markdown
### 实施记录：[Bxx / Axx / FPxx]
- 基准 Commit / 结束 Commit：
- 对应文档与步骤编号：
- 当前状态：PASS / PARTIAL / FAIL / NOT_RUN / BLOCKED
- 接口选用（复用已有接口 / 本步新增接口）：
- 实际新增或修改文件：
- 兼容性说明（旧 SQLite 测试 / 旧 asset-lock / 原主生成图开关关闭回归）：
- 执行命令与真实测试结果（PASS / FAIL / NOT_RUN）：
- WhenDone 逐条判定与证据位置：
- 遗留风险与下一步依赖：
```
