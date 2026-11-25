# AgentForge

AI 全栈软件工程生成平台。将中文自然语言需求经结构化规格确认与契约冻结后，装配并增量生成可下载、可继续开发的 Java（Spring Boot + MyBatis-Plus）+ Vue 3 全栈工程，并在受控隔离 Docker Runner 中完成构建与端到端业务验收。

## 核心能力

- **结构化需求访谈与规格冻结**：多轮澄清、冲突检测、技术选型 Profile 匹配与规格哈希冻结。
- **PostgreSQL 逐文件资产底座**：只读版本化模板（`templates/`）、能力包（`capability-packs/`）、方法单元（`methods/`）与集成包（`integration-packs/`），支持 `seed_assets` 幂等入库与本地只读缓存物化（`.cache/assets`）。
- **自定义 OpenAI 兼容多模型路由**：按角色（`user_interaction`、`technical_planning`、`coding`、`complex_repair` 以及工厂角色 `chief_architect`、`dept_lead`、`dept_coder`、`dept_tester`）独立配置任意 OpenAI 兼容 Provider，强制环境变量注入密钥、严禁明文配置。
- **异步多部门工厂流水线（Feature Flag：`AGENTFORGE_FACTORY_PIPELINE_ENABLED`）**：
  - 项目总管蓝图规划与开工前契约冻结（`APPROVED_FOR_BUILD`）、SQL 号段并行分配与只读 Java `Facade Stub` 生成；
  - 部门三角色最小职责引擎（`DeptLead` 只读规划 / `DeptCoder` 受限路径编码 / `DeptTester` 只写测试 + 真实编译单测）；
  - `asyncio` 双信号量异步并发调度、并发安全事务预算账本与按部门费用归集；
  - 后端总装与权威 `openapi.json` 冻结 → 前端 TS 契约生成与声明式路由自动聚合 → 全栈总装与强制独立 Docker Runner 端到端验收；
  - 平台 UI 多部门泳道进度页与模式切换。
- **受控隔离 Runner**：`X-Runner-Token` 鉴权、固定任务白名单（`/verify`、`/verify-module`、`/accept`）、无网络/只读根/去特权容器执行与日志脱敏。

## 目录结构

| 路径 | 说明 |
|---|---|
| `platform-api/` | 平台后端（Python 3.12 / FastAPI / SQLAlchemy / Alembic） |
| `platform-ui/` | 平台前端（Vue 3 + TypeScript + Vite + Element Plus） |
| `runner/` | 独立受控 Docker Runner（`/verify`、`/verify-module`、`/accept` 与执行镜像） |
| `templates/` | 生成项目基础模板（Spring Boot + Vue 3 只读版本化模板代码） |
| `capability-packs/` | 可参数化业务能力包（只读版本化资产） |
| `methods/` | 可复用方法单元仓库（只读版本；本地派生索引 `.index/` 已 gitignore） |
| `integration-packs/` | 第三方集成只读适配包（`Port/Adapter/Mock` 沙箱模式） |
| `profiles/` | 锁定的 Platform Profile 与认证矩阵 |
| `schemas/` | 规格、契约与资产锁回归样例 |
| `scripts/` | 单一口径回归门禁（`ci_gate.sh`）与备份/恢复/冒烟脚本 |
| `deploy/` | 平台部署编排（`compose.yaml`、`compose.local-pg.yaml`、`api.Dockerfile`、`ui.Dockerfile`） |
| `docs/` | 运维部署手册（[MVP_DEPLOY.md](docs/MVP_DEPLOY.md)） |

## 环境要求

