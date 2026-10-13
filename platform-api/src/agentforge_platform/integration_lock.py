"""E01：Integration Pack 只读清单的 Resolver 锁定（Mock-only）与版本发布策略。

- 复用既有资产寻址/锁定机制（:class:`AssetRegistry` / :class:`AssetLock` / ``lock_hash_of``），
  **不另起一套检索系统**；Integration Pack 只是新增 ``kind``。
- 集成包**不进入**生成默认注册表（``DEFAULT_ROOTS``）：只有显式请求时才由本函数锁定，
  因此无请求时返回与 ``resolve_assets`` **字节/哈希完全一致**的 0.1 锁（MVP 冷启动零漂移）。
- 有请求 → 升 ``schema_version=0.3``，``integration_packs`` 参与哈希、可防篡改。
- 重复请求 / 未知包 / Profile 不兼容 / 冲突 / 缺失依赖 / 路径覆盖 → **结构化错误**，绝不静默补位。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from pathlib import Path

from agentforge_platform.asset_registry import (
    REPO_ROOT,
    AssetRegistry,
    integration_registry,
)
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.integration_contract import integration_contract_issues
from agentforge_platform.resolver import (
    AssetLock,
    LockedAsset,
    lock_hash_of,
    resolve_assets,
)

INTEGRATION_LOCK_SCHEMA_VERSION = "0.3"
_NUM = re.compile(r"\d+")


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in _NUM.findall(version))


def _overlaps(left: str, right: str) -> bool:
    a, b = left.rstrip("/"), right.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _capability_name(requirement: str) -> str:
    return requirement.split(":", 1)[-1]


def _rel_path(directory: Path) -> str:
    try:
        return Path(directory).relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return Path(directory).as_posix()


def _resolve_request(
    request: str, by_key: dict[tuple[str, str], object], by_id: dict[str, list]
):
    if "@" in request:
        asset_id, version = request.split("@", 1)
        return by_key.get((asset_id, version))
    matches = by_id.get(request)
    if not matches:
        return None
    return max(matches, key=lambda manifest: _version_key(manifest.version))


def _present_names(registry: AssetRegistry, base: AssetLock) -> set[str]:
    """已可用能力 = 模板 ``provided_capabilities`` + 已选能力包 ``provides``（依赖判定基准）。"""
    names: set[str] = set()
    for asset in base.assets:
        manifest, _ = registry.get(asset.kind, asset.id, asset.version)
        names.update(manifest.provided_capabilities)
        names.update(manifest.provides)
    return names


def _present_business_paths(registry: AssetRegistry, base: AssetLock) -> list[str]:
    """已占用的**业务路径**（能力包 ``target_dir/owned_path``）。

    模板 ``owned_paths`` 是宽泛工程根（``backend/`` 等），集成包在其下新增模块属正常扩展，
    故不与之比对；集成包不得覆盖能力包业务区或彼此——真正的文件覆盖由后续装配兜底。
    """
    paths: list[str] = []
    for asset in base.assets:
        if asset.kind != "capability-pack":
            continue
        manifest, _ = registry.get(asset.kind, asset.id, asset.version)
        paths.extend(
            f"{manifest.target_dir}/{owned}".strip("/") if manifest.target_dir else owned
            for owned in manifest.owned_paths
        )
    return paths


def resolve_integration_lock(
    registry: AssetRegistry | None,
    profile: str,
    requests: Iterable[str],
    *,
    pins: dict[str, str] | None = None,
) -> AssetLock:
    """按显式请求锁定 Mock-only Integration Pack；无请求时返回 MVP 0.1 锁（不变）。

    ``requests``：条目为 ``<id>`` 或 ``<id>@<version>``；``<id>`` 取该 id 的最高版本。
    ``pins``：可选的 ``{id: version}`` 固定项，语义与 ``resolve_assets`` 一致（复现实验）。
    """
    registry = registry or integration_registry()
    base = resolve_assets(registry, profile, pins=pins)
    requests = list(requests)
    if not requests:
        return base  # MVP 路径：锁与哈希完全不变

    packs = [manifest for manifest in registry.all() if manifest.kind == "integration-pack"]
    by_key = {(manifest.id, manifest.version): manifest for manifest in packs}
    by_id: dict[str, list] = {}
    for manifest in packs:
        by_id.setdefault(manifest.id, []).append(manifest)

    present_names = _present_names(registry, base)
    present_paths = _present_business_paths(registry, base)
    paths_by_key = {
        (manifest.kind, manifest.id, manifest.version): directory
        for manifest, directory in registry.all_with_paths()
    }

    selected: list = []
    seen: set[tuple[str, str]] = set()
    reason_entries: list[str] = []
    for request in requests:
        manifest = _resolve_request(request, by_key, by_id)
        if manifest is None:
            raise AppException(
                ErrorCode.ASSET_NOT_FOUND,
                f"未找到 Integration Pack：{request}",
                status_code=404,
            )
        if (manifest.id, manifest.version) in seen:
            raise AppException(
                ErrorCode.ASSET_CONFLICT,
                f"重复请求 Integration Pack：{request}",
                status_code=422,
            )
        if profile not in manifest.compatible_profiles:
            raise AppException(
                ErrorCode.ASSET_INCOMPATIBLE,
                f"Integration Pack {manifest.id}@{manifest.version} 与 Profile '{profile}' 不兼容",
                status_code=422,
            )

        reasons: list[str] = []
        for requirement in manifest.requires:  # 缺失依赖
            if _capability_name(requirement) not in present_names:
                reasons.append(f"缺失依赖：{requirement}")
        duplicated = sorted(set(manifest.provides) & present_names)  # 能力重复（含与已选集成包）
        if duplicated:
            reasons.append("能力重复：" + "、".join(duplicated))
        conflicts = sorted(set(manifest.conflicts) & present_names)  # 显式冲突
        if conflicts:
            reasons.append("冲突：" + "、".join(conflicts))
        for path in manifest.effective_paths:  # 路径覆盖
            if any(_overlaps(path, taken) for taken in present_paths):
                reasons.append(f"路径覆盖：{path}")
        if manifest.contract_tests:  # E02：声明契约链路的清单在锁定时必须通过确定性契约校验
            reasons.extend(
                integration_contract_issues(
                    manifest, pack_dir=paths_by_key[("integration-pack", manifest.id, manifest.version)]
                )
            )
        if reasons:
            raise AppException(
                ErrorCode.ASSET_CONFLICT,
                f"Integration Pack {request} 未通过：" + "；".join(reasons),
                status_code=422,
            )

        seen.add((manifest.id, manifest.version))
        present_names.update(manifest.provides)
        present_paths.extend(manifest.effective_paths)
        reason_entries.append(f"{manifest.id}@{manifest.version}")
        selected.append(manifest)

    units = [
        LockedAsset(
            kind="integration-pack",
            id=manifest.id,
            version=manifest.version,
            checksum=manifest.checksum,
            path=_rel_path(paths_by_key[("integration-pack", manifest.id, manifest.version)]),
        )
        for manifest in selected
    ]
    units.sort(key=lambda unit: (unit.id, unit.version))

    lock = AssetLock(
        schema_version=INTEGRATION_LOCK_SCHEMA_VERSION,
        profile=base.profile,
        mysql_image=base.mysql_image,
        assets=base.assets,
        lock_hash="",
        integration_packs=units,
        selection_reason="integration_packs=" + ",".join(reason_entries),
    )
    return lock.model_copy(update={"lock_hash": lock_hash_of(lock)})


def main(argv: list[str]) -> int:
    profile = argv[0] if argv else "demo-ts-mp"
    lock = resolve_integration_lock(integration_registry(), profile, argv[1:])
    print(lock.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
