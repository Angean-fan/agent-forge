"""A03：本地只读物化资产缓存（``AssetMaterializer``）。

把数据库 ``asset_release`` + ``asset_file`` 表的资产按需原子物化到本地只读缓存目录
``.cache/assets/<kind>/<id>@<version>/``：
1. 先写出到同级 ``.tmp`` 临时目录并逐文件核验 SHA-256；
2. 核对 ``compute_checksum(tmp_dir) == release.checksum``；
3. ``os.replace`` 原子替换发布，并递归设为只读权限（文件 ``0444``、目录 ``0555``）；
4. 缓存命中且 ``verify_cached_release`` 通过时直接返回本地只读 ``Path``，供 ``assembler.py`` 零改动消费。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from sqlalchemy.orm import Session

from agentforge_platform.asset_registry import (
    REPO_ROOT,
    AssetRegistry,
    compute_checksum,
    is_safe_owned_path,
)
from agentforge_platform.asset_store_pg import PgAssetStore
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.resolver import AssetLock

DEFAULT_CACHE_ROOT = REPO_ROOT / ".cache" / "assets"
_MANIFEST_NAME = "manifest.json"


def default_cache_root() -> Path:
    env_dir = os.environ.get("AGENTFORGE_ASSET_CACHE_DIR", "").strip()
    return Path(env_dir).resolve() if env_dir else DEFAULT_CACHE_ROOT


def _validate_cache_token(token: str, field_name: str) -> str:
    cleaned = token.strip()
    if (
        not cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or ".." in cleaned
        or cleaned.startswith(".")
    ):
        raise AppException(
            ErrorCode.BAD_REQUEST,
            f"非法的资产 {field_name}：{token!r}",
            status_code=422,
        )
    return cleaned


def _validate_rel_path(rel_path: str) -> str:
    if not is_safe_owned_path(rel_path):
        raise AppException(
            ErrorCode.BAD_REQUEST,
            f"资产包含不安全相对路径：{rel_path!r}",
            status_code=422,
        )
    parts = PurePosixPath(rel_path).parts
    if any(p in ("", ".", "..") for p in parts):
        raise AppException(
            ErrorCode.BAD_REQUEST,
            f"资产包含越界路径：{rel_path!r}",
            status_code=422,
        )
    return PurePosixPath(*parts).as_posix()


def _make_readonly_tree(root: Path) -> None:
    """递归将目录树设置为只读（文件 0o444，目录 0o555），防止任何进程或 Agent 原地篡改缓存。"""
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        here = Path(dirpath)
        for fname in filenames:
            fpath = here / fname
            if not fpath.is_symlink():
                fpath.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        if not here.is_symlink():
            here.chmod(
                stat.S_IRUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH
            )


def make_writable_tree(root: Path) -> None:
    """递归恢复目录写权限（仅供清理或重建失效缓存使用）。"""
    if not root.exists():
        return
    for dirpath, _dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        here = Path(dirpath)
        if not here.is_symlink():
            here.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        for fname in filenames:
            fpath = here / fname
            if not fpath.is_symlink():
                fpath.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)


def remove_readonly_tree(root: Path) -> None:
    """安全删除只读缓存目录树。"""
    if not root.exists():
        return
    make_writable_tree(root)
    shutil.rmtree(root)


class AssetMaterializer:
    """从数据库 ``PgAssetStore`` 物化资产到本地只读缓存目录的管理器。"""

    def __init__(
        self,
        store_or_db: PgAssetStore | Session,
        *,
        cache_root: str | Path | None = None,
    ) -> None:
        self.store = (
            store_or_db if isinstance(store_or_db, PgAssetStore) else PgAssetStore(store_or_db)
        )
        self.cache_root = (
            Path(cache_root).resolve() if cache_root is not None else default_cache_root()
        )

    def release_cache_dir(self, kind: str, asset_id: str, version: str) -> Path:
        safe_kind = _validate_cache_token(kind, "kind")
        safe_id = _validate_cache_token(asset_id, "asset_id")
        safe_ver = _validate_cache_token(version, "version")
        return self.cache_root / safe_kind / f"{safe_id}@{safe_ver}"

    def verify_cached_release(
        self,
        kind: str,
        asset_id: str,
        version: str,
        *,
        expected_checksum: str | None = None,
    ) -> bool:
        """校验本地只读缓存目录是否完整且与数据库/manifest checksum 严格一致。"""
        target_dir = self.release_cache_dir(kind, asset_id, version)
        manifest_path = target_dir / _MANIFEST_NAME
        if not target_dir.is_dir() or target_dir.is_symlink() or not manifest_path.is_file():
            return False
        for path in target_dir.rglob("*"):
            if path.is_symlink():
                return False
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False

        manifest_checksum = payload.get("checksum")
        if not isinstance(manifest_checksum, str) or not manifest_checksum:
            return False
        if expected_checksum is None:
            rel = self.store.get_release(kind, asset_id, version)
            if rel is None:
                return False
            expected_checksum = rel.checksum

        if manifest_checksum != expected_checksum:
            return False
        return compute_checksum(target_dir) == expected_checksum

    def materialize_release(self, kind: str, asset_id: str, version: str) -> Path:
        """将指定 ``(kind, asset_id, version)`` 原子物化到只读缓存目录并返回 ``Path``。"""
        release = self.store.get_release(kind, asset_id, version)
        if release is None:
            raise AppException(
                ErrorCode.ASSET_NOT_FOUND,
                f"数据库中不存在已认证资产：{kind}/{asset_id}@{version}",
                status_code=404,
            )

        target_dir = self.release_cache_dir(kind, asset_id, version)
        if self.verify_cached_release(
            kind, asset_id, version, expected_checksum=release.checksum
        ):
            _make_readonly_tree(target_dir)
            return target_dir

        if target_dir.exists():
            remove_readonly_tree(target_dir)

        kind_dir = target_dir.parent
        kind_dir.mkdir(parents=True, exist_ok=True)
        if not os.access(kind_dir, os.W_OK):
            kind_dir.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

        tmp_dir = kind_dir / f".{target_dir.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        if tmp_dir.exists():
            remove_readonly_tree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        try:
            manifest_payload = json.loads(release.manifest_json)
            (tmp_dir / _MANIFEST_NAME).write_text(
                json.dumps(manifest_payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            files = self.store.list_files(release)
            for af in files:
                rel_path = _validate_rel_path(af.rel_path)
                if af.is_binary or af.content_text is None:
                    raise AppException(
                        ErrorCode.ASSET_CONFLICT,
                        f"资产 {kind}/{asset_id}@{version} 包含不可物化的二进制或空文本项：{rel_path}",
                        status_code=409,
                    )
                raw = af.content_text.encode("utf-8")
                actual_sha = hashlib.sha256(raw).hexdigest()
                if actual_sha != af.sha256:
                    raise AppException(
                        ErrorCode.ASSET_CHECKSUM_MISMATCH,
                        f"资产文件 {rel_path} SHA-256 校验失败（expected={af.sha256}, actual={actual_sha}）",
                        status_code=409,
                    )
                dest = (tmp_dir / rel_path).resolve()
                resolved_tmp = tmp_dir.resolve()
                if dest != resolved_tmp and resolved_tmp not in dest.parents:
                    raise AppException(
                        ErrorCode.BAD_REQUEST,
                        f"资产文件路径越界：{rel_path!r}",
                        status_code=422,
                    )
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)

            actual_checksum = compute_checksum(tmp_dir)
            if actual_checksum != release.checksum:
                raise AppException(
                    ErrorCode.ASSET_CHECKSUM_MISMATCH,
                    f"物化缓存 checksum 不匹配：{kind}/{asset_id}@{version} "
                    f"(expected={release.checksum}, actual={actual_checksum})",
                    status_code=409,
                )

            if target_dir.exists():
                remove_readonly_tree(target_dir)
            os.replace(tmp_dir, target_dir)
            _make_readonly_tree(target_dir)
            return target_dir
        except Exception:
            if tmp_dir.exists():
                remove_readonly_tree(tmp_dir)
            raise

    def materialize_lock(self, lock: AssetLock) -> AssetRegistry:
        """物化 ``AssetLock`` 中锁定的全部资产并构造指向本地只读缓存的 :class:`AssetRegistry`。"""
        for item in lock.assets:
            self.materialize_release(item.kind, item.id, item.version)
        for item in lock.method_units:
            self.materialize_release("method-unit", item.id, item.version)
        for item in lock.integration_packs:
            self.materialize_release("integration-pack", item.id, item.version)
        return self.as_registry()

    def materialize_all(
        self, *, kinds: Sequence[str] | None = None
    ) -> AssetRegistry:
        """物化数据库中全部（或指定 kind 的）资产版本并返回 :class:`AssetRegistry`。"""
        target_kinds = set(kinds) if kinds is not None else None
        for rel in self.store.list_releases():
            if target_kinds is None or rel.kind in target_kinds:
                self.materialize_release(rel.kind, rel.asset_id, rel.version)
        return self.as_registry()

    def as_registry(self) -> AssetRegistry:
        """以当前只读缓存目录为根构造 :class:`AssetRegistry`，供 ``assembler.assemble`` 无缝消费。"""
        return AssetRegistry([self.cache_root])

    def clear_cache(self) -> None:
        """清理整个本地资产物化缓存目录。"""
        remove_readonly_tree(self.cache_root)


__all__ = [
    "DEFAULT_CACHE_ROOT",
    "AssetMaterializer",
    "default_cache_root",
    "make_writable_tree",
    "remove_readonly_tree",
]
