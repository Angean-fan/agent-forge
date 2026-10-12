"""R06B：方法仓库（methods/）与可重建 SQLite/FTS5 索引。

覆盖：默认仓库全量构建、重建等价、增量增删、删除后重建等价、checksum 篡改拒绝、
索引损坏恢复、原子发布（失败不落半成品）、并发只读、不跟随符号链接、拒绝非方法单元。
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest

from agentforge_platform.asset_registry import compute_checksum
from agentforge_platform.errors import AppException
from agentforge_platform.method_index import (
    build_index,
    is_valid_index,
    scan_methods,
    search,
    update_index,
)

REPO = Path(__file__).resolve().parents[2]
METHODS = REPO / "methods"


def _manifest(method_id: str, *, kind: str = "method-unit", version: str = "1.0.0") -> dict:
    return {
        "id": method_id, "version": version, "kind": kind,
        "compatible_profiles": ["demo-ts-mp"], "requires": [], "provides": [],
        "conflicts": [], "parameter_schema": {}, "extension_points": [],
        "owned_paths": ["backend/src/main/java/com/agentforge/template/util/"],
        "checks": [], "checksum": "", "license": "internal",
        "symbol": f"pkg.{method_id.replace('-', '_')}.run",
        "signature": "void run()", "layer": "util", "language": "java",
        "framework": "spring-boot", "source_provenance": "test:r06b",
    }


def _make_method(root: Path, method_id: str, *, kind: str = "method-unit") -> Path:
    directory = root / method_id / "1.0.0"
    (directory / "src").mkdir(parents=True)
    (directory / "src" / "Helper.java").write_text("class Helper {}", encoding="utf-8")
    payload = _manifest(method_id, kind=kind)
    payload["checksum"] = compute_checksum(directory)
    (directory / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return directory


# --- 默认仓库：全量构建 / 检索 / 等价 -----------------------------------------

def test_default_store_builds_and_searches(tmp_path: Path) -> None:
    index = tmp_path / "idx.sqlite"

    result = build_index(METHODS, index)

    assert result.count >= 3
    assert result.revision
    assert is_valid_index(index)
    assert search("normalize", path=index)[0]["asset_id"] == "date-normalizer"
    assert search("guard", path=index)[0]["asset_id"] == "status-guard"
    assert search("validation", path=index)[0]["asset_id"] == "field-validator"


def test_rebuild_is_equivalent(tmp_path: Path) -> None:
    first = build_index(METHODS, tmp_path / "a.sqlite")
    second = build_index(METHODS, tmp_path / "b.sqlite")

    assert first.revision == second.revision
    assert first.count == second.count


def test_delete_then_rebuild_is_equivalent(tmp_path: Path) -> None:
    index = tmp_path / "idx.sqlite"
    built = build_index(METHODS, index)

    index.unlink()
    rebuilt = build_index(METHODS, index)

    assert rebuilt.revision == built.revision


# --- 增量更新 -----------------------------------------------------------------

def test_incremental_add_and_remove(tmp_path: Path) -> None:
    root = tmp_path / "methods"
    shutil.copytree(METHODS, root)
    index = tmp_path / "idx.sqlite"
    base = build_index(root, index)

    _make_method(root, "added-method")
    grown = update_index(root, index)
    assert grown.count == base.count + 1
    assert search("added", path=index)[0]["asset_id"] == "added-method"

    shutil.rmtree(root / "added-method")
    shrunk = update_index(root, index)
    assert shrunk.count == base.count
    assert search("added", path=index) == []


# --- 篡改 / 损坏 / 原子 --------------------------------------------------------

def test_checksum_tamper_is_rejected_and_old_index_survives(tmp_path: Path) -> None:
    root = tmp_path / "methods"
    shutil.copytree(METHODS, root)
    index = tmp_path / "idx.sqlite"
    good = build_index(root, index)

    # 篡改方法内容但保留 manifest 里的 checksum
    (root / "date-normalizer" / "1.0.0" / "src" / "DateNormalizer.java").write_text(
        "class Tampered {}", encoding="utf-8"
    )

    with pytest.raises(AppException) as error:
        build_index(root, index)
    assert error.value.code == "ASSET_CHECKSUM_MISMATCH"
    # 原子发布：失败的构建不覆盖已发布的可用索引（未落半成品）
    assert is_valid_index(index)
    assert search("normalize", path=index)[0]["asset_id"] == "date-normalizer"
    assert good.revision


def test_corrupt_index_is_recovered(tmp_path: Path) -> None:
    index = tmp_path / "idx.sqlite"
    build_index(METHODS, index)

    index.write_bytes(b"not-a-sqlite-database")  # 索引损坏
    assert not is_valid_index(index)

    recovered = update_index(METHODS, index)

    assert recovered.count >= 3
    assert is_valid_index(index)
    assert search("normalize", path=index)[0]["asset_id"] == "date-normalizer"


def test_missing_index_rebuilds_on_update(tmp_path: Path) -> None:
    index = tmp_path / "nested" / "idx.sqlite"

    result = update_index(METHODS, index)

    assert result.count >= 3
    assert index.is_file()


def test_concurrent_reads(tmp_path: Path) -> None:
    index = tmp_path / "idx.sqlite"
    build_index(METHODS, index)
    errors: list[Exception] = []
    hits: list[int] = []

    def worker() -> None:
        try:
            hits.append(len(search("normalize", path=index)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert hits == [1, 1, 1, 1]


# --- 安全：不跟随符号链接 / 拒绝非方法单元 ------------------------------------

def test_symlinked_dir_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "methods"
    _make_method(root, "real")
    external = tmp_path / "external"
    external.mkdir()
    _make_method(external, "hidden")  # 宿主敏感路径外的“方法”
    (root / "linkdir").symlink_to(external, target_is_directory=True)

    ids = {record.id for record in scan_methods(root)}

    assert ids == {"real"}  # 符号链接目录未被跟随


def test_non_method_unit_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "methods"
    _make_method(root, "sneaky", kind="capability-pack")

    with pytest.raises(AppException) as error:
        scan_methods(root)

    assert error.value.code == "ASSET_CONFLICT"
