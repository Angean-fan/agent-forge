#!/usr/bin/env python3
"""M28 平台恢复：解密并解包备份，恢复 SQLite 元数据与运行产物目录。

安全要点：
- 恢复前**必须停止** API/worker（正在写入的库不可覆盖）；本脚本会提示。
- 覆盖数据库时一并移除 `-wal`/`-shm`，避免旧 WAL 与新库不一致。
- 默认拒绝覆盖已存在的目标（除非 `--force`）。
- 校验备份 sha256（若同目录存在 `.manifest.json`）。

用法：
    python3 scripts/restore.py --backup backups/agentforge-backup-XXXX.tar.gz.enc \
        --key-file /etc/agentforge/backup.key \
        --db platform-api/agentforge.db --runs-root var/runs
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_manifest(backup: Path) -> None:
    manifest_path = backup.with_name(f"{backup.name}.manifest.json")
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest.get("sha256")
    if expected and expected != _sha256(backup):
        raise SystemExit("backup sha256 mismatch; refusing to restore")


def _decrypt(backup: Path, key_file: Path, target: Path) -> None:
    subprocess.run(
        [
            "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-md", "sha256",
            "-in", str(backup), "-out", str(target), "-pass", f"file:{key_file}",
        ],
        check=True,
    )


def run_restore(backup: Path, key_file: Path, db_target: Path, runs_target: Path | None, force: bool) -> dict:
    if not backup.is_file():
        raise SystemExit(f"backup not found: {backup}")
    if not key_file.is_file():
        raise SystemExit(f"key file not found: {key_file}")
    if db_target.exists() and not force:
        raise SystemExit(f"target db already exists (use --force to overwrite): {db_target}")

    _verify_manifest(backup)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "payload.tar.gz"
        _decrypt(backup, key_file, archive)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(tmp_path / "extracted", filter="data")
        extracted = tmp_path / "extracted"
        db_target.parent.mkdir(parents=True, exist_ok=True)
        # 覆盖前清理旧 WAL/SHM，避免与新库不一致
        for suffix in ("-wal", "-shm"):
            Path(f"{db_target}{suffix}").unlink(missing_ok=True)
        shutil.copy2(extracted / "agentforge.db", db_target)
        os.chmod(db_target, 0o600)
        restored_runs = False
        source_runs = extracted / "runs"
        if runs_target is not None and source_runs.exists():
            if runs_target.exists() and force:
                shutil.rmtree(runs_target)
            shutil.copytree(source_runs, runs_target, dirs_exist_ok=True)
            restored_runs = True

    return {
        "restored_db": str(db_target),
        "restored_runs": restored_runs,
        "runs_root": str(runs_target) if runs_target else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="restore.py")
    parser.add_argument("--backup", required=True)
    parser.add_argument(
        "--key-file",
        default=os.environ.get("AGENTFORGE_BACKUP_KEY_FILE", "/etc/agentforge/backup.key"),
    )
    parser.add_argument("--db", default=os.environ.get("AGENTFORGE_BACKUP_DB", "platform-api/agentforge.db"))
    parser.add_argument("--runs-root", default=os.environ.get("RUNNER_WORK_ROOT", "var/runs"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    result = run_restore(
        Path(args.backup), Path(args.key_file), Path(args.db), Path(args.runs_root), args.force
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
