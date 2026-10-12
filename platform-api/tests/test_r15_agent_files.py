"""R15：受限暂存文件后端。

覆盖：授权路径可暂存/提交并生成 FileChangeSet；越界/受保护/遍历/绝对路径/过大/符号链接逃逸即时拒绝；
提交原子应用且失败回滚不污染源码；删除同样受约束。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentforge_platform.agent_files import FileBackend
from agentforge_platform.errors import AppException


def _backend(tmp_path: Path, allowed: list[str] | None = None) -> tuple[FileBackend, Path]:
    run_dir = tmp_path / "run-1"
    (run_dir / "backend/src/main/java/com/agentforge/template/service").mkdir(parents=True)
    (run_dir / "backend/src/main/java/com/agentforge/template/service/ReportService.java").write_text(
        "class ReportService {}", encoding="utf-8"
    )
    (run_dir / "asset-lock.json").write_text("{}", encoding="utf-8")
    backend = FileBackend(run_dir, allowed or ["backend/src/main/java/com/agentforge/template/service/"])
    return backend, run_dir


# --- 正常路径 ----------------------------------------------------------------

def test_write_commit_creates_change_set(tmp_path: Path) -> None:
    backend, run_dir = _backend(tmp_path)
    rel = "backend/src/main/java/com/agentforge/template/service/Helper.java"

    backend.write(rel, b"class Helper {}")
    change_set = backend.commit()

    assert (run_dir / rel).read_bytes() == b"class Helper {}"
    assert [c.action for c in change_set.changes] == ["create"]
    assert change_set.changes[0].before_sha256 is None
    assert change_set.changes[0].after_sha256
    assert not (backend.staging / rel).exists()  # 提交后清理暂存


def test_modify_existing_records_before_and_after(tmp_path: Path) -> None:
    backend, _run_dir = _backend(tmp_path)
    rel = "backend/src/main/java/com/agentforge/template/service/ReportService.java"

    backend.write(rel, b"class ReportService { /* extended */ }")
    change_set = backend.commit()

    change = change_set.changes[0]
    assert change.action == "modify" and change.before_sha256 and change.after_sha256
    assert change.before_sha256 != change.after_sha256


def test_delete_is_staged_and_applied(tmp_path: Path) -> None:
    backend, run_dir = _backend(tmp_path)
    rel = "backend/src/main/java/com/agentforge/template/service/ReportService.java"

    backend.delete(rel)
    change_set = backend.commit()

    assert not (run_dir / rel).exists()
    assert change_set.changes[0].action == "delete"


# --- 即时拒绝 ----------------------------------------------------------------

def test_write_outside_allowed_paths_is_rejected(tmp_path: Path) -> None:
    backend, _ = _backend(tmp_path)
    with pytest.raises(AppException) as error:
        backend.write("backend/src/main/java/com/agentforge/template/controller/Evil.java", b"x")
    assert error.value.code == "FORBIDDEN"


def test_write_protected_paths_is_rejected(tmp_path: Path) -> None:
    backend, _ = _backend(tmp_path, allowed=["."])
    for rel in ("asset-lock.json", "backend/src/main/java/com/agentforge/template/security/JwtService.java",
                "backend/src/test/java/com/agentforge/template/TaskApiTest.java"):
        with pytest.raises(AppException) as error:
            backend.write(rel, b"x")
        assert error.value.code == "FORBIDDEN", rel


def test_path_traversal_and_absolute_are_rejected(tmp_path: Path) -> None:
    backend, _ = _backend(tmp_path, allowed=["."])
    for rel in ("../escape.txt", "/etc/passwd", "backend/../x.txt", "a\\b.txt"):
        with pytest.raises(AppException) as error:
            backend.write(rel, b"x")
        assert error.value.code == "BAD_REQUEST", rel


def test_oversize_file_is_rejected(tmp_path: Path) -> None:
    backend, _ = _backend(tmp_path, allowed=["."])
    backend = FileBackend(backend.run_dir, ["."], max_file_bytes=8)
    with pytest.raises(AppException) as error:
        backend.write("backend/big.txt", b"x" * 16)
    assert error.value.code == "BAD_REQUEST"


def test_symlink_escape_is_rejected_on_read_and_commit(tmp_path: Path) -> None:
    backend, run_dir = _backend(tmp_path, allowed=["."])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (run_dir / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AppException) as read_error:  # 读取经符号链接逃逸
        backend.read("link/secret.txt")
    assert read_error.value.code == "BAD_REQUEST"

    backend.write("link/x.txt", b"x")  # 写入暂存本身无符号链接
    with pytest.raises(AppException) as commit_error:  # 提交目标经符号链接越界
        backend.commit()
    assert commit_error.value.code == "BAD_REQUEST"
    assert not (outside / "x.txt").exists()  # 未写到受控目录之外


# --- 失败回滚 ----------------------------------------------------------------

def test_commit_failure_rolls_back_without_polluting(tmp_path: Path) -> None:
    backend, run_dir = _backend(tmp_path, allowed=["."])
    (run_dir / "backend/blocker").write_text("i am a file", encoding="utf-8")  # 使 backend/blocker 无法成为目录

    backend.write("backend/a.txt", b"new")
    backend.write("backend/blocker/sub.txt", b"boom")  # 目标父路径是文件 → 应用阶段失败

    with pytest.raises(OSError):
        backend.commit()

    assert not (run_dir / "backend/a.txt").exists()  # 已应用项被回滚，源码未被污染
