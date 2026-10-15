"""C03：Integration Pack 接入生成链路（S1/E03/E04）——**确定性、无模型、无网络**。

在**显式开关**下（``AGENTFORGE_INTEGRATION_PACK_GENERATION_ENABLED``，默认关闭），把
E01/E02/E03 的 Mock-only 集成资产最小接入生成路径：

``冻结 spec →（开关开启且有请求时）集成锁 0.3 → 装配 COPY Port/Adapter/Mock →
生成 .env.example/EXTERNAL_SERVICES.md → 回填 spec.external_requirements → 打包``

约束（对齐《AgentForge_03》C03）：

- 仅对经 Registry/Resolver 校验的 Mock-only Pack 生效；**不绕过、不修改**已发布资产；
- 默认关闭时标准路径**零漂移**（不生成集成锁、不 COPY、不改 spec、不写指南）；
- 外部配置只保留**占位**，秘密留空，**永不写入真实 Key**；未知依赖/密钥/服务不自行下载或请求；
- 真实 Runner 契约测试执行不在本步（本步只保证资产装配与指南交付）。
"""

from __future__ import annotations

import os
from typing import Any

from agentforge_platform.asset_registry import (
    AssetRegistry,
    IntegrationPackManifest,
    integration_registry,
)
from agentforge_platform.external_requirements import ExternalDocs, build_docs, require_clean_docs
from agentforge_platform.integration_lock import resolve_integration_lock
from agentforge_platform.resolver import AssetLock
from agentforge_platform.spec import SoftwareSpecification

FLAG_ENV = "AGENTFORGE_INTEGRATION_PACK_GENERATION_ENABLED"


def integration_generation_enabled() -> bool:
    """集成包接入是否开启（默认关闭 → 普通 Task 产物零漂移）。"""
    return os.environ.get(FLAG_ENV, "").strip().lower() in {"1", "true", "yes"}


def requested_packs(spec: SoftwareSpecification) -> list[str]:
    """解析规格声明的集成包请求：支持 ``"id"`` / ``"id@version"`` 字符串与 ``{"id","version"}`` 映射。"""
    requests: list[str] = []
    for raw in spec.integrations or []:
        if isinstance(raw, str):
            token = raw.strip()
        elif isinstance(raw, dict):
            pack_id = str(raw.get("id") or raw.get("pack") or "").strip()
            version = raw.get("version")
            token = f"{pack_id}@{version}" if pack_id and version else pack_id
        else:
            token = ""
        if token:
            requests.append(token)
    return requests


def manifests_for(
    lock: AssetLock, registry: AssetRegistry | None = None
) -> list[IntegrationPackManifest]:
    """按锁中的 ``integration_packs`` 取回清单（版本/校验以锁为准）。"""
    registry = registry or integration_registry()
    manifests: list[IntegrationPackManifest] = []
    for unit in lock.integration_packs:
        manifest, _directory = registry.get("integration-pack", unit.id, unit.version)
        manifests.append(manifest)
    return manifests


def build_integration_lock(
    spec: SoftwareSpecification,
    *,
    registry: AssetRegistry | None = None,
    base_lock: AssetLock | None = None,
) -> AssetLock | None:
    """开关开启且规格请求了集成包时，返回含 ``integration_packs`` 的 0.3 锁；否则 ``None``。"""
    if not integration_generation_enabled():
        return None
    requests = requested_packs(spec)
    if not requests:
        return None
    return resolve_integration_lock(
        registry or integration_registry(),
        spec.technical_profile.profile_id,
        requests,
        base_lock=base_lock,
    )


def backfill_external_requirements(
    spec: SoftwareSpecification,
    lock: AssetLock,
    registry: AssetRegistry | None = None,
) -> SoftwareSpecification:
    """把集成包 ``config`` 回填进 ``spec.external_requirements``，供装配/打包/就绪使用。

    仅在开关开启时由生成图调用；返回**副本**，不改动冻结的 spec 行。字段名与
    :func:`readiness.parse_declared_services` 对齐（``requirement_id/name/mode/required/
    required_in_modes/secret/env_name`` 等）。
    """
    entries: list[dict[str, Any]] = []
    for manifest in manifests_for(lock, registry):
        for item in manifest.config:
            entry = item.model_dump(mode="json")
            entry.setdefault("mode", manifest.mode)
            entry.setdefault("service", manifest.provider)
            entries.append(entry)
    return spec.model_copy(update={"external_requirements": entries})


def docs_for(
    lock: AssetLock, registry: AssetRegistry | None = None
) -> ExternalDocs | None:
    """生成并校验 ``.env.example``/``EXTERNAL_SERVICES.md``（绑定/秘密扫描不通过即拒）。"""
    manifests = manifests_for(lock, registry)
    if not manifests:
        return None
    return require_clean_docs(build_docs(manifests))


__all__ = [
    "FLAG_ENV",
    "backfill_external_requirements",
    "build_integration_lock",
    "docs_for",
    "integration_generation_enabled",
    "manifests_for",
    "requested_packs",
]
