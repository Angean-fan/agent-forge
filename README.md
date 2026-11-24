# AgentForge

AI 全栈软件工程生成平台。目标：将中文自然语言需求经结构化规格确认与契约冻结后，装配并增量生成可下载、可继续开发的 Java（Spring Boot + MyBatis-Plus）+ Vue 3 全栈工程，并在受控隔离 Runner 中完成构建与端到端业务验收。

## 当前进度与三份核心设计文档导航

- **已完成阶段**：
  - MVP 阶段 `M01—M28`（平台 API / 规格确认 / 模板与能力包确定性装配 / 受控 Docker Runner / 平台 UI / 部署与备份）
  - 迭代增强阶段 `R01—R27` / `C01—C06` / `E01—E05` / `F01—F08`（方法索引 / 补丁引擎 / 模块与契约登记 / 集成包只读清单 / 跨币种预算账本）
  - **底座改造与适配阶段 `A01—A09`（全部完成）**：见 `docs/A01_*.md` — `docs/A09_*.md`
    - `A01`：PostgreSQL 16 + SQLite 双引擎兼容与行锁原语（`db.py`、`deploy/compose.local-pg.yaml`）
    - `A02`：PostgreSQL 逐文件资产表（`asset_release` + `asset_file` + GIN 全文索引）与 `seed_assets` 幂等同步（28 个版本 / 444 个文件）
    - `A03`：本地只读物化资产缓存（`.cache/assets`、`0444/0555`）与子 Agent 逐文件检索/读取/装配工具（`asset_cache.py`、`agent_asset_tools.py`）
    - `A04`：自定义 OpenAI 兼容多模型路由与工厂角色映射（`OpenAICompatibleClient`、`config/models.example.json`）
    - `A05`：工厂模式范围校验（默认软上限 8 模块）与第三方能力强制本地沙箱/Mock 约定（`scope_policy.py`）
    - `A06`：开工前契约冻结态（`APPROVED_FOR_BUILD`）、SQL 号段并行调度、蓝图 5 项硬校验与只读 Java `Facade Stub` 生成器（`blueprint_contracts.py`）
    - `A07`：并发安全事务预算账本（PG `SELECT ... FOR UPDATE` 行锁防双扣 + 部门/阶段维度 `cost_by_department`，迁移 `0017`）
    - `A08`：Runner 单模块定向快检端点（`POST /verify-module`）与分层验证器（部门小循环可选受限子进程，总验收硬编码强制独立 Docker Runner）
    - `A09`：Docker 镜像只读资产打包修复、开源仓库隐私卫生审计与文档同步
  - **异步工厂流水线阶段 `FP01—FP10`（代码与自动化闭环已完成）**：见 `docs/FP01_*.md` — `docs/FP10_*.md` 及 [docs/FACTORY_PIPELINE_ACCEPTANCE.md](docs/FACTORY_PIPELINE_ACCEPTANCE.md)
    - 持久化多部门状态机（迁移 `0018`）→ 项目总管蓝图规划与冻结 → 只读后端底座 + Java `Facade Stub` → 三角色最小职责部门引擎 → `asyncio` 双信号量异步并发池 → 后端总装/归因大循环与权威 `openapi.json` 冻结 → 前端总管/TS 契约底座/并发流水线与声明式路由聚合 → 全栈总装/强制 Docker Runner 验收/工厂 ZIP 打包 → 平台 UI 多部门泳道进度页与模式切换
- **核心设计文档导航**：
  1. [AgentForge_01_已实现能力与接口基线.md](AgentForge_01_已实现能力与接口基线.md)（原 `AgentForge_01_现状盘点与开发约束.md`）：底座模块清单、真实接口签名与基线复核流程
  2. [AgentForge_02_现有底座改造与适配_分步开发Prompt.md](AgentForge_02_现有底座改造与适配_分步开发Prompt.md)：底座升级 `A01—A09`（已完成）
  3. [AgentForge_03_异步工厂流水线与开源交付_分步开发Prompt.md](AgentForge_03_异步工厂流水线与开源交付_分步开发Prompt.md)：异步多部门工厂流水线 `FP01—FP10`（代码与确定性验收已完成；真实商业 LLM + 实机 Docker Runner 待有环境时补跑）

