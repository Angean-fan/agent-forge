"""E08：P1 在线预览的生命周期、授权与隔离（技术文档 §9；需求 FR09）。

- **只为已通过全部代码必需验收**的产物开启（``SUCCEEDED`` 且 ``code_delivery_status=PASS``）；
- **随机授权地址**（不可枚举 token）+ 默认 **15 分钟**到期，到期**自动回收隔离实例**；
- **停止预览不删除源码**：仅回收实例/清理预览数据，ZIP 仍可下载；
- **不向公网任意运行未知代码**：本模块只做确定性授权/生命周期；真实隔离实例的启动归运行时（NOT_RUN，需开发者确认）；
- **不混用生产数据/正式凭据**：预览只用**演示数据**，注入**不含任何秘密**。

本模块确定性、无模型调用、不发起网络；真实预览运行时（隔离容器/实例与网络暴露策略）需开发者确认后接入。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from agentforge_platform.errors import AppException, ErrorCode

PREVIEW_DEFAULT_TTL_SECONDS = 900  # 15 分钟
PREVIEW_STATUS_ACTIVE = "ACTIVE"
PREVIEW_STATUS_STOPPED = "STOPPED"
PREVIEW_STATUS_EXPIRED = "EXPIRED"

ACCESS_OK = "OK"
ACCESS_REJECTED = "REJECTED"
ACCESS_EXPIRED = "EXPIRED"

# 预览一律使用**演示占位值**；绝不使用真实值或生产数据。
_DEMO_PLACEHOLDER = "preview-demo"


@dataclass(frozen=True)
class PreviewSession:
    """一次预览会话（脱敏；不含任何真实凭据）。"""

    run_id: int
    token: str
    created_at: str
    expires_at: str
    status: str = PREVIEW_STATUS_ACTIVE
    demo_data: bool = True
    runtime_attached: bool = False  # 真实隔离实例未接入（NOT_RUN）

    def view(self) -> dict:
        return {
            "run_id": self.run_id,
            "url": authorized_url(self.token),
            "status": self.status,
            "demo_data": self.demo_data,
            "runtime_attached": self.runtime_attached,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def authorized_url(token: str, *, base: str = "/api/preview") -> str:
    return f"{base.rstrip('/')}/{token}"


def preview_eligibility(run_status: str, code_delivery_status: str) -> list[str]:
    """只有**完成且代码交付通过**的产物方可开启预览。"""
    issues: list[str] = []
    if run_status != "SUCCEEDED":
        issues.append(f"产物未完成（状态 {run_status}），不得开启预览")
    elif code_delivery_status != "PASS":
        issues.append(f"代码交付状态为 {code_delivery_status}，未通过必需检查，不得开启预览")
    return issues


def create_preview(
    run_id: int,
    *,
    run_status: str,
    code_delivery_status: str,
    now: datetime | None = None,
    ttl_seconds: int = PREVIEW_DEFAULT_TTL_SECONDS,
    token_factory: Callable[[], str] | None = None,
) -> PreviewSession:
    """创建预览会话；不满足资格即抛 ``PREVIEW_INELIGIBLE``（不向公网运行未验证产物）。"""
    issues = preview_eligibility(run_status, code_delivery_status)
    if issues:
        raise AppException(
            ErrorCode.PREVIEW_INELIGIBLE, "不可开启在线预览：" + "；".join(issues), status_code=409
        )
    moment = now or datetime.now(UTC)
    factory = token_factory or (lambda: secrets.token_urlsafe(32))
    return PreviewSession(
        run_id=run_id,
        token=factory(),
        created_at=_iso(moment),
        expires_at=_iso(moment + timedelta(seconds=ttl_seconds)),
        status=PREVIEW_STATUS_ACTIVE,
    )


def is_expired(session: PreviewSession, *, now: datetime | None = None) -> bool:
    moment = now or datetime.now(UTC)
    return moment >= datetime.fromisoformat(session.expires_at)


def preview_access(session: PreviewSession, token: str, *, now: datetime | None = None) -> str:
    """按 token 授权：``OK`` / ``REJECTED``（token 错或已停止）/ ``EXPIRED``（已到期）。"""
    if session.status != PREVIEW_STATUS_ACTIVE:
        return ACCESS_REJECTED
    if not secrets.compare_digest(session.token, token):
        return ACCESS_REJECTED
    if is_expired(session, now=now):
        return ACCESS_EXPIRED
    return ACCESS_OK


def recycle(session: PreviewSession, *, stopped: bool = False, now: datetime | None = None) -> PreviewSession:
    """回收隔离实例（到期或手动停止）：**只**回收预览实例/数据，**不触碰源码与 ZIP**。"""
    _ = now  # 时间仅用于审计，不影响状态
    return replace(
        session,
        status=PREVIEW_STATUS_STOPPED if stopped else PREVIEW_STATUS_EXPIRED,
        runtime_attached=False,
    )


def build_preview_env(services: Iterable[object]) -> dict[str, str]:
    """预览环境：只产出**演示占位值**，**绝不**包含任何秘密（不混用正式凭据/生产数据）。"""
    env: dict[str, str] = {}
    for service in services:
        for var in getattr(service, "config", ()) or ():
            env_name = getattr(var, "env_name", None)
            if not env_name or getattr(var, "secret", False):
                continue  # 秘密一律不进预览
            env[env_name] = _DEMO_PLACEHOLDER
    return env


def secret_env_names(services: Iterable[object]) -> list[str]:
    """收集声明的秘密变量名（用于隔离校验：预览环境不得出现它们）。"""
    names: list[str] = []
    for service in services:
        for var in getattr(service, "config", ()) or ():
            env_name = getattr(var, "env_name", None)
            if env_name and getattr(var, "secret", False):
                names.append(env_name)
    return names


def isolation_issues(env: Mapping[str, str], secret_names: Iterable[str]) -> list[str]:
    """隔离校验：预览环境中**不得**出现任何秘密变量。"""
    leaked = sorted(name for name in secret_names if name in env)
    return [f"预览环境混入秘密变量（禁止）：{name}" for name in leaked]


__all__ = [
    "ACCESS_EXPIRED",
    "ACCESS_OK",
    "ACCESS_REJECTED",
    "PREVIEW_DEFAULT_TTL_SECONDS",
    "PREVIEW_STATUS_ACTIVE",
    "PREVIEW_STATUS_EXPIRED",
    "PREVIEW_STATUS_STOPPED",
    "PreviewSession",
    "authorized_url",
    "build_preview_env",
    "create_preview",
    "is_expired",
    "isolation_issues",
    "preview_access",
    "preview_eligibility",
    "recycle",
    "secret_env_names",
]
