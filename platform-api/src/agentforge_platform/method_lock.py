"""R06D：把方法索引接入既有 Resolver/装配器与 Asset Lock 的扩展适配器。

- 先在 MVP 路径上完成 Template/Capability Pack 的锁定（``resolve_assets``），**方法不重复实现
  认证/权限或覆盖原能力**：模板/能力包先占有的 ``provides`` 与 ``owned_paths`` 一律保护；
- 规格通过 ``method_requests``（默认空）显式请求预审核方法单元；经 **Finder 的确定性兼容筛选**
  （Profile/语言框架/依赖/冲突/扩展点/许可证 + 路径不重叠）后才入库；
- **含方法** → 锁升到 ``schema_version=0.2``（``method_units`` 参与哈希、可防篡改）；
  **无方法** → 返回原 0.1 锁，字节与哈希完全不变（旧锁按 0.1 语义重放）；
- 未命中/冲突/路径覆盖 → **结构化缺口**（抛 ``ASSET_CONFLICT``，绝不静默覆盖文件或使用未审核代码）。
"""

from __future__ import annotations

import re
from pathlib import Path

from agentforge_platform.asset_finder import (
    Candidate,
    CapabilityQuery,
    candidate_from_method,
    candidate_rejections,
    derive_capability_query,
)
from agentforge_platform.asset_registry import (
    AssetRegistry,
    default_registry,
    known_capabilities,
    known_extension_points,
)
from agentforge_platform.contract import BusinessContract
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.method_index import (
    MethodRecord,
    index_path,
    methods_root,
    read_meta,
    scan_methods,
)
from agentforge_platform.resolver import (
    LOCK_SCHEMA_VERSION,
    AssetLock,
    LockedAsset,
    lock_hash_of,
    resolve_assets,
)
from agentforge_platform.spec import SoftwareSpecification

LOCK_SCHEMA_METHODS = "0.2"
_NUM = re.compile(r"\d+")


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in _NUM.findall(version))


def _overlaps(left: str, right: str) -> bool:
    a, b = left.rstrip("/"), right.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _resolve_request(
    request: str, by_key: dict[tuple[str, str], MethodRecord], by_id: dict[str, list[MethodRecord]]
) -> MethodRecord | None:
    if "@" in request:
        asset_id, version = request.split("@", 1)
        return by_key.get((asset_id, version))
    matches = by_id.get(request)
    if not matches:
        return None
    return max(matches, key=lambda record: _version_key(record.version))


def _present_business_paths(registry: AssetRegistry, lock: AssetLock) -> list[str]:
    """已占用的**业务路径**（能力包有效路径：``target_dir/owned_path``）。

    模板的 ``owned_paths`` 是宽泛工程根（``backend/`` 等），方法在其下新增文件属正常扩展，
    故不与之比对；方法不得覆盖能力包业务区或其它已选方法——真正的**文件覆盖**由装配器写重拒绝兜底。
    """
    paths: list[str] = []
    for asset in lock.assets:
        if asset.kind != "capability-pack":
            continue
        manifest, _ = registry.get(asset.kind, asset.id, asset.version)
        for owned in manifest.owned_paths:
            paths.append(f"{manifest.target_dir}/{owned}".strip("/") if manifest.target_dir else owned)
    return paths


def resolve_lock(
    registry: AssetRegistry | None,
    profile: str,
    spec: SoftwareSpecification,
    contract: BusinessContract,
    *,
    methods_root_path: str | Path | None = None,
    index_path_override: str | Path | None = None,
) -> AssetLock:
    """按规格装配锁：无 ``method_requests`` 时与 MVP 完全一致；有则升 0.2 并纳入方法单元。"""
    registry = registry or default_registry()
    base = resolve_assets(registry, profile)
    requests = list(spec.method_requests)
    if not requests:
        return base  # MVP 路径：锁与哈希不变

    root = Path(methods_root_path) if methods_root_path is not None else methods_root()
    records = scan_methods(root)
    by_key = {(record.id, record.version): record for record in records}
    by_id: dict[str, list[MethodRecord]] = {}
    for record in records:
        by_id.setdefault(record.id, []).append(record)

    query: CapabilityQuery = derive_capability_query(spec, contract)
    available = known_capabilities(registry, profile)
    present_capabilities = set(query.capability_ids) | available
    registered_extension_points = known_extension_points(registry, profile)
    present_paths = _present_business_paths(registry, base)

    selected: list[MethodRecord] = []
    gaps: list[str] = []
    for request in requests:
        record = _resolve_request(request, by_key, by_id)
        if record is None:
            gaps.append(f"未找到方法：{request}")
            continue
        candidate: Candidate = candidate_from_method(record)
        reasons = candidate_rejections(
            candidate, query,
            available_capabilities=available,
            present_capabilities=present_capabilities,
            registered_extension_points=registered_extension_points,
        )
        for path in record.owned_paths:  # 模板/能力包/已选方法已占用的路径不得被覆盖
            if any(_overlaps(path, taken) for taken in present_paths):
                reasons.append(f"path_overlap:{path}")
        if reasons:
            gaps.append(f"{request}：{'、'.join(reasons)}")
            continue
        present_paths.extend(record.owned_paths)  # 方法之间也不得相互覆盖
        selected.append(record)

    if gaps:
        raise AppException(
            ErrorCode.ASSET_CONFLICT,
            "方法装配存在未满足的缺口：" + "；".join(gaps),
            status_code=422,
        )

    revision: str | None = None
    index = Path(index_path_override) if index_path_override is not None else index_path()
    if index.is_file():
        try:
            revision = read_meta(index).get("index_revision")
        except AppException:
            revision = None

    method_units = [
        LockedAsset(
            kind="method-unit", id=record.id, version=record.version,
            checksum=record.checksum, path=record.asset_path,
        )
        for record in selected
    ]
    lock = AssetLock(
        schema_version=LOCK_SCHEMA_METHODS,
        profile=base.profile,
        mysql_image=base.mysql_image,
        assets=base.assets,
        lock_hash="",
        method_units=method_units,
        index_schema_version="0.1",
        index_revision=revision,
        selection_reason="method_requests=" + ",".join(requests),
    )
    return lock.model_copy(update={"lock_hash": lock_hash_of(lock)})


__all__ = ["LOCK_SCHEMA_METHODS", "LOCK_SCHEMA_VERSION", "resolve_lock"]