> **真实状态说明**：Demo P0 端到端评估见 [docs/DEMO_P0_ACCEPTANCE.md](docs/DEMO_P0_ACCEPTANCE.md)，异步工厂流水线标杆验收见 [docs/FACTORY_PIPELINE_ACCEPTANCE.md](docs/FACTORY_PIPELINE_ACCEPTANCE.md)；无真实 Docker Runner 或无商业大模型 API Key 时一律如实标为 `NOT_RUN`，绝不伪造 `PASS`。

## 目录结构

| 路径 | 说明 |
|---|---|
| `platform-api/` | 平台后端（Python 3.12 / FastAPI / SQLAlchemy / Alembic `0001..0018`） |
| `runner/` | 独立受控 Runner（`X-Runner-Token` 鉴权、`/verify`、`/verify-module`、`/accept`、受限容器契约与工厂 OpenAPI 验收） |
| `platform-ui/` | 平台前端（Vue 3 + TypeScript + Vite + Element Plus，含多部门泳道进度页与模式切换） |
| `profiles/` | 锁定的 Platform Profile 与认证矩阵（`certified.json`、`demo-ts-mp-0.3` 等） |
| `templates/` | 生成项目基础模板（只读版本化资产，如 `demo-0.1/`、`demo-0.2/`） |
| `capability-packs/` | 可参数化业务能力包（4 族 17 个只读版本） |
| `methods/` | 可复用方法单元仓库（只读版本；本地派生索引 `.index/` 已 gitignore） |
| `integration-packs/` | 第三方集成只读适配包（Mock-only：`Port/Adapter/Mock`） |
| `schemas/` | 规格、契约与 7 份历史 `asset-lock.*.sample.json` 回归样例 |
| `scripts/` | 单一口径回归门禁（`ci_gate.sh`）与备份/恢复脚本（`backup.py` / `restore.py`） |
| `deploy/` | 平台部署编排（`compose.yaml`、`compose.local-pg.yaml`、`api.Dockerfile`、`ui.Dockerfile`） |
| `docs/` | 分步实施记录（`M01..M28`、`R01..R27`、`A01..A09`、`FP01..FP10`）、标杆验收报告与运维手册（`MVP_DEPLOY.md`） |

## 环境要求

