"""最小 Asset Resolver 与锁清单（M14）。

为生成链路按 Profile 选择模板与能力包，在写文件前检测**重复能力、路径冲突、依赖关系**，
并输出稳定的 ``asset-lock.json``（含资产版本与内容 hash）。

LLM 无法直接指定磁盘路径/包地址/依赖版本：选择只依据注册表中的 manifest 与 Profile。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agentforge_platform.asset_registry import (
    REPO_ROOT,
    AssetManifest,
    AssetRegistry,
    default_registry,
)
from agentforge_platform.errors import AppException, ErrorCode

LOCK_SCHEMA_VERSION = "0.1"
# 按 schema_version 指定**参与哈希**的字段白名单。旧版本锁只哈希其发布时的字段集，
# 显式排除后续新增字段（method_units/index_*/selection_reason/integration_packs）的默认值，
# 保证旧锁字节与哈希不变；新版本锁（0.3）按自己的字段集计算。
LOCK_HASH_FIELDS: dict[str, tuple[str, ...]] = {
    "0.1": ("schema_version", "profile", "mysql_image", "assets"),
    "0.2": (
        "schema_version", "profile", "mysql_image", "assets", "method_units",
        "index_schema_version", "index_revision", "selection_reason",
    ),
    "0.3": (
        "schema_version", "profile", "mysql_image", "assets", "integration_packs",
        "selection_reason",
    ),
}
_STRICT = ConfigDict(extra="forbid")


class ImageRef(BaseModel):
    model_config = _STRICT

    reference: str
    digest: str


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
    mysql_image: ImageRef
    assets: list[LockedAsset]
    lock_hash: str
    # R06A 新增（均可选）：方法单元锁与索引/选择审计；0.1 锁哈希不含这些字段。
    method_units: list[LockedAsset] = []
    index_schema_version: str | None = None
    index_revision: str | None = None
    selection_reason: str | None = None
    # E01 新增（可选）：Integration Pack 锁；0.1/0.2 锁哈希白名单不含该字段（旧锁字节不变）。
    integration_packs: list[LockedAsset] = []


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def lock_hash_of(lock: AssetLock) -> str:
    full = lock.model_dump(mode="json")
    fields = LOCK_HASH_FIELDS.get(lock.schema_version)
    if fields is None:
        payload = {key: value for key, value in full.items() if key != "lock_hash"}
    else:
        payload = {key: full[key] for key in fields}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def verify_lock_hash(lock: AssetLock) -> AssetLock:
    """校验锁哈希与内容一致（按 ``schema_version`` 的字段白名单）；不一致即拒绝（疑似被篡改）。"""
    actual = lock_hash_of(lock)
    if actual != lock.lock_hash:
        raise AppException(
            ErrorCode.ASSET_CHECKSUM_MISMATCH,
            "asset-lock 哈希校验未通过（疑似被篡改）。",
            status_code=409,
        )
    return lock


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


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def _latest_by_id(assets: list[AssetManifest]) -> dict[str, AssetManifest]:
    latest: dict[str, AssetManifest] = {}
    for manifest in assets:
        current = latest.get(manifest.id)
        if current is None or _version_key(manifest.version) > _version_key(current.version):
            latest[manifest.id] = manifest
    return latest


def _select(
    assets: list[AssetManifest], pins: dict[str, str] | None
) -> dict[str, AssetManifest]:
    chosen = _latest_by_id(assets)
    if pins:
        for asset_id, version in pins.items():
            pinned = next(
                (m for m in assets if m.id == asset_id and m.version == version), None
            )
            if pinned is not None:
                chosen[asset_id] = pinned
    return chosen


def _mysql_image(template_dir: Path) -> ImageRef:
    compose = template_dir / "compose.yaml"
    if not compose.is_file():
        raise AppException(ErrorCode.ASSET_CONFLICT, "模板缺少 compose.yaml", status_code=422)
    match = re.search(
        r"image:\s*(mysql:[0-9][^\s@]*)@(sha256:[0-9a-f]{64})", compose.read_text("utf-8")
    )
    if match is None:
        raise AppException(
            ErrorCode.ASSET_CONFLICT,
            "compose 的 MySQL 镜像未锁定 digest（不接受可变 tag）",
            status_code=422,
        )
    return ImageRef(reference=match.group(1), digest=match.group(2))


def resolve_assets(
    registry: AssetRegistry, profile: str, *, pins: dict[str, str] | None = None
) -> AssetLock:
    compatible_templates = [
        manifest
        for manifest in registry.all()
        if manifest.kind == "template" and profile in manifest.compatible_profiles
    ]
    templates_by_id = _select(compatible_templates, pins)
    if len(templates_by_id) != 1:
        raise AppException(
            ErrorCode.ASSET_CONFLICT,
            f"Profile '{profile}' 需要恰好 1 个兼容模板 ID，找到 {len(templates_by_id)} 个",
            status_code=422,
        )
    template = next(iter(templates_by_id.values()))
    compatible_packs = [
        manifest
        for manifest in registry.all()
        if manifest.kind == "capability-pack" and profile in manifest.compatible_profiles
    ]
    packs = list(_select(compatible_packs, pins).values())
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

    _, template_dir = registry.get(template.kind, template.id, template.version)
    lock = AssetLock(
        schema_version=LOCK_SCHEMA_VERSION,
        profile=profile,
        mysql_image=_mysql_image(Path(template_dir)),
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
