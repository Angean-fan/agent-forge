"""平台系统级路由（M02：健康检查）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from agentforge_platform import __version__
from agentforge_platform.middleware import get_request_id
from agentforge_platform.responses import ApiResponse, HealthData

router = APIRouter()


@router.get("/health", response_model=ApiResponse[HealthData], tags=["system"])
def health(request: Request) -> ApiResponse[HealthData]:
    return ApiResponse(
        request_id=get_request_id(request),
        data=HealthData(status="ok", version=__version__),
    )
