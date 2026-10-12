# AgentForge

AI 软件项目生成平台（Demo / MVP 阶段）。目标：把中文自然语言需求，经结构化规格确认后装配为可下载、可继续开发的 Java 全栈工程，并在受控沙箱中完成构建与业务验收。

- 需求依据：《技术文档/Demo需求分析文档》v0.1
- 技术依据：《技术文档/Demo技术文档_修订版_v0.3》
- 分步实施说明：MVP `AgentForge_01_MVP_分步开发.md`（M01—M28 已完成）；迭代增强 `AgentForge_02_迭代增强_分步开发Prompt_方法索引版.md`
- 当前进度：**第二阶段 R06B**（需求访谈闭环 R01—R06 完成；方法单元契约 R06A、方法与 FTS5 索引 R06B 完成；见 `docs/R06*.md`）；
  MVP 冷启动验收见 `docs/MVP_ACCEPTANCE.md`、`docs/MVP_DEPLOY.md`
- 下一步：**R06C**（Asset Finder：精确检索 + FTS5 召回 + 确定性兼容筛选）

## 目录

| 路径 | 说明 | 引入阶段 |
|---|---|---|
| `platform-api/` | 平台后端（Python 3.12 / FastAPI） | M07 ✅ |
| `runner/` | 独立受控 Runner（内部认证 + 固定任务 + 隔离容器契约） | M16 ✅ |
| `platform-ui/` | 平台前端（Vue 3 + TS + Vite + Element Plus） | M25–M27 ✅ |
| `profiles/` | 锁定的 Platform Profile（当前 `demo-ts-mp-0.3`） | M08/M16 ✅ |
| `templates/` | 生成项目基础模板（版本化 `demo-0.1/`、`demo-0.2/`） | M09/M13/M15A ✅ |
| `capability-packs/` | 可参数化业务能力包（版本化 `<id>/<version>/`） | M11–M15A ✅ |
| `methods/` | 可复用方法单元仓库（`<id>/<semver>/`，含 manifest/src/tests）；FTS5 索引为派生视图（`.index/`，gitignore） | R06A/R06B ✅ |
| `schemas/` | 规格/契约协议与样例（`*.spec.json`、`*.contract.json`、`asset-lock.sample.json`） | M06/M10/M14 ✅ |
| `scripts/` | 平台基础回归门禁（`ci_gate.sh`）+ 备份/恢复（`backup.py`/`restore.py`） | M08A/M28 ✅ |
| `deploy/` | 平台部署编排（compose + Dockerfile + nginx） | M28 ✅ |
| `.workflow/` | Gitee Go 流水线配置（待启用后核对） | M08A ✅ |
| `docs/` | 每步实施记录 + `MVP_DEPLOY.md`/`MVP_ACCEPTANCE.md` | M01/M28 |

> 其余建议目录（worker/、runner/、profiles/、templates/、capability-packs/、schemas/ 等）在对应步骤落地时再创建，本步不预建空目录。

## 环境要求

本机已验证的版本：

