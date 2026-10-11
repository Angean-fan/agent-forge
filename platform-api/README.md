# platform-api

AgentForge MVP 的平台后端（Python / FastAPI / Pydantic / SQLAlchemy）。

当前状态：M07，新增规格草稿编辑与确认冻结 API。

## 数据库

```bash
uv run alembic upgrade head    # 应用迁移（空库可执行）
uv run alembic current         # 查看当前版本
uv run alembic revision -m "..." --autogenerate   # 生成新迁移
```

连接串：`AGENTFORGE_DATABASE_URL`（默认 `sqlite+pysqlite:///./agentforge.db`，WAL）。平台元数据用 SQLite；生成项目用独立 MySQL，两者不可复用。

## 运行

本机已验证 Python 3.12.10（见仓库根 `.python-version`）。

```bash
cd platform-api
uv sync --dev        # 安装依赖并按 uv.lock 复现环境
uv run pytest        # 运行测试
uv run ruff check    # 静态检查
uv run uvicorn agentforge_platform.main:app --reload   # 启动 API
```

## 模块

- `main.py` — `create_app()` 应用工厂与 `app` 实例
- `routes.py` — 系统级路由（`GET /health`）
- `middleware.py` — `X-Request-ID` 生成/沿用与 `request.state` 传播
- `responses.py` — `ApiResponse` / `ApiErrorResponse` / `ApiError` 统一信封
- `errors.py` — `ErrorCode`、`AppException` 与异常处理器
- `db.py` — SQLite 引擎（WAL）、会话工厂、`session_scope` 短事务与 `get_db` 依赖
- `models.py` — `Project` / `SpecVersion` / `PlatformSession`
- `routes_projects.py` — `/api/projects` 创建/列表/详情（保存原始需求文本）
- `spec.py` — `SoftwareSpecification` schema、来源标注与 `is_confirmable` 校验
- `spec_service.py` — 规格草稿读写/确认冻结与稳定 hash
- `routes_spec.py` — `/api/projects/{id}/spec`、`/api/projects/{id}/confirm`
- `accounts.py` — 预置平台账号加载（JSON，仅存哈希）
- `passwords.py` — scrypt 密码哈希/校验 + 生成 CLI
- `auth.py` — 会话存储、`get_current_account` / `require_csrf` / `require_project_owner` 依赖
- `routes_auth.py` — `/api/auth/login` `/api/auth/me` `/api/auth/logout`
- `../migrations/` — Alembic 迁移（`0001` project/spec_version，`0002` platform_session）

## 预置账号

复制 `config/accounts.example.json` → `config/accounts.json`，用
`uv run python -m agentforge_platform.passwords` 生成密码哈希后填入。平台账号与
生成项目内的独立登录账号是两套体系，不可复用。
