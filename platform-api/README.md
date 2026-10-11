# platform-api

AgentForge MVP 的平台后端（Python / FastAPI / Pydantic / SQLAlchemy）。

当前状态：M01，仅包含可安装的包骨架与一个最小测试；FastAPI 应用在 M02 落地。

## 运行

本机已验证 Python 3.12.10（见仓库根 `.python-version`）。

```bash
cd platform-api
uv sync --dev        # 安装依赖并按 uv.lock 复现环境
uv run pytest        # 运行测试
```
