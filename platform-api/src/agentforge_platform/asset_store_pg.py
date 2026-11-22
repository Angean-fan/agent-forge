"""A02：PostgreSQL 逐文件资产存储（``asset_release`` + ``asset_file``）与同步/检索服务。

支持：
1. 从本地 Git 仓库（``templates/``、``capability-packs/``、``methods/``、``integration-packs/``）
   幂等同步资产版本与逐文件源码入库；
2. 严格不可变版本保护（同一 ``(kind, asset_id, version)`` 若 ``checksum`` 相同则跳过，不同则拒绝覆盖）；
3. 安全过滤（拒绝跟随符号链接、跳过/拦截二进制文件与 > 2 MiB 超大文件并记录原因）；
4. 双引擎检索：PostgreSQL 下使用 ``to_tsvector('simple', search_text) @@ plainto_tsquery('simple', :q)``
   配合 ``ILIKE`` 兜底，SQLite 下优雅退化为大小写不敏感子串检索。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from agentforge_platform.asset_registry import (
    DEFAULT_INTEGRATION_ROOT,
    DEFAULT_ROOTS,
    AssetManifest,
    IntegrationPackManifest,
    Manifest,
    MethodUnitManifest,
    compute_checksum,
    is_safe_owned_path,
)
from agentforge_platform.db import is_postgres
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.method_index import DEFAULT_METHODS_ROOT, scan_methods
from agentforge_platform.models import AssetFile, AssetRelease

DEFAULT_SEED_ROOTS: tuple[Path, ...] = (
    *DEFAULT_ROOTS,
    DEFAULT_METHODS_ROOT,
    DEFAULT_INTEGRATION_ROOT,
)
MAX_ASSET_FILE_BYTES = 2 * 1024 * 1024  # 2 MiB

_MANIFEST_NAME = "manifest.json"
_EXCLUDED_DIRS = {"node_modules", "target", "dist", "build", "__pycache__", ".git", ".index"}
_EXCLUDED_FILES = {_MANIFEST_NAME, ".DS_Store", "Thumbs.db"}

_MODEL_BY_KIND: dict[str, type[Manifest]] = {
    "method-unit": MethodUnitManifest,
    "integration-pack": IntegrationPackManifest,
}

_JAVA_TYPE_RE = re.compile(
    r"\b(?:public\s+|protected\s+|abstract\s+|final\s+)*"
    r"(?:class|interface|enum|record)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
_JAVA_METHOD_RE = re.compile(
    r"\b(?:public|protected)\s+(?:static\s+|final\s+)*"
    r"[A-Za-z0-9_<>,\[\].?\s]+\s+([a-zA-Z_][A-Za-z0-9_]*)\s*\("
)
_TS_EXPORT_RE = re.compile(
    r"\bexport\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function|const|let|class|interface|type|enum)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
_WORD_SPLIT_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_JAVA_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "new", "super", "this"}


@dataclass(frozen=True)
class SkippedFile:
    """被安全策略跳过导入纯文本的文件记录。"""

    release_key: str
    rel_path: str
    reason: str


@dataclass
class SyncResult:
    """``sync_from_filesystem`` 的幂等同步结果汇总。"""

    inserted_releases: int = 0
    unchanged_releases: int = 0
    inserted_files: int = 0
    skipped_files: list[SkippedFile] = field(default_factory=list)
    release_checksums: dict[str, str] = field(default_factory=dict)

    @property
    def skipped_releases(self) -> int:
        return self.unchanged_releases

    def to_dict(self) -> dict[str, Any]:
        return {
            "inserted_releases": self.inserted_releases,
            "unchanged_releases": self.unchanged_releases,
            "inserted_files": self.inserted_files,
            "total_releases": len(self.release_checksums),
            "skipped_files": [
                {
                    "release_key": item.release_key,
                    "rel_path": item.rel_path,
                    "reason": item.reason,
                }
                for item in self.skipped_files
            ],
            "release_checksums": dict(sorted(self.release_checksums.items())),
        }


def extract_symbol_summary(rel_path: str, content_text: str) -> str:
    """从 Java / TS / JS / Vue 源码或模板中确定性提取关键类/方法/导出符号摘要。"""
    lower = rel_path.lower()
    symbols: list[str] = []
    seen: set[str] = set()

    def _add(sym: str) -> None:
        if sym and sym not in seen and sym not in _JAVA_KEYWORDS:
            seen.add(sym)
            symbols.append(sym)

    if ".java" in lower:
        for m in _JAVA_TYPE_RE.finditer(content_text):
            _add(m.group(1))
        for m in _JAVA_METHOD_RE.finditer(content_text):
            _add(m.group(1))
    elif any(ext in lower for ext in (".ts", ".js", ".vue")):
        for m in _TS_EXPORT_RE.finditer(content_text):
            _add(m.group(1))
        if ".vue" in lower:
            stem = Path(rel_path).stem.split(".")[0]
            _add(stem)

    if not symbols:
        stem = Path(rel_path).name.removesuffix(".j2")
        _add(Path(stem).stem)

    return " ".join(symbols[:40])


def _query_terms(query: str) -> list[str]:
    """将用户/Agent 搜索词（含驼峰如 TaskController 或连字符如 task-crud）拆分为独立小写词项。"""
    de_camel = _CAMEL_BOUNDARY_RE.sub(r"\1 \2", query or "")
    terms: list[str] = []
    for raw in _WORD_SPLIT_RE.findall(de_camel):
        lowered = raw.lower().strip()
        if lowered and lowered not in terms:
            terms.append(lowered)
    return terms


def _tokenize_for_fts(parts: Sequence[str]) -> str:
    """将标识符、驼峰词、路径片段与中文词拆分为空格分隔的词元，适配 PG ``to_tsvector('simple', ...)``。"""
    raw = " ".join(p for p in parts if p)
    de_camel = _CAMEL_BOUNDARY_RE.sub(r"\1 \2", raw)
    tokens: list[str] = []
    seen: set[str] = set()
    for word in _WORD_SPLIT_RE.findall(raw + " " + de_camel):
        lowered = word.lower()
        if lowered not in seen:
            seen.add(lowered)
            tokens.append(lowered)
        # 对连续中文字符串补充单字与双字词元，使 plainto_tsquery('simple', ...) 原生命中中文词
        if "\u4e00" <= word[0] <= "\u9fff" and len(word) > 1:
            for ch in word:
                if ch not in seen:
                    seen.add(ch)
                    tokens.append(ch)
            for idx in range(len(word) - 1):
                bg = word[idx : idx + 2]
                if bg not in seen:
                    seen.add(bg)
                    tokens.append(bg)
    return " ".join(tokens)


def _parse_manifest(payload: dict[str, Any]) -> Manifest:
    model: type[Manifest] = _MODEL_BY_KIND.get(str(payload.get("kind") or ""), AssetManifest)
    manifest = model.model_validate(payload)
    for owned in manifest.owned_paths:
        if not is_safe_owned_path(owned):
            raise AppException(
                ErrorCode.ASSET_CONFLICT,
                f"资产 {manifest.id}@{manifest.version} 含不安全 owned_path：{owned!r}",
                status_code=422,
            )
    return manifest


def _manifest_description(manifest: Manifest, payload: dict[str, Any]) -> str:
    desc = getattr(manifest, "description", None) or payload.get("description") or ""
    if isinstance(manifest, MethodUnitManifest):
        return f"{manifest.symbol} {manifest.signature} {desc}".strip()
    if isinstance(manifest, IntegrationPackManifest):
        return f"{manifest.provider} {desc}".strip()
    return str(desc).strip()


def _is_binary_bytes(raw: bytes) -> bool:
    if b"\x00" in raw:
        return True
    try:
        raw.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def _iter_asset_directories(
    root: Path, skipped_files: list[SkippedFile]
) -> list[Path]:
    """受控扫描资产目录：严禁跟随符号链接，自动记录并跳过符号链接。"""
    root = Path(root)
    if root.is_symlink():
        skipped_files.append(
            SkippedFile(release_key=root.name, rel_path=root.as_posix(), reason="symlink_rejected")
        )
        return []
    if not root.is_dir():
        return []

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        kept_dirs: list[str] = []
        for dname in sorted(dirnames):
            candidate = here / dname
            if candidate.is_symlink():
                skipped_files.append(
                    SkippedFile(
                        release_key=here.name,
                        rel_path=candidate.relative_to(root).as_posix(),
                        reason="symlink_rejected",
                    )
                )
                continue
            if dname in _EXCLUDED_DIRS or dname.startswith("."):
                continue
            kept_dirs.append(dname)
        dirnames[:] = kept_dirs

        if _MANIFEST_NAME in filenames:
            manifest_file = here / _MANIFEST_NAME
            if manifest_file.is_symlink():
                skipped_files.append(
                    SkippedFile(
                        release_key=here.name,
                        rel_path=manifest_file.relative_to(root).as_posix(),
                        reason="symlink_rejected",
                    )
                )
                continue
            found.append(here)
    return sorted(found)


class PgAssetStore:
    """A02：基于 SQLAlchemy 会话（PostgreSQL / SQLite 双兼容）的逐文件资产仓库。"""

    def __init__(self, db: Session) -> None:
        self.db = db

    def sync_from_filesystem(
        self,
        roots: Sequence[str | Path] | None = None,
        *,
        max_file_bytes: int = MAX_ASSET_FILE_BYTES,
    ) -> SyncResult:
        """扫描指定根目录集合，将全部已发布资产幂等同步至 ``asset_release`` 与 ``asset_file``。"""
        target_roots = [Path(r) for r in (roots if roots is not None else DEFAULT_SEED_ROOTS)]
        for r in target_roots:
            if r.resolve() == DEFAULT_METHODS_ROOT.resolve() and r.is_dir():
                # 复用 method_index.scan_methods 对方法单元做严格前置校验
                scan_methods(r)

        result = SyncResult()
        asset_dirs: list[Path] = []
        for root in target_roots:
            asset_dirs.extend(_iter_asset_directories(root, result.skipped_files))

        for directory in asset_dirs:
            manifest_path = directory / _MANIFEST_NAME
            raw_manifest = manifest_path.read_text(encoding="utf-8")
            payload = json.loads(raw_manifest)
            manifest = _parse_manifest(payload)
            release_key = f"{manifest.kind}/{manifest.id}@{manifest.version}"

            actual_checksum = compute_checksum(directory)
            if actual_checksum != manifest.checksum:
                raise AppException(
                    ErrorCode.ASSET_CHECKSUM_MISMATCH,
                    f"资产 {release_key} 目录内容 checksum ({actual_checksum}) "
                    f"与 manifest.json ({manifest.checksum}) 不一致",
                    status_code=409,
                )
            result.release_checksums[release_key] = actual_checksum

            existing = self.get_release(manifest.kind, manifest.id, manifest.version)
            if existing is not None:
                if existing.checksum == actual_checksum:
                    result.unchanged_releases += 1
                    continue
                raise AppException(
                    ErrorCode.ASSET_CONFLICT,
                    f"不可变版本保护：已发布资产 {release_key} 已存在且 checksum 不同 "
                    f"(db={existing.checksum}, new={actual_checksum})，禁止覆盖",
                    status_code=409,
                )

            description = _manifest_description(manifest, payload)
            release_search = _tokenize_for_fts(
                [
                    manifest.kind,
                    manifest.id,
                    manifest.version,
                    description,
                    " ".join(manifest.compatible_profiles),
                    " ".join(manifest.provides),
                    " ".join(manifest.requires),
                    " ".join(getattr(manifest, "tags", []) or []),
                ]
            )
            release_row = AssetRelease(
                kind=manifest.kind,
                asset_id=manifest.id,
                version=manifest.version,
                profile_tags_json=json.dumps(list(manifest.compatible_profiles), ensure_ascii=False),
                provides_json=json.dumps(list(manifest.provides), ensure_ascii=False),
                requires_json=json.dumps(list(manifest.requires), ensure_ascii=False),
                description=description,
                manifest_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                checksum=actual_checksum,
                search_text=release_search,
            )
            self.db.add(release_row)
            self.db.flush()
            result.inserted_releases += 1

            self._sync_release_files(
                release_row=release_row,
                release_key=release_key,
                directory=directory,
                max_file_bytes=max_file_bytes,
                result=result,
            )

        self.db.flush()
        return result

    def _sync_release_files(
        self,
        *,
        release_row: AssetRelease,
        release_key: str,
        directory: Path,
        max_file_bytes: int,
        result: SyncResult,
    ) -> None:
        for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
            here = Path(dirpath)
            kept_dirs: list[str] = []
            for dname in sorted(dirnames):
                candidate = here / dname
                rel_dir = candidate.relative_to(directory).as_posix()
                if candidate.is_symlink():
                    result.skipped_files.append(
                        SkippedFile(
                            release_key=release_key,
                            rel_path=rel_dir,
                            reason="symlink_rejected",
                        )
                    )
                    continue
                if dname in _EXCLUDED_DIRS:
                    continue
                kept_dirs.append(dname)
            dirnames[:] = kept_dirs

            for fname in sorted(filenames):
                if fname in _EXCLUDED_FILES:
                    continue
                fpath = here / fname
                rel_path = fpath.relative_to(directory).as_posix()
                if fpath.is_symlink():
                    result.skipped_files.append(
                        SkippedFile(
                            release_key=release_key,
                            rel_path=rel_path,
                            reason="symlink_rejected",
                        )
                    )
                    continue
                if not fpath.is_file():
                    continue

                size_bytes = fpath.stat().st_size
                if size_bytes > max_file_bytes:
                    result.skipped_files.append(
                        SkippedFile(
                            release_key=release_key,
                            rel_path=rel_path,
                            reason=f"oversize_gt_{max_file_bytes}_bytes",
                        )
                    )
                    continue

                raw = fpath.read_bytes()
                sha256 = hashlib.sha256(raw).hexdigest()
                if _is_binary_bytes(raw):
                    result.skipped_files.append(
                        SkippedFile(
                            release_key=release_key,
                            rel_path=rel_path,
                            reason="binary_file",
                        )
                    )
                    continue

                content_text = raw.decode("utf-8")
                symbols = extract_symbol_summary(rel_path, content_text)
                file_search = _tokenize_for_fts(
                    [
                        release_row.kind,
                        release_row.asset_id,
                        release_row.version,
                        rel_path,
                        symbols,
                        content_text[:8000],
                    ]
                )
                self.db.add(
                    AssetFile(
                        release_id=release_row.id,
                        rel_path=rel_path,
                        content_text=content_text,
                        sha256=sha256,
                        size_bytes=size_bytes,
                        is_binary=False,
                        symbol_summary=symbols,
                        search_text=file_search,
                    )
                )
                result.inserted_files += 1

    def get_release(self, kind: str, asset_id: str, version: str) -> AssetRelease | None:
        """按 ``(kind, asset_id, version)`` 精确获取已入库的资产版本。"""
        stmt = select(AssetRelease).where(
            AssetRelease.kind == kind,
            AssetRelease.asset_id == asset_id,
            AssetRelease.version == version,
        )
        return self.db.scalars(stmt).first()

    def list_releases(
        self,
        *,
        kind: str | None = None,
        asset_id: str | None = None,
        profile: str | None = None,
    ) -> list[AssetRelease]:
        """列出已入库资产版本（可选按 ``kind``、``asset_id``、``profile`` 过滤）。"""
        stmt = select(AssetRelease).order_by(
            AssetRelease.kind, AssetRelease.asset_id, AssetRelease.version
        )
        if kind:
            stmt = stmt.where(AssetRelease.kind == kind)
        if asset_id:
            stmt = stmt.where(AssetRelease.asset_id == asset_id)
        rows = list(self.db.scalars(stmt).all())
        if profile:
            rows = [r for r in rows if profile in r.profile_tags]
        return rows

    def search_releases(
        self,
        query: str,
        *,
        kind: str | None = None,
        profile: str | None = None,
        limit: int = 20,
    ) -> list[AssetRelease]:
        """全文检索资产版本：PG 下使用 ``to_tsvector('simple', search_text)`` + ``ILIKE``，SQLite 下使用 ``LIKE``。"""
        terms = _query_terms(query)
        if not terms:
            return self.list_releases(kind=kind, profile=profile)[:limit]

        stmt = select(AssetRelease)
        if kind:
            stmt = stmt.where(AssetRelease.kind == kind)

        if is_postgres(self.db):
            ts_query = " & ".join(terms)
            fts_match = func.to_tsvector("simple", AssetRelease.search_text).op("@@")(
                func.plainto_tsquery("simple", " ".join(terms))
            )
            ilike_match = and_(*[AssetRelease.search_text.ilike(f"%{t}%") for t in terms])
            _ = ts_query
            stmt = stmt.where(or_(fts_match, ilike_match))
        else:
            stmt = stmt.where(
                and_(*[func.lower(AssetRelease.search_text).like(f"%{t}%") for t in terms])
            )

        stmt = stmt.order_by(AssetRelease.kind, AssetRelease.asset_id, AssetRelease.version)
        rows = list(self.db.scalars(stmt).all())
        if profile:
            rows = [r for r in rows if profile in r.profile_tags]
        return rows[:limit]

    def _resolve_release_id(self, release_or_id: int | AssetRelease) -> int:
        if isinstance(release_or_id, AssetRelease):
            return release_or_id.id
        return int(release_or_id)

    def get_file(self, release_or_id: int | AssetRelease, rel_path: str) -> AssetFile | None:
        """按 ``(release_id, rel_path)`` 获取单个资产文件。"""
        release_id = self._resolve_release_id(release_or_id)
        normalized = rel_path.strip().lstrip("/")
        stmt = select(AssetFile).where(
            AssetFile.release_id == release_id,
            AssetFile.rel_path == normalized,
        )
        return self.db.scalars(stmt).first()

    def list_files(self, release_or_id: int | AssetRelease) -> list[AssetFile]:
        """按相对路径排序列出某资产版本下的全部文件。"""
        release_id = self._resolve_release_id(release_or_id)
        stmt = (
            select(AssetFile)
            .where(AssetFile.release_id == release_id)
            .order_by(AssetFile.rel_path)
        )
        return list(self.db.scalars(stmt).all())

    def search_files(
        self,
        query: str,
        *,
        release_id: int | None = None,
        kind: str | None = None,
        asset_id: str | None = None,
        version: str | None = None,
        limit: int = 20,
    ) -> list[AssetFile]:
        """全文检索资产文件（支持按版本、类型或关键词搜索源码与符号摘要）。"""
        terms = _query_terms(query)
        stmt = select(AssetFile).join(AssetRelease, AssetFile.release_id == AssetRelease.id)
        if release_id is not None:
            stmt = stmt.where(AssetFile.release_id == release_id)
        if kind:
            stmt = stmt.where(AssetRelease.kind == kind)
        if asset_id:
            stmt = stmt.where(AssetRelease.asset_id == asset_id)
        if version:
            stmt = stmt.where(AssetRelease.version == version)

        if terms:
            if is_postgres(self.db):
                fts_match = func.to_tsvector("simple", AssetFile.search_text).op("@@")(
                    func.plainto_tsquery("simple", " ".join(terms))
                )
                ilike_match = and_(*[AssetFile.search_text.ilike(f"%{t}%") for t in terms])
                stmt = stmt.where(or_(fts_match, ilike_match))
            else:
                stmt = stmt.where(
                    and_(*[func.lower(AssetFile.search_text).like(f"%{t}%") for t in terms])
                )

        stmt = stmt.order_by(AssetFile.release_id, AssetFile.rel_path).limit(limit)
        return list(self.db.scalars(stmt).all())


__all__ = [
    "DEFAULT_SEED_ROOTS",
    "MAX_ASSET_FILE_BYTES",
    "PgAssetStore",
    "SkippedFile",
    "SyncResult",
    "extract_symbol_summary",
]
