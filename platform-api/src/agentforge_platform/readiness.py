"""E04：区分**代码交付状态**与**外部服务就绪（Mock/Live）**（技术文档 §3/§7）。

- ``code_delivery_status`` 由本次代码的必需检查决定（构建/固定验收/Mock 集成测试）；
- ``external_readiness`` 取 ``NOT_REQUIRED / MOCK_ONLY / AWAITING_USER_CONFIG /
  LIVE_VERIFIED / LIVE_FAILED``；
- **Mock 通过不证明 Live 可用**：已配置但未执行 Live **不得**标 ``LIVE_VERIFIED``；
- 只有规格确认 Live 为**交付必需**且缺配置时，才影响"完整就绪"（``AWAITING_USER_CONFIG``）；
- 代码检查与第三方故障**分别**经 ``code_delivery_status`` 与各服务 ``reason`` 呈现；
- 全程确定性，无模型调用；**无真实密钥**也能在 Mock 下生成与验收。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_NOT_RUN = "NOT_RUN"

CODE_PASS = STATUS_PASS
CODE_FAIL = STATUS_FAIL
CODE_NOT_RUN = STATUS_NOT_RUN

READINESS_NOT_REQUIRED = "NOT_REQUIRED"
READINESS_MOCK_ONLY = "MOCK_ONLY"
READINESS_AWAITING_CONFIG = "AWAITING_USER_CONFIG"
READINESS_LIVE_VERIFIED = "LIVE_VERIFIED"
READINESS_LIVE_FAILED = "LIVE_FAILED"
READINESS_VALUES = (
    READINESS_NOT_REQUIRED,
    READINESS_MOCK_ONLY,
    READINESS_AWAITING_CONFIG,
    READINESS_LIVE_VERIFIED,
    READINESS_LIVE_FAILED,
)

_INFRASTRUCTURE_CODE = "INFRASTRUCTURE"


@dataclass(frozen=True)
class ExternalService:
    """一个外部服务/待办的**脱敏**就绪快照（不含任何真实值，仅布尔/状态）。"""

    service_id: str
    name: str
    mode: str
    required: bool
    required_for_live: bool
    secret: bool
    env_name: str
    config_present: bool
    mock_status: str = STATUS_NOT_RUN
    live_status: str = STATUS_NOT_RUN
    reason: str | None = None


def _modes(raw: object) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        return [raw]
    return ["live"]


def parse_declared_services(
    entries: Iterable[object] | None, *, env: Mapping[str, str] | None = None
) -> list[ExternalService]:
    """从规格冻结的 ``external_requirements`` 解析服务快照（容错，缺字段用安全默认）。

    配置是否就绪只看**环境变量是否存在**（不读取/不暴露其值）：``env`` 缺省为进程环境。
    """
    environ = os.environ if env is None else env
    services: list[ExternalService] = []
    for index, raw in enumerate(entries or []):
        if not isinstance(raw, dict):
            continue
        modes = _modes(raw.get("required_in_modes"))
        env_name = str(raw.get("env_name") or "")
        services.append(
            ExternalService(
                service_id=str(raw.get("requirement_id") or raw.get("service_id") or f"svc-{index}"),
                name=str(raw.get("name") or raw.get("service") or env_name or f"svc-{index}"),
                mode=str(raw.get("mode") or "mock-only"),
                required=bool(raw.get("required", True)),
                required_for_live="live" in modes,
                secret=bool(raw.get("secret", False)),
                env_name=env_name,
                config_present=bool(env_name) and env_name in environ,
            )
        )
    services.sort(key=lambda service: service.service_id)
    return services


def apply_verification_records(
    services: list[ExternalService], records: Iterable[object] | None
) -> list[ExternalService]:
    """合并 verification 报告的逐服务记录（``service_id`` → mock/live 状态与原因）。"""
    by_id = {
        str(record.get("service_id")): record
        for record in (records or [])
        if isinstance(record, dict)
    }
    merged: list[ExternalService] = []
    for service in services:
        record = by_id.get(service.service_id, {})
        merged.append(
            replace(
                service,
                mock_status=str(record.get("mock_status") or service.mock_status),
                live_status=str(record.get("live_status") or service.live_status),
                reason=record.get("reason") or service.reason,
            )
        )
    return merged


def declared_services(
    spec: object, verification: Mapping[str, object] | None = None, *, env: Mapping[str, str] | None = None
) -> list[ExternalService]:
    """规格声明的服务 + verification 记录合并后的就绪快照。"""
    services = parse_declared_services(getattr(spec, "external_requirements", None), env=env)
    if verification:
        services = apply_verification_records(services, verification.get("external_services"))
    return services


def compute_readiness(services: list[ExternalService]) -> str:
    """聚合就绪状态。优先级：无服务 → Live 失败 → 全部 Live 通过 → 缺必需配置 → 仅 Mock。"""
    if not services:
        return READINESS_NOT_REQUIRED
    if any(service.live_status == STATUS_FAIL for service in services):
        return READINESS_LIVE_FAILED
    live_required = [
        service for service in services if service.required and service.required_for_live
    ]
    if live_required and all(service.live_status == STATUS_PASS for service in live_required):
        return READINESS_LIVE_VERIFIED
    if any(service.required and service.required_for_live and not service.config_present
           for service in services):
        return READINESS_AWAITING_CONFIG
    return READINESS_MOCK_ONLY


def code_delivery_status(
    run_status: str, *, error_code: str | None = None
) -> str:
    """代码交付状态：SUCCEEDED→PASS；FAILED→FAIL（**基础设施故障单列 NOT_RUN**，不算代码失败）；在途→NOT_RUN。"""
    if run_status == "SUCCEEDED":
        return CODE_PASS
    if run_status == "FAILED":
        return CODE_NOT_RUN if error_code == _INFRASTRUCTURE_CODE else CODE_FAIL
    return CODE_NOT_RUN


def readiness_notice(readiness: str, services: list[ExternalService]) -> str | None:
    """面向用户的中性提示（确定性模板，不改事实/权限/费用/检查状态）。"""
    missing = [
        service.env_name or service.name
        for service in services
        if service.required and service.required_for_live and not service.config_present
    ]
    if readiness == READINESS_AWAITING_CONFIG:
        return "缺少 Live 必需配置：" + "、".join(missing) + "；Mock 通过不代表 Live 可用。"
    if readiness == READINESS_MOCK_ONLY:
        return "仅 Mock 已验证；Mock 通过不代表 Live 鉴权/网络/回调或厂商审批可用。"
    if readiness == READINESS_LIVE_FAILED:
        return "Live 验证失败：代码交付结果单独保留，请查看各服务的失败原因。"
    if readiness == READINESS_LIVE_VERIFIED:
        return "Live 验证通过（仅表示本次受控验证结果）。"
    return None


__all__ = [
    "CODE_FAIL",
    "CODE_NOT_RUN",
    "CODE_PASS",
    "READINESS_AWAITING_CONFIG",
    "READINESS_LIVE_FAILED",
    "READINESS_LIVE_VERIFIED",
    "READINESS_MOCK_ONLY",
    "READINESS_NOT_REQUIRED",
    "READINESS_VALUES",
    "ExternalService",
    "apply_verification_records",
    "code_delivery_status",
    "compute_readiness",
    "declared_services",
    "parse_declared_services",
    "readiness_notice",
]
