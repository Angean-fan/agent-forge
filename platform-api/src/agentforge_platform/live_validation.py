"""E05：有限 Live 沙箱验证的**安全前置**与**脱敏结果**（技术文档 §6/§7）。

本模块只做**确定性、可审计**的部分，**不发起任何网络请求**：

- 平台**不收集**真实第三方密钥，也不把密钥交给模型：注入计划只列**变量名**，绝不含值；
- Live 启动前必须通过 :func:`live_preflight`：所需配置齐全 + 集成**声明可 Live** +
  **精确域名/端口**已声明 + 有真实副作用的服务经**人工批准**；任一不满足 → :func:`require_live_preflight`
  抛 ``LIVE_NOT_READY``（**明确阻止**，不静默回退 Mock）；
- Live 与普通 Mock 测试**分开**；结果按服务记录 mode/required/status/reason/环境/包版本/耗时/证据（**脱敏**）；
- 失败**分类**：鉴权失败 / 网络不通 / 超时 / 供应商错误 / 业务错误分别区分；凭据**不泄漏**。

真实 Live 执行（提供商沙箱网络、注入秘密、允许域名/端口的 Runner 权限、副作用人工批准机制）
需开发者确认后接入，见 ``docs/E05_…记录.md``；本步不含真实调用。
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from agentforge_platform.errors import AppException, ErrorCode

LIVE_MODE = "live"

OUTCOME_OK = "OK"
OUTCOME_AUTH = "AUTH"
OUTCOME_NETWORK = "NETWORK"
OUTCOME_TIMEOUT = "TIMEOUT"
OUTCOME_PROVIDER = "PROVIDER"
OUTCOME_BUSINESS = "BUSINESS"
OUTCOME_VALUES = (
    OUTCOME_OK, OUTCOME_AUTH, OUTCOME_NETWORK, OUTCOME_TIMEOUT, OUTCOME_PROVIDER, OUTCOME_BUSINESS,
)

# 确定性、脱敏的原因文案（不含任何凭据；不改事实/权限/费用/检查状态）。
_OUTCOME_REASON = {
    OUTCOME_OK: "Live 契约用例通过。",
    OUTCOME_AUTH: "供应商鉴权失败：凭据无效或权限不足。",
    OUTCOME_NETWORK: "网络不通：无法连接允许域名/端口（连接或 DNS 失败）。",
    OUTCOME_TIMEOUT: "调用超时：在允许时限内未返回。",
    OUTCOME_PROVIDER: "供应商侧错误：服务不可用或返回 5xx。",
    OUTCOME_BUSINESS: "业务错误：请求被供应商拒绝（非鉴权/网络问题）。",
}

# 允许清单只接受**精确**域名（无通配）与端口；避免"开放任意网络以兼容陌生 API"。
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+\Z")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")
_AWS = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_SECRET_ASSIGN = re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PRIVATE)[A-Z0-9_]*)\s*[:=]\s*(\S+)")


@dataclass(frozen=True)
class LiveConfigVar:
    """Live 必需的一个配置变量（只记名与是否秘密，**不含值**）。"""

    env_name: str
    secret: bool = False


@dataclass(frozen=True)
class LiveService:
    """一个需要 Live 验证的外部服务（脱敏元数据）。"""

    service_id: str
    name: str
    provider: str
    live_capable: bool
    required_for_live: bool
    config: tuple[LiveConfigVar, ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    allowed_ports: tuple[int, ...] = ()
    side_effects: tuple[str, ...] = ()
    requires_approval: bool = False
    approved: bool = False


@dataclass(frozen=True)
class LivePreflight:
    """Live 启动前置结论（确定性）；``issues`` 非空即**阻止**启动。"""

    allowed: bool
    issues: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LiveInjectionPlan:
    """最小注入计划：只列该 Adapter **必需**的变量名与副作用，**绝不含值**。"""

    env_names: tuple[str, ...]
    secret_env_names: tuple[str, ...]
    side_effects: tuple[str, ...]


def _modes(raw: object) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        return [raw]
    return ["live"]


def _config_vars(raw: dict) -> tuple[LiveConfigVar, ...]:
    entries = raw.get("config")
    if isinstance(entries, list) and entries:
        return tuple(
            LiveConfigVar(env_name=str(item.get("env_name")), secret=bool(item.get("secret", False)))
            for item in entries
            if isinstance(item, dict) and item.get("env_name")
        )
    env_name = raw.get("env_name")
    return (LiveConfigVar(env_name=str(env_name), secret=bool(raw.get("secret", False))),) if env_name else ()


def parse_live_services(
    entries: Iterable[object] | None, *, env: Mapping[str, str] | None = None
) -> list[LiveService]:
    """从规格冻结的 ``external_requirements`` 解析 Live 服务（按 ``service``/``provider`` 归组）。

    只用于本模块的前置与规划；缺字段用安全默认（``live_capable`` 默认 **False**，即不擅自开 Live）。
    """
    _environ = os.environ if env is None else env
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for index, raw in enumerate(entries or []):
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("service") or raw.get("provider") or raw.get("requirement_id") or f"svc-{index}")
        if key not in grouped:
            grouped[key] = {"raw": raw, "config": []}
            order.append(key)
        grouped[key]["config"].extend(_config_vars(raw))

    services: list[LiveService] = []
    for key in order:
        raw = grouped[key]["raw"]
        modes = _modes(raw.get("required_in_modes"))
        config = tuple(dict.fromkeys(grouped[key]["config"]))
        services.append(
            LiveService(
                service_id=str(raw.get("requirement_id") or raw.get("service") or key),
                name=str(raw.get("name") or key),
                provider=str(raw.get("provider") or key),
                live_capable=bool(raw.get("live_capable", False)),
                required_for_live="live" in modes,
                config=config,
                allowed_hosts=tuple(str(host) for host in (raw.get("allowed_hosts") or [])),
                allowed_ports=tuple(int(port) for port in (raw.get("allowed_ports") or [])),
                side_effects=tuple(str(effect) for effect in (raw.get("side_effects") or [])),
                requires_approval=bool(raw.get("requires_approval", bool(raw.get("side_effects")))),
                approved=bool(raw.get("approved", False)),
            )
        )
    return services


def live_preflight(
    services: list[LiveService], *, env: Mapping[str, str] | None = None
) -> LivePreflight:
    """确定性前置：**不可猜测安全配置**——域名/端口须已声明、缺配置即阻止、副作用须已批准。"""
    environ = os.environ if env is None else env
    issues: list[str] = []
    for service in services:
        if not service.required_for_live:
            continue
        if not service.live_capable:
            issues.append(f"{service.name} 未声明支持 Live（仅 Mock），拒绝启动 Live")
        for var in service.config:
            if var.env_name not in environ:
                issues.append(f"缺少必要配置：{var.env_name}（{service.name}）")
        if not service.allowed_hosts:
            issues.append(f"{service.name} 未声明允许域名（Live 须精确放行域名/端口）")
        elif any(_HOST.fullmatch(host) is None for host in service.allowed_hosts):
            issues.append(f"{service.name} 的允许域名非法（须为精确主机名，不允许通配）")
        if not service.allowed_ports:
            issues.append(f"{service.name} 未声明允许端口")
        if service.requires_approval and service.side_effects and not service.approved:
            issues.append(
                f"{service.name} 存在真实副作用需人工批准：{'、'.join(service.side_effects)}"
            )
    return LivePreflight(allowed=not issues, issues=issues)


def require_live_preflight(
    services: list[LiveService], *, env: Mapping[str, str] | None = None
) -> LivePreflight:
    """未满足前置则抛 ``LIVE_NOT_READY``（422），**明确阻止 Live 启动**。"""
    preflight = live_preflight(services, env=env)
    if not preflight.allowed:
        raise AppException(
            ErrorCode.LIVE_NOT_READY,
            "Live 验证前置未满足：" + "；".join(preflight.issues),
            status_code=422,
        )
    return preflight


def injection_plan(services: list[LiveService]) -> LiveInjectionPlan:
    """最小注入计划：只含 Live 必需变量**名**（秘密与非秘密分列）；**绝不含值**。"""
    env_names: list[str] = []
    secret_names: list[str] = []
    side_effects: list[str] = []
    for service in services:
        if not service.required_for_live:
            continue
        for var in service.config:
            env_names.append(var.env_name)
            if var.secret:
                secret_names.append(var.env_name)
        side_effects.extend(service.side_effects)
    return LiveInjectionPlan(
        env_names=tuple(dict.fromkeys(env_names)),
        secret_env_names=tuple(dict.fromkeys(secret_names)),
        side_effects=tuple(dict.fromkeys(side_effects)),
    )


def classify_live_outcome(*, status_code: int | None = None, error: str | None = None) -> str:
    """把 Live 结果归入确定性分类（鉴权/网络/超时/供应商/业务/通过）。"""
    if error:
        lowered = error.lower()
        if "timeout" in lowered or "timed out" in lowered:
            return OUTCOME_TIMEOUT
        if any(token in lowered for token in ("connection", "network", "dns", "unreachable")):
            return OUTCOME_NETWORK
    if status_code is None:
        return OUTCOME_NETWORK if error else OUTCOME_OK
    if status_code in (401, 403):
        return OUTCOME_AUTH
    if status_code == 408 or status_code == 504:
        return OUTCOME_TIMEOUT
    if 200 <= status_code < 300:
        return OUTCOME_OK
    if status_code >= 500:
        return OUTCOME_PROVIDER
    return OUTCOME_BUSINESS


def redact_secrets(text: str, secrets: Iterable[str] = ()) -> str:
    """脱敏：先按已知秘密值精确替换，再按模式（PEM/JWT/AWS/秘密赋值）兜底。"""
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***")
    redacted = _PEM.sub("-----BEGIN *** PRIVATE KEY-----", redacted)
    redacted = _JWT.sub("***", redacted)
    redacted = _AWS.sub("***", redacted)
    redacted = _SECRET_ASSIGN.sub(lambda match: f"{match.group(1)}=***", redacted)
    return redacted


def build_live_record(
    service: LiveService,
    outcome: str,
    *,
    environment: str,
    package_version: str,
    duration_ms: int,
    evidence: str | None = None,
    secrets: Iterable[str] = (),
) -> dict:
    """按服务构造**脱敏** Live 结果记录（供写入 verification 的 ``external_services``）。"""
    if outcome not in OUTCOME_VALUES:
        outcome = OUTCOME_BUSINESS
    passed = outcome == OUTCOME_OK
    return {
        "service_id": service.service_id,
        "mode": LIVE_MODE,
        "required": service.required_for_live,
        "status": "PASS" if passed else "FAIL",
        "mock_status": None,
        "live_status": "PASS" if passed else "FAIL",
        "outcome": outcome,
        "reason": _OUTCOME_REASON[outcome],
        "environment": environment,
        "package_version": package_version,
        "duration_ms": duration_ms,
        "evidence": redact_secrets(evidence, secrets) if evidence else None,
    }


def merge_live_records(report: dict | None, records: Iterable[dict]) -> dict:
    """把 Live 记录合并进 verification 报告的 ``external_services``（按 ``service_id`` 覆盖/追加）。

    合并后的报告可被 E04 的就绪派生消费；**不带任何凭据**。
    """
    merged = dict(report or {})
    existing = {
        str(item.get("service_id")): dict(item)
        for item in (merged.get("external_services") or [])
        if isinstance(item, dict)
    }
    for record in records:
        service_id = str(record.get("service_id"))
        entry = dict(existing.get(service_id, {}))
        entry.update(record)
        existing[service_id] = entry
    merged["external_services"] = [existing[key] for key in sorted(existing)]
    return merged


def main(argv: list[str]) -> int:
    """CLI：对一个规格文件的 Live 前置做**只读**检查（输出变量名，不含值）。"""
    import json
    from pathlib import Path

    from agentforge_platform.spec import SoftwareSpecification

    spec_path = Path(argv[0]) if argv else None
    if spec_path is None:
        print("usage: python -m agentforge_platform.live_validation <spec.json>", file=sys.stderr)
        return 2
    spec = SoftwareSpecification.model_validate_json(spec_path.read_text(encoding="utf-8"))
    services = parse_live_services(getattr(spec, "external_requirements", []))
    preflight = live_preflight(services)
    plan = injection_plan(services)
    print(json.dumps(
        {
            "allowed": preflight.allowed,
            "issues": preflight.issues,
            "services": [service.service_id for service in services],
            "injection_env_names": list(plan.env_names),
            "secret_env_names": list(plan.secret_env_names),
            "side_effects": list(plan.side_effects),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0 if preflight.allowed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
