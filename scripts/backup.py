#!/usr/bin/env python3
"""M28 平台备份：SQLite 元数据一致性快照 + 运行产物目录，加密后落盘。

- **一致性快照**：用 SQLite `VACUUM INTO` 生成单文件快照。它走 SQLite 自身事务，
  在 WAL 写入期间也不会读到半写状态——**不**直接复制 `agentforge.db`（那样会漏掉
  `-wal` 中未合并的已提交事务）。
- **加密**：快照与产物目录打包后用 `openssl enc -aes-256-cbc -pbkdf2` 加密；
  密钥来自**独立的密钥文件**（默认 `/etc/agentforge/backup.key`，权限 0600），
  与备份产物分离保存、不同机存放（见 docs/MVP_DEPLOY.md）。
- **权限**：备份文件与 manifest 一律 0600。

用法：
    python3 scripts/backup.py --db platform-api/agentforge.db \
        --runs-root var/runs --out-dir backups/ --key-file /etc/agentforge/backup.key
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def _configure_sqlite_source(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def snapshot_database(db_path: Path, target: Path) -> None:
    """用 VACUUM INTO 生成一致性快照；目标文件必须不存在。"""
    if target.exists():
        target.unlink()
    connection = _configure_sqlite_source(db_path)
    try:
        connection.execute("VACUUM INTO ?", (str(target),))
    finally:
        connection.close()
    os.chmod(target, 0o600)


def build_archive(snapshot: Path, runs_root: Path | None, archive: Path) -> None:
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(snapshot, arcname="agentforge.db")
        if runs_root is not None and runs_root.exists():
            tar.add(runs_root, arcname="runs")


def encrypt(archive: Path, key_file: Path, output: Path) -> None:
    subprocess.run(
        [
            "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-md", "sha256",
            "-in", str(archive), "-out", str(output), "-pass", f"file:{key_file}",
        ],
        check=True,
    )
    os.chmod(output, 0o600)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_backup(db_path: Path, runs_root: Path | None, out_dir: Path, key_file: Path) -> dict:
    if not db_path.is_file():
        raise SystemExit(f"database not found: {db_path}")
    if not key_file.is_file():
        raise SystemExit(f"backup key file not found: {key_file}")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    encrypted = out_dir / f"agentforge-backup-{stamp}.tar.gz.enc"

    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "agentforge.db"
        archive = Path(tmp) / "payload.tar.gz"
        snapshot_database(db_path, snapshot)
        build_archive(snapshot, runs_root, archive)
        encrypt(archive, key_file, encrypted)

    manifest = {
        "created_at": stamp,
        "encrypted_file": encrypted.name,
        "sha256": _sha256(encrypted),
        "bytes": encrypted.stat().st_size,
        "db_source": str(db_path),
        "runs_root": str(runs_root) if runs_root else None,
        "encryption": "openssl enc -aes-256-cbc -pbkdf2 -md sha256",
    }
    manifest_path = out_dir / f"{encrypted.name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backup.py")
    parser.add_argument(
        "--db",
        default=os.environ.get("AGENTFORGE_BACKUP_DB", "platform-api/agentforge.db"),
    )
    parser.add_argument("--runs-root", default=os.environ.get("RUNNER_WORK_ROOT", "var/runs"))
    parser.add_argument("--out-dir", default=os.environ.get("AGENTFORGE_BACKUP_DIR", "backups"))
    parser.add_argument(
        "--key-file",
        default=os.environ.get("AGENTFORGE_BACKUP_KEY_FILE", "/etc/agentforge/backup.key"),
    )
    parser.add_argument("--no-runs", action="store_true", help="只备份数据库元数据")
    args = parser.parse_args(argv)

    runs_root = None if args.no_runs else Path(args.runs_root)
    try:
        manifest = run_backup(Path(args.db), runs_root, Path(args.out_dir), Path(args.key_file))
    except subprocess.CalledProcessError as exc:  # pragma: no cover - 环境相关
        print(json.dumps({"error": "openssl failed", "detail": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
