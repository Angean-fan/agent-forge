"""认证路由：预置账号登录、当前账号、登出。不做注册/找回。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agentforge_platform.accounts import find_by_username, get_accounts
from agentforge_platform.auth import (
    clear_session_cookies,
    create_session,
    delete_session,
    get_current_account,
    get_session_row,
    require_csrf,
    set_session_cookies,
)
from agentforge_platform.db import get_db
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.middleware import get_request_id
from agentforge_platform.models import PlatformSession
from agentforge_platform.passwords import verify_password
from agentforge_platform.responses import ApiResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class AccountInfo(BaseModel):
    id: int
    username: str


class LoginData(BaseModel):
    account: AccountInfo


class LogoutData(BaseModel):
    ok: bool


def _account_info(account_id: int, username: str) -> AccountInfo:
    return AccountInfo(id=account_id, username=username)


@router.post("/login", response_model=ApiResponse[LoginData])
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> ApiResponse[LoginData]:
    limiter = request.app.state.login_limiter
    client_ip = request.client.host if request.client else "unknown"
    if not limiter.allow(client_ip):
        raise AppException(
            ErrorCode.RATE_LIMITED,
            "登录尝试过于频繁，请稍后再试。",
            status_code=429,
            retryable=True,
        )

    account = find_by_username(get_accounts(), payload.username)
    if (
        account is None
        or not account.is_active
        or not verify_password(payload.password, account.password_hash)
    ):
        raise AppException(ErrorCode.UNAUTHORIZED, "用户名或密码错误。", status_code=401)

    raw_token, csrf_token, expires_at = create_session(db, account.id)
    set_session_cookies(response, raw_token, csrf_token, expires_at)
    return ApiResponse(
        request_id=get_request_id(request),
        data=LoginData(account=_account_info(account.id, account.username)),
    )


@router.get("/me", response_model=ApiResponse[LoginData])
def me(request: Request, account=Depends(get_current_account)) -> ApiResponse[LoginData]:
    return ApiResponse(
        request_id=get_request_id(request),
        data=LoginData(account=_account_info(account.id, account.username)),
    )


@router.post("/logout", response_model=ApiResponse[LogoutData])
def logout(
    request: Request,
    response: Response,
    session_row: PlatformSession = Depends(get_session_row),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApiResponse[LogoutData]:
    delete_session(db, session_row)
    clear_session_cookies(response)
    return ApiResponse(request_id=get_request_id(request), data=LogoutData(ok=True))
