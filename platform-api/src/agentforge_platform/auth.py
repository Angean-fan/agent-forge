"""会话存储、认证依赖与项目归属校验。

- 会话为服务端存储（SQLite），Cookie 仅携带随机令牌，DB 只存其哈希；
- HttpOnly 会话 Cookie + 可读的 CSRF Cookie（双提交），状态变更方法校验 CSRF；
- 归属校验在服务端执行，前端隐藏按钮不能替代授权。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Request, Response
from sqlalchemy.orm import Session

from agentforge_platform.accounts import Account, find_by_id, get_accounts
from agentforge_platform.db import get_db
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.models import PlatformSession, Project

SESSION_COOKIE = "af_session"
CSRF_COOKIE = "af_csrf"
CSRF_HEADER = "X-CSRF-Token"
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_DEFAULT_TTL_SECONDS = 12 * 3600


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _session_ttl_seconds() -> int:
    return int(os.environ.get("AGENTFORGE_SESSION_TTL_SECONDS", str(_DEFAULT_TTL_SECONDS)))


def _cookie_secure() -> bool:
    return os.environ.get("AGENTFORGE_COOKIE_SECURE", "").lower() in {"1", "true", "yes", "on"}


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(
    db: Session, account_id: int, *, ttl_seconds: int | None = None
) -> tuple[str, str, datetime]:
    """创建会话，返回 (原始令牌, CSRF 令牌, 过期时间)。"""
    raw_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = _utcnow()
    ttl = _session_ttl_seconds() if ttl_seconds is None else ttl_seconds
    row = PlatformSession(
        token_hash=_token_hash(raw_token),
        account_id=account_id,
        csrf_token=csrf_token,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl),
    )
    db.add(row)
    db.commit()
    return raw_token, csrf_token, row.expires_at


def lookup_session(db: Session, raw_token: str) -> PlatformSession | None:
    row = db.get(PlatformSession, _token_hash(raw_token))
    if row is None or row.expires_at <= _utcnow():
        return None
    return row


def delete_session(db: Session, row: PlatformSession) -> None:
    db.delete(row)
    db.commit()


def set_session_cookies(
    response: Response, raw_token: str, csrf_token: str, expires_at: datetime
) -> None:
    max_age = max(0, int((expires_at - _utcnow()).total_seconds()))
    secure = _cookie_secure()
    response.set_cookie(
        SESSION_COOKIE, raw_token, max_age=max_age, httponly=True, samesite="lax",
        secure=secure, path="/",
    )
    response.set_cookie(
        CSRF_COOKIE, csrf_token, max_age=max_age, httponly=False, samesite="lax",
        secure=secure, path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def get_session_row(request: Request, db: Session = Depends(get_db)) -> PlatformSession:
    raw_token = request.cookies.get(SESSION_COOKIE)
    if not raw_token:
        raise AppException(ErrorCode.UNAUTHORIZED, "未登录。", status_code=401)
    row = lookup_session(db, raw_token)
    if row is None:
        raise AppException(ErrorCode.UNAUTHORIZED, "会话无效或已过期。", status_code=401)
    return row


def get_current_account(session_row: PlatformSession = Depends(get_session_row)) -> Account:
    account = find_by_id(get_accounts(), session_row.account_id)
    if account is None or not account.is_active:
        raise AppException(ErrorCode.UNAUTHORIZED, "账号不可用。", status_code=401)
    return account


def require_csrf(request: Request, session_row: PlatformSession = Depends(get_session_row)) -> None:
    """状态变更方法必须携带与会话匹配的 ``X-CSRF-Token``。"""
    if request.method.upper() not in _UNSAFE_METHODS:
        return
    provided = request.headers.get(CSRF_HEADER, "")
    if not provided or not hmac.compare_digest(provided, session_row.csrf_token):
        raise AppException(ErrorCode.CSRF_INVALID, "缺少或无效的 CSRF 令牌。", status_code=403)


def require_project_owner(
    project_id: int,
    account: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> Project:
    """加载项目并校验归属；不存在 404，非本人 403。"""
    project = db.get(Project, project_id)
    if project is None:
        raise AppException(ErrorCode.NOT_FOUND, "项目不存在。", status_code=404)
    if project.owner_id != account.id:
        raise AppException(ErrorCode.FORBIDDEN, "无权访问该项目。", status_code=403)
    return project