- Python **3.12**（系统 `python3` 为 3.12.10）
- Node.js **22**（v22.19.0）、npm 10.9.3
- [uv](https://docs.astral.sh/uv/)（Python 依赖与虚拟环境管理）
- git

## 安装与运行测试

### 后端（platform-api）

```bash
cd platform-api
uv sync --dev      # 按 uv.lock 复现依赖环境
uv run pytest      # 运行测试
uv run ruff check  # 静态检查
uv run alembic upgrade head   # 在空 SQLite 上应用平台迁移（默认 ./agentforge.db）
```

平台数据库连接串可用 `AGENTFORGE_DATABASE_URL` 覆盖（默认 `sqlite+pysqlite:///./agentforge.db`，已开启 WAL）。

平台基础门禁（本地一键，CI 亦调用同一脚本）：

```bash
./scripts/ci_gate.sh   # = uv run --locked pytest + ruff check . + npm test
```

启动 API（默认 http://127.0.0.1:8000，健康检查 `GET /health`）：

```bash
cd platform-api
set -a; source .env; set +a  # 本机私有配置；见下方 M22 说明
uv run uvicorn agentforge_platform.main:app --reload
```

统一响应约定：成功 `{"request_id", "data"}`；失败 `{"request_id", "error": {"code", "message", "retryable"}}`；每个响应带 `X-Request-ID` 头（可用同名请求头指定）。

平台内测账号（不做注册/找回）：复制 `platform-api/config/accounts.example.json` 为 `config/accounts.json`（已 gitignore），用下面命令生成密码哈希填入：

```bash
cd platform-api
uv run python -m agentforge_platform.passwords   # 交互式生成 scrypt 哈希
```

认证：`POST /api/auth/login`（预置账号）→ 下发 HttpOnly 会话 Cookie 与可读 CSRF Cookie；状态变更请求须带 `X-CSRF-Token`；`GET /api/auth/me`、`POST /api/auth/logout`。

项目：`POST /api/projects`（名称 1–60 字、初始需求 20–5000 字）、`GET /api/projects`（仅当前用户）、`GET /api/projects/{id}`（归属校验）。创建仅保存需求草稿，不生成代码。

规格：`GET/PUT /api/projects/{id}/spec`（读取/编辑草稿）、`POST /api/projects/{id}/confirm`（校验必需字段并冻结版本，生成 hash）。读取与提取响应带 `version` 和 `draft_revision`；`PUT`、`confirm` 必须将两者带回，过期返回 409 后重新读取。首次 `PUT` 使用 `version=0,draft_revision=0`。已确认版本不可原地修改。

M22 提取：复制 `platform-api/.env.example` 为 `platform-api/.env`（被 Git 忽略，权限设为 `0600`），配置 `DEEPSEEK_API_KEY` 和本机适用的 `SSL_CERT_FILE`；启动前加载环境变量。`POST /api/projects/{id}/extract` 携带 `{"version":0}` 和 CSRF 头，最多一次结构纠正重试；`GET /api/projects/{id}/spec` 读取摘要、缺失字段、问题与所有 `pending` 项。模型草稿须由用户编辑确认后，才能 `POST /api/projects/{id}/confirm`。缺少经核对的同币种价目表时，账本保留预留并把成本记为 `UNKNOWN`，不以其他币种价格伪算人民币。

### 前端（platform-ui）

平台 UI 的依赖在 `platform-ui/package-lock.json` 中独立锁定（Vue 3 + TypeScript + Vite + Element Plus），与生成项目 Profile 的版本决定相互独立。

```bash
cd platform-ui
npm ci             # 按 package-lock.json 复现依赖
npm run type-check # vue-tsc --noEmit
npm run build      # vue-tsc + vite build → dist/
npm test           # vitest run（API 客户端、组件、依赖锁定约定）

# 开发（默认 http://localhost:5173，/api 代理到平台 API）
npm run dev        # 需先启动 platform-api：uv run uvicorn ... --port 8000
```

开发态通过 Vite 的 `/api` 代理把浏览器请求转发到平台 API（默认 `http://127.0.0.1:8000`，可用 `AGENTFORGE_API_TARGET` 覆盖），因此会话 Cookie 与 CSRF 双提交按同源处理。平台 UI 需要平台账号登录（`platform-api/config/accounts.json`，见上文）。

页面流程（M25–M27）：登录 → 项目列表 → 创建项目（需求输入）→ 生成需求草稿 → 编辑规格并逐项确认（模型推断一律为“待确认”，需用户显式确认）→ 确认冻结 → 开始生成 → 运行页（轮询阶段/任务/成本/事件，成功可下载 ZIP，失败显示错误与 NOT_RUN 项）。

规格编辑调用 `PUT /api/projects/{id}/spec`（携带 `version`、`draft_revision` 做乐观锁），确认调用 `POST /api/projects/{id}/confirm`；两者过期均返回 `409` 并重新读取。生成调用 `POST /api/projects/{id}/runs`（`Idempotency-Key` 头必需，`spec_id` 须与已确认版本一致），未确认时后端返回 `409 SPEC_NOT_READY`，前端在未确认时禁用“开始生成”。

### 运行查询与产物下载（M27）

- `GET /api/runs/{id}`：运行详情——`status`、`phase`（最近阶段）、`tasks{total,done}`、`cost{settled_micro,reserved_micro,unknown_calls}`（已结算/预留分列）、`artifacts`（含 `kind/sha256/verified`）、终态时的 `verification` 报告（逐项 `PASS/FAIL/NOT_RUN`）与 `error_code`。
- `GET /api/runs/{id}/events?after_sequence=N`：按 `sequence > N` 增量返回事件，`latest_sequence` 便于续读。
- `GET /api/runs/{id}/artifact`：下载本 run 的源码 ZIP。只按 `run_id` + 已记录产物解析路径（拒绝目录穿越/任意文件路径），先校验归属；`X-Artifact-Verified` 标注是否已验证。前端运行页每 2 秒增量轮询，失败退避、后台标签降低频率、终态停止；不展示虚构进度百分比。

### 部署与运维（M28）

`deploy/` 提供平台自身的编排（API + 独立 worker + 维护任务 + UI + 持久化 SQLite 卷）：

```bash
cd deploy && cp .env.example .env   # 填 DEEPSEEK_API_KEY 与 RUNNER_*
docker compose up -d --build        # 迁移由 api 容器启动命令执行；health: /health
```

独立 Runner 部署在受控主机（需 Docker、与 worker 共享运行目录），平台不获得 Docker 权限。
完整手册（含 Runner 连通性、时钟/心跳告警、备份恢复、保留配额）见 **`docs/MVP_DEPLOY.md`**。

- 登录限流：`POST /api/auth/login` 按客户端 IP 滑动窗口限流（`AGENTFORGE_LOGIN_MAX_ATTEMPTS`/`_WINDOW_SECONDS`），超限返回 `429 RATE_LIMITED`。
- 维护任务：`python -m agentforge_platform.maintenance {sessions purge | runs status | runs cleanup | workers check}`。
- 备份/恢复：`scripts/backup.py`（`VACUUM INTO` 一致性快照 + 产物，加密 0600）、`scripts/restore.py`。

## 范围说明

- M01 不接入任何大模型、不包含 Java 代码生成器、不安装 DeepAgent。
- MVP 仅启用 `user_interaction → DeepSeek 官方 API / deepseek-flash` 一条业务模型路由；其余角色仅保留配置占位。
- 生成代码的构建与沙箱执行在 M09 / M16 之后引入。