- **Python 3.12** + [uv](https://docs.astral.sh/uv/)
- **Node.js 22+** & npm
- **JDK 17+**（用于生成 Java `Facade Stub` 校验及本地编译单测）
- **Docker**（可选：用于本地启动 PostgreSQL 16 或运行受控隔离 Runner）

## 开源快速上手

### 1. 一键运行全量回归门禁（无需外部依赖，默认 SQLite）

```bash
./scripts/ci_gate.sh
```

该脚本依次执行 `platform-api`（pytest + ruff）、`platform-ui`（`npm ci` + `vue-tsc` + `vite build` + `vitest`）和 `runner`（pytest + ruff）。

### 2. 启动 PostgreSQL 16 并初始化逐文件资产库（推荐）

**方式 A：本地 Docker 启动 PostgreSQL 16**

```bash
export POSTGRES_PASSWORD="<your_local_password>"
docker compose -f deploy/compose.local-pg.yaml up -d
```

**方式 B：连接已有 / 云端 PostgreSQL（如通过 SSH 本地隧道映射至 `127.0.0.1:15432`）**

复制配置模板并填写私有连接串（`platform-api/.env` 已 gitignore，严禁提交）：

```bash
cp platform-api/.env.example platform-api/.env
chmod 600 platform-api/.env
# 在 platform-api/.env 中配置 AGENTFORGE_DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:<port>/agentforge
```

执行数据库迁移并同步全仓库只读资产（28 个版本 / 444 个文件，严格幂等）：

```bash
cd platform-api
set -a; source .env; set +a
uv run --locked alembic upgrade head
uv run --locked python -m agentforge_platform.seed_assets
```

### 3. 配置自定义 OpenAI 兼容多模型路由（可选）

支持按角色（`user_interaction`、`technical_planning`、`coding`、`complex_repair` 以及工厂角色 `chief_architect`、`dept_lead`、`dept_coder`、`dept_tester`）独立配置任意 OpenAI 兼容 Provider（DeepSeek / DashScope / OpenRouter / 本地 Ollama 等）：

```bash
cp platform-api/config/models.example.json platform-api/config/models.json
# 仅通过 api_key_env 指定环境变量名（严禁直写密钥明文）
export AGENTFORGE_MODELS_CONFIG="$(pwd)/platform-api/config/models.json"
```

### 4. 配置登录账号并启动前后端开发服务

```bash
# 1) 生成预置账号（config/accounts.json 已 gitignore）
cp platform-api/config/accounts.example.json platform-api/config/accounts.json
cd platform-api && uv run --locked python -m agentforge_platform.passwords

# 2) 启动平台后端（默认 http://127.0.0.1:8000，健康检查 GET /health）
set -a; source .env; set +a
uv run --locked uvicorn agentforge_platform.main:app --reload

# 3) 启动独立 worker（如需开启异步工厂模式，同时在 API 与 worker 环境变量中设置 AGENTFORGE_FACTORY_PIPELINE_ENABLED=1）
uv run --locked python -m agentforge_platform.worker

# 4) 新终端启动平台前端（默认 http://localhost:5173，/api 代理到 8000）
cd platform-ui && npm ci && npm run dev
```

### 5. 开启异步工厂模式与分层测试

- **默认安全开关**：`AGENTFORGE_FACTORY_PIPELINE_ENABLED` 默认为 `false`（关闭时保持原有标准装配流水线零变更）；设为 `1` 后可在项目详情页切换「标准装配模式 / 异步工厂模式」，并在运行详情页实时查看多部门泳道进度（`GET /api/runs/{run_id}/factory`）。
- **总验收（后端总装验收 / 全栈最终验收）**：硬编码强制走独立 Docker Runner（`POST /verify`、`POST /accept`），严禁降级为本地子进程；后端总装自动下发 `backend_static_manifest / backend_test / backend_build`，`/accept` 对含权威 `backend/openapi.json` 的工厂产物自动走通用 OpenAPI + 沙箱路由验收。
- **单模块定向快检**：Runner 提供 `POST /verify-module`（`java` 定向 `mvn -B test -Dtest=...`，`frontend` 定向 `vue-tsc` + `vitest`）。仅在本地开发显式设置 `AGENTFORGE_LOCAL_DEPT_TEST_RUNNER=1` 时，允许部门小循环使用受限本地子进程快检（强制目录隔离、命令白名单 `mvn/npm/npx`、敏感环境变量清除、超时强杀进程树与日志脱敏）。

### 6. Docker Compose 部署

`deploy/api.Dockerfile` 已完整打包只读资产种子目录（`templates/`、`capability-packs/`、`methods/`、`integration-packs/`、`profiles/`、`schemas/`），容器内可直接完成模板装配与 `seed_assets`：

```bash
cd deploy && cp .env.example .env
# 默认 SQLite 模式：
docker compose up -d --build
# 或启用内置 PostgreSQL 16 模式（需在 deploy/.env 中设置 POSTGRES_PASSWORD 与 AGENTFORGE_DATABASE_URL）：
docker compose --profile postgres up -d --build
```

完整运维手册（Runner 隔离部署、登录限流、维护任务与备份恢复 `scripts/backup.py`/`restore.py`）见 [docs/MVP_DEPLOY.md](docs/MVP_DEPLOY.md)。
