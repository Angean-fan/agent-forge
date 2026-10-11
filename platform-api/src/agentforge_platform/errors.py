"""统一错误码、领域异常与异常处理器。

对外错误响应只暴露 ``code/message/retryable``；未预期异常返回通用消息，
不把堆栈或环境变量写进响应体。
"""

from __future__ import annotations

import logging
from enum import StrEnum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from agentforge_platform.middleware import REQUEST_ID_HEADER, get_request_id
from agentforge_platform.responses import ApiError, ApiErrorResponse

logger = logging.getLogger("agentforge.platform")


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    BAD_REQUEST = "BAD_REQUEST"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    CSRF_INVALID = "CSRF_INVALID"
    RATE_LIMITED = "RATE_LIMITED"
    SPEC_CONFLICT = "SPEC_CONFLICT"
    SPEC_NOT_FOUND = "SPEC_NOT_FOUND"
    SPEC_NOT_READY = "SPEC_NOT_READY"
    CONTRACT_UNSUPPORTED = "CONTRACT_UNSUPPORTED"
    CONTRACT_CONFLICT = "CONTRACT_CONFLICT"
    ASSET_NOT_FOUND = "ASSET_NOT_FOUND"
    ASSET_INCOMPATIBLE = "ASSET_INCOMPATIBLE"
    ASSET_CONFLICT = "ASSET_CONFLICT"
    ASSET_CHECKSUM_MISMATCH = "ASSET_CHECKSUM_MISMATCH"
    ASSEMBLY_ERROR = "ASSEMBLY_ERROR"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    RUN_INVALID_TRANSITION = "RUN_INVALID_TRANSITION"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    STORAGE_QUOTA_EXCEEDED = "STORAGE_QUOTA_EXCEEDED"
    MODEL_ROLE_DISABLED = "MODEL_ROLE_DISABLED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    DRAFT_LIMIT_EXCEEDED = "DRAFT_LIMIT_EXCEEDED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    EXTRACTION_INVALID = "EXTRACTION_INVALID"


class AppException(Exception):
    """业务/领域错误，由处理器转为统一失败响应。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
) -> JSONResponse:
    request_id = get_request_id(request)
    body = ApiErrorResponse(
        request_id=request_id,
        error=ApiError(code=code, message=message, retryable=retryable),
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(),
        headers={REQUEST_ID_HEADER: request_id},
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def _handle_app_exception(request: Request, exc: AppException) -> JSONResponse:
        return error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            request,
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message="Request validation failed.",
            retryable=False,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.BAD_REQUEST
        return error_response(
            request,
            status_code=exc.status_code,
            code=code,
            message=str(exc.detail),
            retryable=False,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception while processing request")
        return error_response(
            request,
            status_code=500,
            code=ErrorCode.INTERNAL_ERROR,
            message="Internal server error.",
            retryable=True,
        )
