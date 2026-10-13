"""E02：Port/Adapter/Mock 契约链路的确定性校验（技术文档 §6）。

业务 Service **只依赖内部 Port**；Adapter 实现第三方协议；Mock 提供确定性测试行为。
本模块在不编译 Java 的前提下，用**清单声明 + 已发布源码文本**做确定性校验，确保：

- 每个 Port 都有实现它的 Adapter，且至少一个**非 Mock**（协议）Adapter 与**恰好一个** Mock；
- Adapter 不得绑定未声明的 Port；
- Mock ``scenarios`` 覆盖 ``success/failure/timeout``（Mock 下可演示成功/失败/超时）；
- **Port 方法签名只引用内部类型**（供应商 DTO 不得进入 Port → 不传播到核心业务规则）；
- 已发布清单必须携带 Port **契约测试**，且该测试确实断言 Port 与三种场景。

失败原因结构化可追溯；不通过即 :func:`require_valid_integration_contract` 抛 ``ASSET_CONFLICT``。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from agentforge_platform.asset_registry import (
    _ALLOWED_ADAPTER_KINDS,
    _MOCK_ADAPTER_KIND,
    IntegrationPackManifest,
)
from agentforge_platform.errors import AppException, ErrorCode

# Mock 必须覆盖的确定性场景（Mock 模式不依赖用户凭据）。
REQUIRED_MOCK_SCENARIOS = ("failure", "success", "timeout")
_SCENARIO_EXPECTATION = {
    "success": "ok",
    "failure": "typed_error",
    "timeout": "typed_timeout",
}

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Port 允许引用的 JDK/基础类型（非供应商 DTO）；其余标识符必须来自 ``internal_types``。
_JDK_TYPE_TOKENS = frozenset({
    "void", "boolean", "byte", "short", "int", "long", "float", "double", "char",
    "String", "Object", "Boolean", "Integer", "Long", "Double", "BigDecimal",
    "LocalDate", "LocalDateTime", "Collection", "Iterable", "List", "Map", "Set",
    "Optional",
})


@dataclass(frozen=True)
class ContractCase:
    """Port 契约的一条确定性用例（供审计：同一 Port 在 Mock 下演示成功/失败/超时）。"""

    port: str
    scenario: str
    expectation: str


def port_contract_cases(manifest: IntegrationPackManifest) -> list[ContractCase]:
    """从清单确定性派生：每个 Port × 必需场景 → 期望结果（稳定排序）。"""
    cases = [
        ContractCase(port=port.name, scenario=scenario, expectation=_SCENARIO_EXPECTATION[scenario])
        for port in manifest.ports
        for scenario in REQUIRED_MOCK_SCENARIOS
    ]
    cases.sort(key=lambda case: (case.port, case.scenario))
    return cases


def _method_type_expressions(signature: str) -> list[str]:
    """从 Java 方法签名中抽出**类型表达式**（返回类型 + 各参数类型），忽略方法/参数名。"""
    if "(" not in signature or ")" not in signature:
        return []
    head, _, rest = signature.partition("(")
    params, _, _ = rest.partition(")")
    expressions: list[str] = []
    head_parts = head.strip().split()
    if head_parts:  # 返回类型 = 方法名前的部分（含泛型实参）
        expressions.append(head_parts[0])
    for param in params.split(","):
        parts = param.strip().split()
        if parts:
            expressions.append(parts[0])
    return expressions


def _foreign_port_types(manifest: IntegrationPackManifest, methods: list[str]) -> list[str]:
    allowed = set(_JDK_TYPE_TOKENS) | set(manifest.internal_types)
    foreign: set[str] = set()
    for signature in methods:
        for expression in _method_type_expressions(signature):
            for token in _TOKEN.findall(expression):
                if token not in allowed:
                    foreign.add(token)
    return sorted(foreign)


def integration_contract_issues(
    manifest: IntegrationPackManifest, *, pack_dir: str | Path | None = None
) -> list[str]:
    """返回契约链路的拒绝理由（空 = 通过）。``pack_dir`` 提供时进一步核对契约测试文件。"""
    issues: list[str] = []
    port_names = {port.name for port in manifest.ports}

    for adapter in manifest.adapters:
        if adapter.implements and adapter.implements not in port_names:
            issues.append(f"Adapter {adapter.name} 绑定了未声明的 Port：{adapter.implements}")
        if adapter.kind not in _ALLOWED_ADAPTER_KINDS:
            issues.append(f"Adapter {adapter.name} 类型非法：{adapter.kind}")

    for port in manifest.ports:
        impls = [adapter for adapter in manifest.adapters if adapter.implements == port.name]
        mocks = [adapter for adapter in impls if adapter.kind == _MOCK_ADAPTER_KIND]
        protocols = [adapter for adapter in impls if adapter.kind != _MOCK_ADAPTER_KIND]
        if not impls:
            issues.append(f"Port {port.name} 缺少实现 Adapter")
        if not mocks:
            issues.append(f"Port {port.name} 缺少确定性 Mock")
        elif len(mocks) > 1:
            issues.append(f"Port {port.name} 的 Mock 不唯一（{len(mocks)} 个）")
        if not protocols:
            issues.append(f"Port {port.name} 缺少协议 Adapter（仅 Mock，未实现第三方协议）")
        foreign = _foreign_port_types(manifest, port.methods)
        if foreign:
            issues.append(
                f"Port {port.name} 引用了非内部类型（供应商 DTO 不得进入 Port）：{'、'.join(foreign)}"
            )

    missing = sorted(set(REQUIRED_MOCK_SCENARIOS) - set(manifest.mock.scenarios))
    if missing:
        issues.append("Mock 缺少场景：" + "、".join(missing))

    if not manifest.contract_tests:
        issues.append("缺少 Port 契约测试（contract_tests 为空）")
    elif pack_dir is not None:
        root = Path(pack_dir)
        for relative in manifest.contract_tests:
            path = root / relative
            if not path.is_file():
                issues.append(f"契约测试文件不存在：{relative}")
                continue
            text = path.read_text(encoding="utf-8")
            for name in sorted(port_names):
                if name not in text:
                    issues.append(f"契约测试未断言 Port：{name}")
            for scenario in REQUIRED_MOCK_SCENARIOS:
                if scenario not in text:
                    issues.append(f"契约测试未覆盖场景：{scenario}")
    return issues


def require_valid_integration_contract(
    manifest: IntegrationPackManifest, *, pack_dir: str | Path | None = None
) -> IntegrationPackManifest:
    """校验契约链路；不通过则抛 ``ASSET_CONFLICT``（422），理由可追溯。"""
    issues = integration_contract_issues(manifest, pack_dir=pack_dir)
    if issues:
        raise AppException(
            ErrorCode.ASSET_CONFLICT,
            f"Integration Pack {manifest.id}@{manifest.version} 契约未通过：" + "；".join(issues),
            status_code=422,
        )
    return manifest


def main(argv: list[str]) -> int:
    import json

    from agentforge_platform.asset_registry import integration_registry

    asset_id = argv[0] if argv else "weather-lookup"
    registry = integration_registry()
    version = argv[1] if len(argv) > 1 else max(
        (manifest.version for manifest, _ in registry.all_with_paths()
         if manifest.kind == "integration-pack" and manifest.id == asset_id),
        default="",
    )
    manifest, directory = registry.get("integration-pack", asset_id, version)
    cases = port_contract_cases(manifest)
    print(json.dumps(
        {
            "asset": f"{asset_id}@{version}",
            "cases": [case.__dict__ for case in cases],
            "issues": integration_contract_issues(manifest, pack_dir=directory),
        },
        ensure_ascii=False, indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
