"""最小 Asset Resolver 与锁清单（M14）。

为生成链路按 Profile 选择模板与能力包，在写文件前检测**重复能力、路径冲突、依赖关系**，
并输出稳定的 ``asset-lock.json``（含资产版本与内容 hash）。

LLM 无法直接指定磁盘路径/包地址/依赖版本：选择只依据注册表中的 manifest 与 Profile。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agentforge_platform.asset_registry import (
    REPO_ROOT,
    AssetRegistry,
    default_registry,
)
from agentforge_platform.errors import AppException, ErrorCode

LOCK_SCHEMA_VERSION = "0.1"
_STRICT = ConfigDict(extra="forbid")


class LockedAsset(BaseModel):
    model_config = _STRICT

    kind: str
    id: str
    version: str
    checksum: str
    path: str


class AssetLock(BaseModel):
    model_config = _STRICT

    schema_version: str
    profile: str
    assets: list[LockedAsset]
    lock_hash: str


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def lock_hash_of(lock: AssetLock) -> str:
    payload = lock.model_dump(mode="json")
    payload.pop("lock_hash", None)
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _asset_path(directory: Path) -> str:
    try:
        return Path(directory).relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return Path(directory).as_posix()


def _overlaps(left: str, right: str) -> bool:
    a = left.rstrip("/")
    b = right.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _require_name(requirement: str) -> str:
    return requirement.split(":", 1)[1] if ":" in requirement else requirement


def resolve_assets(registry: AssetRegistry, profile: str) -> AssetLock:
    templates = [
        manifest
        for manifest in registry.all()
        if manifest.kind == "template" and profile in manifest.compatible_profiles
    ]
    if len(templates) != 1:
        raise AppException(
            ErrorCode.ASSET_CONFLICT,
            f"Profile '{profile}' 需要恰好 1 个兼容模板，找到 {len(templates)} 个",
            status_code=422,
        )
    template = templates[0]
    packs = [
        manifest
        for manifest in registry.all()
        if manifest.kind == "capability-pack" and profile in manifest.compatible_profiles
    ]
    selected = [template, *packs]

    # 1) 重复能力
    seen: dict[str, str] = {}
    for manifest in selected:
        for capability in manifest.provides:
            if capability in seen:
                raise AppException(
                    ErrorCode.ASSET_CONFLICT,
                    f"能力 '{capability}' 被 {seen[capability]} 与 {manifest.id} 重复提供",
                    status_code=422,
                )
            seen[capability] = manifest.id

    # 2) 路径冲突
    for index, left in enumerate(selected):
        for right in selected[index + 1:]:
            for left_path in left.owned_paths:
                for right_path in right.owned_paths:
                    if _overlaps(left_path, right_path):
                        raise AppException(
                            ErrorCode.ASSET_CONFLICT,
                            f"路径冲突：{left.id} 的 '{left_path}' 与 {right.id} 的 '{right_path}'",
                            status_code=422,
                        )

    # 3) 依赖关系（requires 需被模板已提供能力满足）
    provided = set(template.provided_capabilities)
    for manifest in selected:
        for requirement in manifest.requires:
            if _require_name(requirement) not in provided:
                raise AppException(
                    ErrorCode.ASSET_CONFLICT,
                    f"依赖未满足：{manifest.id} 需要 '{requirement}'",
                    status_code=422,
                )

    selected_ids = {(manifest.kind, manifest.id, manifest.version) for manifest in selected}
    locked = [
        LockedAsset(
            kind=manifest.kind,
            id=manifest.id,
            version=manifest.version,
            checksum=manifest.checksum,
            path=_asset_path(directory),
        )
        for manifest, directory in registry.all_with_paths()
        if (manifest.kind, manifest.id, manifest.version) in selected_ids
    ]
    locked.sort(key=lambda asset: (asset.kind, asset.id))

    lock = AssetLock(
        schema_version=LOCK_SCHEMA_VERSION,
        profile=profile,
        assets=locked,
        lock_hash="",
    )
    return lock.model_copy(update={"lock_hash": lock_hash_of(lock)})


def main(argv: list[str]) -> int:
    profile = argv[0] if argv else "demo-ts-mp"
    lock = resolve_assets(default_registry(), profile)
    print(lock.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
