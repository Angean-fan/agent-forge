# AgentForge

AI 软件项目生成平台（Demo / MVP 阶段）。目标：把中文自然语言需求，经结构化规格确认后装配为可下载、可继续开发的 Java 全栈工程，并在受控沙箱中完成构建与业务验收。

- 需求依据：《技术文档/Demo需求分析文档》v0.1
- 技术依据：《技术文档/Demo技术文档_修订版_v0.3》
- 分步实施说明：`AgentForge_01_MVP_分步开发.md`
- 当前进度：**M03**（SQLite 数据层 + Alembic 迁移）

## 目录

| 路径 | 说明 | 引入阶段 |
|---|---|---|
| `platform-api/` | 平台后端（Python 3.12 / FastAPI） | M02 ✅ |
| `platform-ui/` | 平台前端（Vue 3 + TS 目标） | M25 |
| `docs/` | 每步实施记录 | M01 |

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

启动 API（默认 http://127.0.0.1:8000，健康检查 `GET /health`）：

```bash
cd platform-api
uv run uvicorn agentforge_platform.main:app --reload
```

统一响应约定：成功 `{"request_id", "data"}`；失败 `{"request_id", "error": {"code", "message", "retryable"}}`；每个响应带 `X-Request-ID` 头（可用同名请求头指定）。

### 前端（platform-ui）

```bash
cd platform-ui
npm test           # 使用 Node 内置 node --test，本步零依赖
```

## 范围说明

- M01 不接入任何大模型、不包含 Java 代码生成器、不安装 DeepAgent。
- MVP 仅启用 `user_interaction → DeepSeek 官方 API / deepseek-flash` 一条业务模型路由；其余角色仅保留配置占位。
- 生成代码的构建与沙箱执行在 M09 / M16 之后引入。
