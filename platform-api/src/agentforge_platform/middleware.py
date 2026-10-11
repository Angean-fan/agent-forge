"""请求 ID 中间件与访问辅助。

统一为每个请求生成或沿用 `X-Request-ID`，写入 `request.state` 并回写响应头，
使成功与失败响应都能携带同一个 request_id。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"
_MAX_REQUEST_ID_LENGTH = 128


def resolve_request_id(incoming: str | None) -> str:
    """优先沿用调用方传入的合法 request id，否则生成新的 uuid4。"""
    if incoming:
        candidate = incoming.strip()
        if 0 < len(candidate) <= _MAX_REQUEST_ID_LENGTH:
            return candidate
    return str(uuid.uuid4())


def get_request_id(request: Request) -> str:
    """读取当前请求的 request id；异常处理器在中间件范围外也能安全获取。"""
    value = getattr(request.state, "request_id", None)
    if isinstance(value, str) and value:
        return value
    return str(uuid.uuid4())


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
