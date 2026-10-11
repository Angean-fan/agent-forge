"""AgentForge 平台 API 入口。

启动：``uv run uvicorn agentforge_platform.main:app --reload``
"""

from __future__ import annotations

from fastapi import FastAPI

from agentforge_platform import __version__
from agentforge_platform.errors import register_exception_handlers
from agentforge_platform.middleware import RequestIdMiddleware
from agentforge_platform.routes import router as system_router
from agentforge_platform.routes_auth import router as auth_router


def create_app() -> FastAPI:
    app = FastAPI(title="AgentForge Platform API", version=__version__)
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    app.include_router(system_router)
    app.include_router(auth_router)
    return app


app = create_app()
