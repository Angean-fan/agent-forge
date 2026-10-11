# 平台 API / worker 镜像（同一镜像，不同启动命令）。
# 构建上下文为仓库根：docker build -f deploy/api.Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY platform-api/pyproject.toml platform-api/uv.lock platform-api/README.md ./
COPY platform-api/src ./src
COPY platform-api/migrations ./migrations
COPY platform-api/alembic.ini ./alembic.ini
RUN uv sync --locked --no-dev

EXPOSE 8000
# 默认启动 API；worker 服务在 compose 里覆盖 command。
CMD ["sh", "-c", "alembic upgrade head && uvicorn agentforge_platform.main:app --host 0.0.0.0 --port 8000"]
