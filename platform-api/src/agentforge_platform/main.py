"""AgentForge 平台 API 入口。

启动：``uv run uvicorn agentforge_platform.main:app --reload``
"""

from __future__ import annotations

from fastapi import FastAPI

from agentforge_platform import __version__
from agentforge_platform.errors import register_exception_handlers
from agentforge_platform.middleware import RequestIdMiddleware
from agentforge_platform.ratelimit import (
    SlidingWindowLimiter,
    login_max_attempts,
    login_window_seconds,
)
from agentforge_platform.routes import router as system_router
from agentforge_platform.routes_auth import router as auth_router
from agentforge_platform.routes_projects import router as projects_router
from agentforge_platform.routes_runs import router as runs_router
from agentforge_platform.routes_spec import router as spec_router


def create_app() -> FastAPI:
    app = FastAPI(title="AgentForge Platform API", version=__version__)
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    # 登录限流器随应用实例创建；测试可用 app.state.login_limiter 替换。
    app.state.login_limiter = SlidingWindowLimiter(
        login_max_attempts(), login_window_seconds()
    )
    app.include_router(system_router)
    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(spec_router)
    app.include_router(runs_router)
    return app


app = create_app()
