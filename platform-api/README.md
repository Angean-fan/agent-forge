# platform-api

AgentForge MVP 的平台后端（Python / FastAPI / Pydantic / SQLAlchemy）。

当前状态：M02，FastAPI 应用骨架 + 统一响应/错误结构 + 请求 ID + `/health`。

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
