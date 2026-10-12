"""R06B：方法代码仓库（``methods/``）与可重建的 SQLite/FTS5 只读索引。

- **权威数据是源码与 manifest**（:class:`MethodUnitManifest`），索引只是**可丢弃的派生视图**，
  丢失或损坏都能从仓库全量重建；
- 扫描**受控文件树**：不跟随符号链接、跳过隐藏目录，避免读取宿主敏感路径；
- 索引只保存检索字段与关系（``id/version/kind/language/framework/profile/tags/provides/
  description/asset_path/checksum``），**不以索引内容替代 manifest 校验**；
- 构建写入临时文件后 ``os.replace`` **原子发布**；``update`` 做增量重索引；索引损坏时自动重建；
- 不在数据库存密钥或未经清洗的私有业务源码，不引入向量库/远程索引服务。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from agentforge_platform.asset_registry import (
    REPO_ROOT,
    MethodUnitManifest,
    compute_checksum,
)
from agentforge_platform.errors import AppException, ErrorCode

INDEX_SCHEMA_VERSION = "0.1"
_MANIFEST_NAME = "manifest.json"
INDEX_FILENAME = "method-index.sqlite"
DEFAULT_METHODS_ROOT = REPO_ROOT / "methods"
DEFAULT_INDEX_PATH = DEFAULT_METHODS_ROOT / ".index" / INDEX_FILENAME

_QUERY_TERM = re.compile(r"[A-Za-z0-9_]+")


def methods_root() -> Path:
    return Path(os.environ.get("AGENTFORGE_METHODS_ROOT", str(DEFAULT_METHODS_ROOT)))


def index_path() -> Path:
    return Path(os.environ.get("AGENTFORGE_METHOD_INDEX", str(DEFAULT_INDEX_PATH)))


@dataclass(frozen=True)
class MethodRecord:
    """一条可索引的方法单元（从 manifest + checksum 派生，不含源码正文）。"""

    id: str
    version: str
    symbol: str
    language: str
    framework: str
    layer: str
    description: str
    asset_path: str
    checksum: str
    profiles: list[str]
    provides: list[str]
    requires: list[str]
    tags: list[str]
    acceptance_ids: list[str]


@dataclass(frozen=True)
class IndexBuildResult:
    count: int
    revision: str
    path: str


def _iter_method_dirs(root: Path):
    """受控遍历：不跟随符号链接、跳过隐藏目录（含索引目录 ``.index``）。"""
    root = Path(root)
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(
            name for name in dirnames
            if not name.startswith(".") and not (here / name).is_symlink()
        )
        files = [name for name in filenames if not (here / name).is_symlink()]
        if _MANIFEST_NAME in files:
            yield here


def _asset_path(directory: Path) -> str:
    try:
        return directory.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return directory.as_posix()


def scan_methods(root: str | Path) -> list[MethodRecord]:
    """扫描方法仓库并**校验每个 manifest 与内容 checksum**；不一致或非方法单元即拒绝。"""
    records: list[MethodRecord] = []
    for directory in _iter_method_dirs(Path(root)):
        payload = json.loads((directory / _MANIFEST_NAME).read_text(encoding="utf-8"))
        if payload.get("kind") != "method-unit":
            raise AppException(
                ErrorCode.ASSET_CONFLICT,
                f"方法仓库包含非 method-unit 资产：{directory}",
                status_code=422,
            )
        manifest = MethodUnitManifest.model_validate(payload)
        actual = compute_checksum(directory)
        if actual != manifest.checksum:
            raise AppException(
                ErrorCode.ASSET_CHECKSUM_MISMATCH,
                f"方法 {manifest.id}@{manifest.version} checksum 与内容不一致（疑似被篡改）",
                status_code=409,
            )
        records.append(
            MethodRecord(
                id=manifest.id, version=manifest.version, symbol=manifest.symbol,
                language=manifest.language, framework=manifest.framework,
                layer=manifest.layer, description=manifest.description,
                asset_path=_asset_path(directory), checksum=manifest.checksum,
                profiles=list(manifest.compatible_profiles), provides=list(manifest.provides),
                requires=list(manifest.requires), tags=list(manifest.tags),
                acceptance_ids=list(manifest.acceptance_ids),
            )
        )
    records.sort(key=lambda record: (record.id, record.version))
    return records


def index_revision(records: list[MethodRecord]) -> str:
    """索引修订号：对 (id, version, checksum) 排序后取 SHA-256（源码不变则等价）。"""
    payload = [[r.id, r.version, r.checksum] for r in records]
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS method_unit (
    asset_id TEXT NOT NULL,
    version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    language TEXT NOT NULL,
    framework TEXT NOT NULL,
    layer TEXT NOT NULL,
    description TEXT NOT NULL,
    asset_path TEXT NOT NULL,
    checksum TEXT NOT NULL,
    profiles TEXT NOT NULL,
    provides TEXT NOT NULL,
    requires TEXT NOT NULL,
    tags TEXT NOT NULL,
    acceptance_ids TEXT NOT NULL,
    PRIMARY KEY (asset_id, version)
);
CREATE VIRTUAL TABLE IF NOT EXISTS method_unit_fts USING fts5(
    asset_id UNINDEXED,
    version UNINDEXED,
    symbol,
    description,
    tags,
    provides,
    language,
    framework,
    layer,
    tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def _row_values(record: MethodRecord) -> tuple:
    return (
        record.id, record.version, record.symbol, record.language, record.framework,
        record.layer, record.description, record.asset_path, record.checksum,
        json.dumps(record.profiles, ensure_ascii=False),
        json.dumps(record.provides, ensure_ascii=False),
        json.dumps(record.requires, ensure_ascii=False),
        json.dumps(record.tags, ensure_ascii=False),
        json.dumps(record.acceptance_ids, ensure_ascii=False),
    )


def _insert_method(conn: sqlite3.Connection, record: MethodRecord) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO method_unit (asset_id, version, symbol, language, framework, "
        "layer, description, asset_path, checksum, profiles, provides, requires, tags, "
        "acceptance_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        _row_values(record),
    )
    conn.execute("DELETE FROM method_unit_fts WHERE asset_id=? AND version=?",
                 (record.id, record.version))
    conn.execute(
        "INSERT INTO method_unit_fts (asset_id, version, symbol, description, tags, provides, "
        "language, framework, layer) VALUES (?,?,?,?,?,?,?,?,?)",
        (record.id, record.version, record.symbol.replace(".", " "), record.description,
         " ".join(record.tags), " ".join(record.provides), record.language,
         record.framework, record.layer),
    )


def _write_meta(conn: sqlite3.Connection, records: list[MethodRecord]) -> None:
    conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('index_schema_version', ?)",
                 (INDEX_SCHEMA_VERSION,))
    conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('index_revision', ?)",
                 (index_revision(records),))
    conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('asset_count', ?)",
                 (str(len(records)),))


def is_valid_index(path: str | Path) -> bool:
    """索引是否可用（能打开、meta 版本匹配、表存在）；损坏/缺失返回 False。"""
    path = Path(path)
    if not path.is_file():
        return False
    try:
        conn = _connect(path, read_only=True)
    except sqlite3.Error:
        return False
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )}
        if not {"method_unit", "method_unit_fts", "index_meta"} <= tables:
            return False
        row = conn.execute("SELECT value FROM index_meta WHERE key='index_schema_version'").fetchone()
        return row is not None and row[0] == INDEX_SCHEMA_VERSION
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def build_index(root: str | Path, path: str | Path) -> IndexBuildResult:
    """全量重建索引：扫描 → 校验 checksum → 写临时库 → 原子替换（不产生半成品）。"""
    path = Path(path)
    records = scan_methods(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}"
    if tmp.exists():
        tmp.unlink()
    conn = _connect(tmp)
    try:
        conn.executescript(_SCHEMA)
        for record in records:
            _insert_method(conn, record)
        _write_meta(conn, records)
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp, path)  # 原子发布
    return IndexBuildResult(count=len(records), revision=index_revision(records), path=str(path))


def _existing_versions(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    return {
        (row["asset_id"], row["version"]): row["checksum"]
        for row in conn.execute("SELECT asset_id, version, checksum FROM method_unit")
    }


def update_index(root: str | Path, path: str | Path) -> IndexBuildResult:
    """增量重索引：新增/变更写入、删除移除；索引缺失或损坏则整库重建。"""
    path = Path(path)
    if not is_valid_index(path):
        return build_index(root, path)
    records = scan_methods(root)
    conn = _connect(path)
    try:
        conn.executescript(_SCHEMA)
        existing = _existing_versions(conn)
        target = {(r.id, r.version): r for r in records}
        removed = set(existing) - set(target)
        for asset_id, version in removed:
            conn.execute("DELETE FROM method_unit WHERE asset_id=? AND version=?", (asset_id, version))
            conn.execute("DELETE FROM method_unit_fts WHERE asset_id=? AND version=?", (asset_id, version))
        changed = [
            record for key, record in target.items()
            if existing.get(key) != record.checksum
        ]
        for record in changed:
            _insert_method(conn, record)
        _write_meta(conn, records)
        conn.commit()
    finally:
        conn.close()
    return IndexBuildResult(count=len(records), revision=index_revision(records), path=str(path))


def read_meta(path: str | Path) -> dict[str, str]:
    conn = _connect(Path(path), read_only=True)
    try:
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM index_meta")}
    finally:
        conn.close()


def _match_expression(query: str) -> str:
    """把查询规整为 FTS5 安全的 AND 词项串（避免 MATCH 语法注入）。"""
    return " ".join(_QUERY_TERM.findall(query))


def search(query: str, *, path: str | Path | None = None, limit: int = 20) -> list[dict]:
    """FTS5 只读检索：按关键词召回方法单元（语义判定/兼容筛选归 R06C）。"""
    match = _match_expression(query)
    if not match:
        return []
    index = Path(path) if path is not None else index_path()
    if not index.is_file():
        raise AppException(ErrorCode.ASSET_NOT_FOUND, "方法索引不存在，请先构建。", status_code=404)
    conn = _connect(index, read_only=True)
    try:
        rows = conn.execute(
            "SELECT m.asset_id, m.version, m.symbol, m.description, m.layer, m.asset_path "
            "FROM method_unit_fts "
            "JOIN method_unit AS m "
            "ON m.asset_id = method_unit_fts.asset_id AND m.version = method_unit_fts.version "
            "WHERE method_unit_fts MATCH ? LIMIT ?",
            (match, limit),
        )
        return [dict(row) for row in rows]
    finally:
        conn.close()


def refresh_checksums(root: str | Path) -> None:
    """回填方法 manifest 的 checksum（发布/建档用；与 asset_registry 的 --update 同源）。"""
    for directory in _iter_method_dirs(Path(root)):
        manifest_path = directory / _MANIFEST_NAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["checksum"] = compute_checksum(directory)
        manifest_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"updated checksum: {payload['id']}@{payload['version']}")


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "build"
    root, index = methods_root(), index_path()
    if command == "build":
        print(build_index(root, index))
    elif command == "update":
        print(update_index(root, index))
    elif command == "checksums":
        refresh_checksums(root)
    elif command == "search":
        for hit in search(argv[1], path=index):
            print(f"{hit['asset_id']}@{hit['version']} {hit['symbol']}")
    else:
        print(f"unknown command: {command}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
