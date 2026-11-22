# 平台 API / worker 镜像（同一镜像，不同启动命令）。
# 构建上下文为仓库根：docker build -f deploy/api.Dockerfile .
#
# A09：保持与源码树一致的目录深度（/app 为 REPO_ROOT，/app/platform-api 为应用工作目录），
# 同步打包 templates / capability-packs / methods / integration-packs / profiles / schemas
# 只读资产种子目录，确保容器内模板装配与 seed_assets 完整可用。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv

WORKDIR /app

# 打包只读资产种子与契约目录（供 asset_registry / seed_assets / profiles 读取）
COPY templates ./templates
COPY capability-packs ./capability-packs
COPY methods ./methods
COPY integration-packs ./integration-packs
COPY profiles ./profiles
COPY schemas ./schemas
RUN chmod -R a-w /app/templates /app/capability-packs /app/methods /app/integration-packs /app/profiles /app/schemas \
    && mkdir -p /app/.cache/assets

# 安装平台后端（保留 /app/platform-api 深度，确保 Path(__file__).parents[3] == /app）
WORKDIR /app/platform-api
COPY platform-api/pyproject.toml platform-api/uv.lock platform-api/README.md ./
COPY platform-api/src ./src
COPY platform-api/migrations ./migrations
COPY platform-api/alembic.ini ./alembic.ini
COPY platform-api/config/accounts.example.json platform-api/config/models.example.json ./config/
RUN uv sync --locked --no-dev

EXPOSE 8000
# 默认启动 API；worker 服务在 compose 里覆盖 command。
CMD ["sh", "-c", "alembic upgrade head && uvicorn agentforge_platform.main:app --host 0.0.0.0 --port 8000"]
