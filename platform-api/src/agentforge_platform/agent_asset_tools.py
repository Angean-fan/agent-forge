"""A03：面向部门编码子 Agent 的只读资产检索、单文件片段读取与安全暂存套用工具集。

提供三个显式白名单工具：
1. ``search_assets(query, kind, profile, limit)``：检索 PG 已认证资产与匹配文件符号摘要；
2. ``read_asset_file(asset_ref, rel_path, start_line, end_line)``：按行号范围精准读取单个资产文件片段
   （强制单次上限 ≤ 300 行且 ≤ 16 KB，防止一次塞爆子 Agent 上下文）；
3. ``apply_asset_to_staging(asset_ref, target_subdir, file_backend)``：将已认证资产文件安全写入子 Agent
   专属暂存区（严格经过 ``FileBackend`` 的 ``allowed_paths`` 与 ``PROTECTED_PREFIXES`` 校验，严禁越界或写只读缓存）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform.agent_files import FileBackend
from agentforge_platform.asset_cache import DEFAULT_CACHE_ROOT, default_cache_root
from agentforge_platform.asset_registry import is_safe_owned_path
from agentforge_platform.asset_store_pg import PgAssetStore
from agentforge_platform.db import SessionLocal, session_scope
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.models import AssetRelease

DEFAULT_MAX_READ_LINES = 300
DEFAULT_MAX_READ_BYTES = 16 * 1024  # 16 KB


@contextmanager
def _resolve_store(
    store: PgAssetStore | None = None,
    db: Session | None = None,
    session_factory: sessionmaker[Session] | None = None,
):
    if store is not None:
        yield store
        return
    if db is not None:
        yield PgAssetStore(db)
        return
    factory = session_factory or SessionLocal
    with session_scope(factory) as session:
        yield PgAssetStore(session)


def parse_asset_ref(asset_ref: str) -> tuple[str | None, str, str]:
    """解析资产引用串（支持 ``<kind>/<asset_id>@<version>`` 或 ``<asset_id>@<version>``）。"""
    raw = (asset_ref or "").strip()
    if "@" not in raw:
        raise AppException(
            ErrorCode.BAD_REQUEST,
            f"非法 asset_ref 格式（需形如 <kind>/<id>@<version> 或 <id>@<version>）：{asset_ref!r}",
            status_code=422,
        )
    left, version = raw.rsplit("@", 1)
    version = version.strip()
    if "/" in left:
        parts = left.split("/")
        if len(parts) != 2:
            raise AppException(
                ErrorCode.BAD_REQUEST,
                f"非法 asset_ref 格式：{asset_ref!r}",
                status_code=422,
            )
        kind, asset_id = parts[0].strip(), parts[1].strip()
    else:
        kind, asset_id = None, left.strip()

    if not asset_id or not version or ".." in raw or "\\" in raw:
        raise AppException(
            ErrorCode.BAD_REQUEST,
            f"非法 asset_ref：{asset_ref!r}",
            status_code=422,
        )
    return kind, asset_id, version


def _find_release(store: PgAssetStore, asset_ref: str) -> AssetRelease:
    kind, asset_id, version = parse_asset_ref(asset_ref)
    if kind is not None:
        rel = store.get_release(kind, asset_id, version)
        if rel is None:
            raise AppException(
                ErrorCode.ASSET_NOT_FOUND,
                f"未找到已认证资产：{kind}/{asset_id}@{version}",
                status_code=404,
            )
        return rel

    matches = [
        r for r in store.list_releases(asset_id=asset_id) if r.version == version
    ]
    if not matches:
        raise AppException(
            ErrorCode.ASSET_NOT_FOUND,
            f"未找到已认证资产：{asset_id}@{version}",
            status_code=404,
        )
    if len(matches) > 1:
        raise AppException(
            ErrorCode.ASSET_CONFLICT,
            f"资产引用 {asset_id}@{version} 存在多种 kind，请显式指定 <kind>/<id>@<version>",
            status_code=409,
        )
    return matches[0]


def _validate_asset_rel_path(rel_path: str) -> str:
    cleaned = (rel_path or "").strip()
    if not is_safe_owned_path(cleaned):
        raise AppException(
            ErrorCode.BAD_REQUEST,
            f"非法资产文件相对路径：{rel_path!r}",
            status_code=422,
        )
    parts = PurePosixPath(cleaned).parts
    if any(p in ("", ".", "..") for p in parts):
        raise AppException(
            ErrorCode.BAD_REQUEST,
            f"非法资产文件相对路径：{rel_path!r}",
            status_code=422,
        )
    return PurePosixPath(*parts).as_posix()


def search_assets(
    query: str,
    kind: str | None = None,
    profile: str | None = None,
    limit: int = 10,
    *,
    store: PgAssetStore | None = None,
    db: Session | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> list[dict[str, Any]]:
    """检索 PG 已认证资产与匹配文件符号摘要（只读工具）。"""
    effective_limit = max(1, min(int(limit), 50))
    with _resolve_store(store=store, db=db, session_factory=session_factory) as pg_store:
        releases = pg_store.search_releases(
            query, kind=kind, profile=profile, limit=effective_limit
        )
        file_hits = pg_store.search_files(query, kind=kind, limit=effective_limit * 2)

        files_by_release: dict[int, list[dict[str, Any]]] = {}
        for fh in file_hits:
            files_by_release.setdefault(fh.release_id, []).append(
                {
                    "rel_path": fh.rel_path,
                    "symbol_summary": fh.symbol_summary,
                    "size_bytes": fh.size_bytes,
                }
            )

        seen_release_ids = {r.id for r in releases}
        if len(releases) < effective_limit and file_hits:
            for fh in file_hits:
                if fh.release_id not in seen_release_ids:
                    extra_rel = pg_store.db.get(AssetRelease, fh.release_id)
                    if extra_rel is not None and (
                        profile is None or profile in extra_rel.profile_tags
                    ):
                        releases.append(extra_rel)
                        seen_release_ids.add(extra_rel.id)
                        if len(releases) >= effective_limit:
                            break

        results: list[dict[str, Any]] = []
        for rel in releases[:effective_limit]:
            matched = files_by_release.get(rel.id)
            if not matched:
                matched = [
                    {
                        "rel_path": af.rel_path,
                        "symbol_summary": af.symbol_summary,
                        "size_bytes": af.size_bytes,
                    }
                    for af in pg_store.list_files(rel)[:5]
                ]
            results.append(
                {
                    "asset_ref": f"{rel.kind}/{rel.asset_id}@{rel.version}",
                    "kind": rel.kind,
                    "asset_id": rel.asset_id,
                    "version": rel.version,
                    "description": rel.description,
                    "profile_tags": rel.profile_tags,
                    "provides": rel.provides,
                    "requires": rel.requires,
                    "checksum": rel.checksum,
                    "matched_files": matched[:10],
                }
            )
        return results


def read_asset_file(
    asset_ref: str,
    rel_path: str,
    start_line: int = 1,
    end_line: int | None = None,
    *,
    store: PgAssetStore | None = None,
    db: Session | None = None,
    session_factory: sessionmaker[Session] | None = None,
    max_lines: int = DEFAULT_MAX_READ_LINES,
    max_bytes: int = DEFAULT_MAX_READ_BYTES,
) -> dict[str, Any]:
    """按文件与行号范围从 PG ``asset_file`` 精准读取参考代码片段（单次默认上限 ≤ 300 行或 16 KB）。"""
    safe_rel = _validate_asset_rel_path(rel_path)
    if start_line < 1:
        raise AppException(
            ErrorCode.BAD_REQUEST,
            f"start_line 必须 >= 1，收到：{start_line}",
            status_code=422,
        )
    if end_line is not None and end_line < start_line:
        raise AppException(
            ErrorCode.BAD_REQUEST,
            f"end_line ({end_line}) 不得小于 start_line ({start_line})",
            status_code=422,
        )

    line_cap = max(1, min(int(max_lines), DEFAULT_MAX_READ_LINES))
    byte_cap = max(1, min(int(max_bytes), DEFAULT_MAX_READ_BYTES))

    with _resolve_store(store=store, db=db, session_factory=session_factory) as pg_store:
        release = _find_release(pg_store, asset_ref)
        af = pg_store.get_file(release, safe_rel)
        if af is None or af.content_text is None:
            raise AppException(
                ErrorCode.ASSET_NOT_FOUND,
                f"资产 {release.kind}/{release.asset_id}@{release.version} 中不存在文件：{safe_rel}",
                status_code=404,
            )

        all_lines = af.content_text.splitlines()
        total_lines = len(all_lines)
        if total_lines == 0:
            return {
                "asset_ref": f"{release.kind}/{release.asset_id}@{release.version}",
                "rel_path": af.rel_path,
                "start_line": 1,
                "end_line": 0,
                "total_lines": 0,
                "truncated": False,
                "symbol_summary": af.symbol_summary,
                "content": "",
            }

        if start_line > total_lines:
            raise AppException(
                ErrorCode.BAD_REQUEST,
                f"start_line ({start_line}) 超出文件总行数 ({total_lines})",
                status_code=422,
            )

        requested_end = total_lines if end_line is None else min(end_line, total_lines)
        capped_end = min(requested_end, start_line + line_cap - 1)
        truncated = capped_end < requested_end or (end_line is None and capped_end < total_lines)

        selected_lines: list[str] = []
        used_bytes = 0
        actual_end = start_line - 1
        for idx in range(start_line - 1, capped_end):
            line_text = all_lines[idx]
            encoded_len = len(line_text.encode("utf-8")) + (1 if selected_lines else 0)
            if used_bytes + encoded_len > byte_cap:
                truncated = True
                if not selected_lines:
                    # 单行超长时按字节安全截断该行
                    clipped = line_text.encode("utf-8")[:byte_cap].decode("utf-8", errors="ignore")
                    selected_lines.append(clipped)
                    actual_end = idx + 1
                break
            selected_lines.append(line_text)
            used_bytes += encoded_len
            actual_end = idx + 1

        return {
            "asset_ref": f"{release.kind}/{release.asset_id}@{release.version}",
            "rel_path": af.rel_path,
            "start_line": start_line,
            "end_line": actual_end,
            "total_lines": total_lines,
            "truncated": truncated,
            "symbol_summary": af.symbol_summary,
            "content": "\n".join(selected_lines),
        }


def _assert_not_inside_cache(file_backend: FileBackend) -> None:
    """严禁任何子 Agent 将只读缓存目录 ``.cache/assets`` 作为 FileBackend 的写入目标。"""
    for cache_dir in {DEFAULT_CACHE_ROOT.resolve(), default_cache_root().resolve()}:
        if file_backend.run_dir == cache_dir or cache_dir in file_backend.run_dir.parents:
            raise AppException(
                ErrorCode.FORBIDDEN,
                "严禁向只读资产缓存目录 .cache/assets 写入或套用文件",
                status_code=403,
            )


def apply_asset_to_staging(
    asset_ref: str,
    target_subdir: str,
    file_backend: FileBackend,
    *,
    store: PgAssetStore | None = None,
    db: Session | None = None,
    session_factory: sessionmaker[Session] | None = None,
    rel_paths: Sequence[str] | None = None,
) -> dict[str, Any]:
    """将已认证资产文件套用到子 Agent 暂存区（严格经 ``FileBackend`` 校验 ``allowed_paths`` 与 ``PROTECTED_PREFIXES``）。"""
    _assert_not_inside_cache(file_backend)
    cleaned_subdir = (target_subdir or "").strip().strip("/")
    if cleaned_subdir and not is_safe_owned_path(cleaned_subdir):
        raise AppException(
            ErrorCode.BAD_REQUEST,
            f"非法 target_subdir：{target_subdir!r}",
            status_code=422,
        )
    if ".cache" in PurePosixPath(cleaned_subdir).parts:
        raise AppException(
            ErrorCode.FORBIDDEN,
            "禁止向 .cache 只读缓存路径套用资产",
            status_code=403,
        )

    filter_set = {_validate_asset_rel_path(p) for p in rel_paths} if rel_paths else None

    with _resolve_store(store=store, db=db, session_factory=session_factory) as pg_store:
        release = _find_release(pg_store, asset_ref)
        files = pg_store.list_files(release)
        if filter_set is not None:
            by_path = {af.rel_path: af for af in files}
            missing = sorted(filter_set - set(by_path))
            if missing:
                raise AppException(
                    ErrorCode.ASSET_NOT_FOUND,
                    f"资产 {release.kind}/{release.asset_id}@{release.version} 缺少文件：{missing}",
                    status_code=404,
                )
            files = [by_path[p] for p in sorted(filter_set)]

        if not files:
            raise AppException(
                ErrorCode.ASSET_NOT_FOUND,
                f"资产 {release.kind}/{release.asset_id}@{release.version} 无可套用文件",
                status_code=404,
            )

        staged_paths: list[str] = []
        for af in files:
            if af.content_text is None or af.is_binary:
                continue
            rel_in_asset = _validate_asset_rel_path(af.rel_path)
            if release.kind == "method-unit" and rel_in_asset.startswith("src/"):
                rel_in_asset = rel_in_asset[len("src/") :]
            target_rel = (
                f"{cleaned_subdir}/{rel_in_asset}" if cleaned_subdir else rel_in_asset
            )
            file_backend.write(target_rel, af.content_text.encode("utf-8"))
            staged_paths.append(target_rel)

        return {
            "asset_ref": f"{release.kind}/{release.asset_id}@{release.version}",
            "checksum": release.checksum,
            "staged_files": staged_paths,
        }


def build_agent_asset_tools(
    *,
    file_backend: FileBackend,
    store: PgAssetStore | None = None,
    db: Session | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> tuple[Callable[..., Any], ...]:
    """构造可挂载到 ``build_patch_engine(..., extra_tools=..., allow_asset_tools=True)`` 的只读资产工具三元组。"""

    def search_assets_tool(
        query: str,
        kind: str | None = None,
        profile: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """在 PG 公共资产库中检索已认证的模板、能力包、方法单元或集成包。"""
        return search_assets(
            query,
            kind=kind,
            profile=profile,
            limit=limit,
            store=store,
            db=db,
            session_factory=session_factory,
        )

    def read_asset_file_tool(
        asset_ref: str,
        rel_path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        """从 PG 公共资产库按行号范围读取单个资产文件片段（单次最多 300 行或 16 KB）。"""
        return read_asset_file(
            asset_ref,
            rel_path,
            start_line=start_line,
            end_line=end_line,
            store=store,
            db=db,
            session_factory=session_factory,
        )

    def apply_asset_to_staging_tool(
        asset_ref: str,
        target_subdir: str,
        rel_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """将 PG 已认证资产文件安全套用到当前子 Agent 的授权暂存区。"""
        return apply_asset_to_staging(
            asset_ref,
            target_subdir,
            file_backend,
            store=store,
            db=db,
            session_factory=session_factory,
            rel_paths=rel_paths,
        )

    search_assets_tool.__name__ = "search_assets"
    read_asset_file_tool.__name__ = "read_asset_file"
    apply_asset_to_staging_tool.__name__ = "apply_asset_to_staging"
    return (search_assets_tool, read_asset_file_tool, apply_asset_to_staging_tool)


__all__ = [
    "DEFAULT_MAX_READ_BYTES",
    "DEFAULT_MAX_READ_LINES",
    "apply_asset_to_staging",
    "build_agent_asset_tools",
    "parse_asset_ref",
    "read_asset_file",
    "search_assets",
]