- **Python 3.12** + [uv](https://docs.astral.sh/uv/)
- **Node.js 22+** & npm
- **JDK 17+** & Maven（用于 Java 模板与生成项目编译单测）
- **Docker**（用于本地 PostgreSQL 16 及受控隔离 Runner）

## 快速上手

### 1. 运行全量回归门禁（无需外部依赖，默认 SQLite）

```bash
./scripts/ci_gate.sh
```

依次执行 `platform-api`（`pytest` + `ruff`）、`platform-ui`（`npm ci` + `vue-tsc` + `vite build` + `vitest`）和 `runner`（`pytest` + `ruff`）。

### 2. 启动 PostgreSQL 16 并初始化资产库（推荐）

**方式 A：本地 Docker 启动 PostgreSQL 16**

```bash
export POSTGRES_PASSWORD="<your_local_password>"
docker compose -f deploy/compose.local-pg.yaml up -d
```

**方式 B：连接已有 PostgreSQL**

复制配置模板并填写连接串（`platform-api/.env` 已 gitignore，严禁提交）：

```bash
cp platform-api/.env.example platform-api/.env
chmod 600 platform-api/.env
# 在 platform-api/.env 中配置 AGENTFORGE_DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:<port>/agentforge
```

执行数据库迁移并同步全仓库只读资产（严格幂等）：

```bash
cd platform-api
set -a; source .env; set +a
uv run --locked alembic upgrade head
uv run --locked python -m agentforge_platform.seed_assets
```

### 3. 配置模型路由

支持按角色配置任意 OpenAI 兼容 Provider（参考 `platform-api/config/models.example.json` 或全 DeepSeek 模板 `platform-api/config/models.deepseek.example.json`）：

```bash
cp platform-api/config/models.example.json platform-api/config/models.json
# 仅通过 api_key_env 指定环境变量名（严禁直写密钥明文）
export AGENTFORGE_MODELS_CONFIG="$(pwd)/platform-api/config/models.json"
```

### 4. 配置登录账号并启动前后端服务

```bash
# 1) 生成预置账号（platform-api/config/accounts.json 已 gitignore）
cp platform-api/config/accounts.example.json platform-api/config/accounts.json
cd platform-api && uv run --locked python -m agentforge_platform.passwords

# 2) 启动平台后端（默认 http://127.0.0.1:8000，健康检查 GET /health）
set -a; source .env; set +a
uv run --locked uvicorn agentforge_platform.main:app --reload

# 3) 启动独立 worker（开启异步工厂模式时在 API 与 worker 同时设置 AGENTFORGE_FACTORY_PIPELINE_ENABLED=1）
uv run --locked python -m agentforge_platform.worker

# 4) 新终端启动平台前端（默认 http://localhost:5173，/api 代理到 8000）
cd platform-ui && npm ci && npm run dev
```

### 5. 开启异步工厂模式与分层验证

- **特性开关**：`AGENTFORGE_FACTORY_PIPELINE_ENABLED` 默认关闭；设为 `1` 后可在项目详情页切换「标准装配模式 / 异步工厂模式」，并在运行详情页实时查看多部门泳道进度（`GET /api/runs/{run_id}/factory`）。
- **总验收**：后端总装验收与全栈最终验收强制走独立 Docker Runner（`POST /verify`、`POST /accept`），缺少 Docker Runner 时如实标 `NOT_RUN` / `INFRASTRUCTURE`，绝不伪造 `PASS`。
- **单模块定向快检**：Runner 提供 `POST /verify-module`；本地开发亦可显式设置 `AGENTFORGE_LOCAL_DEPT_TEST_RUNNER=1` 启用受限本地子进程快检。

### 6. Docker Compose 部署

`deploy/api.Dockerfile` 已完整打包只读资产种子目录（`templates/`、`capability-packs/`、`methods/`、`integration-packs/`、`profiles/`、`schemas/`）：

```bash
cd deploy && cp .env.example .env
# 默认 SQLite 模式：
docker compose up -d --build
# 或启用内置 PostgreSQL 16 模式（需在 deploy/.env 中设置 POSTGRES_PASSWORD 与 AGENTFORGE_DATABASE_URL）：
docker compose --profile postgres up -d --build
```

完整运维手册（Runner 隔离部署、登录限流、维护任务与备份恢复 `scripts/backup.py`/`restore.py`）见 [docs/MVP_DEPLOY.md](docs/MVP_DEPLOY.md)。
