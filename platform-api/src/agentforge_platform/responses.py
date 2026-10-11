"""统一响应结构：成功用 ``data``，失败用 ``error``，两者都带 ``request_id``。"""

from __future__ import annotations

from pydantic import BaseModel


class ApiError(BaseModel):
    code: str
    message: str
    retryable: bool


class ApiResponse[T](BaseModel):
    request_id: str
    data: T


class ApiErrorResponse(BaseModel):
    request_id: str
    error: ApiError


class HealthData(BaseModel):
    status: str
    version: str
