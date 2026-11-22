"""R15：Agent 的**受限暂存文件后端**（工具层，纯 Python 校验）。

- **读**：只读当前 run 的授权源码与只读契约（越界/符号链接逃逸即时拒绝）。
- **写**：只允许写入当前 run 的**专属暂存目录** ``run-<id>/.staging/``，且路径必须落在 `allowed_paths` 内；
  越界、`..`、绝对路径、反斜杠、符号链接逃逸、过大文件一律即时拒绝。
- **受保护**：认证/安全模板、`asset-lock.json`、`spec.json`、`verification.json` 等交付/验收产物、生成项目**固定测试**一律禁止修改；
  版本化方法仓库、Registry、FTS5 索引与发布队列在 run 目录之外，Agent **无法触达**。
- **提交**：先整体校验，再**原子应用**到 run 源码树，产出 :class:`FileChangeSet`；任一失败即回滚，**不污染正式源码**。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agentforge_platform.errors import AppException, ErrorCode

STAGING_DIRNAME = ".staging"
MAX_FILE_BYTES = 2 * 1024 * 1024  # 与装配器一致

# 交付/验收产物：Agent 不得修改（固定验收断言、资产锁、规格与报告）。
PROTECTED_FILES = frozenset({
    "asset-lock.json", "spec.json", "verification.json", "coverage.json",
    "task-plan.json", "asset-search.json", "cost-report.json", "README.md",
})
# 认证/安全模板与生成项目的固定测试：禁止修改（不改写认证与固定验收断言）。
PROTECTED_PREFIXES = (
    "backend/src/main/java/com/agentforge/template/security/",
    "backend/src/main/java/com/agentforge/template/config/",
    "backend/src/main/java/com/agentforge/template/controller/AuthController.java",
    "backend/src/main/java/com/agentforge/template/service/AuthService.java",
    "backend/src/main/java/com/agentforge/template/entity/AppUser.java",
    "backend/src/main/java/com/agentforge/template/mapper/AppUserMapper.java",
    "backend/src/main/resources/mapper/AppUserMapper.xml",
    "backend/src/test/",
    "frontend/src/api/auth",
    "frontend/src/stores/auth",
    "frontend/src/views/LoginView.vue",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rel_parts(relative: str) -> tuple[str, ...]:
    if not relative or relative.startswith(("/", "~")) or "\\" in relative:
        raise AppException(ErrorCode.BAD_REQUEST, f"非法路径：{relative!r}", status_code=422)
    parts = PurePosixPath(relative).parts
    if any(part in ("", ".", "..") for part in parts):
        raise AppException(ErrorCode.BAD_REQUEST, f"非法路径：{relative!r}", status_code=422)
    return parts


def _safe_join(root: Path, relative: str) -> Path:
    """拼接并解析路径；解析后必须仍在 root 内（拒绝 ``..``/绝对路径/符号链接逃逸）。"""
    parts = _rel_parts(relative)
    base = root.resolve()
    candidate = (base.joinpath(*parts)).resolve()
    if candidate != base and base not in candidate.parents:
        raise AppException(
            ErrorCode.BAD_REQUEST, f"路径越界（疑似符号链接逃逸）：{relative!r}", status_code=422
        )
    return candidate


def _is_protected(relative: str) -> bool:
    if PurePosixPath(relative).name in PROTECTED_FILES:
        return True
    if any(relative.startswith(prefix) for prefix in PROTECTED_PREFIXES):
        return True
    # A06：生成的跨模块 Java Facade Stub 与公共 API DTO 纳入只读受保护路径，禁止单部门私自篡改接口签名
    parts = PurePosixPath(relative).parts
    if "modules" in parts and "api" in parts and relative.endswith(".java"):
        mod_idx = parts.index("modules")
        if mod_idx + 2 < len(parts) and parts[mod_idx + 2] == "api":
            return True
    return False


def _within_allowed(relative: str, allowed: list[str]) -> bool:
    for root in allowed:
        root = root.rstrip("/")
        if root in ("", "."):  # 以 run 根为授权范围
            return True
        if relative == root or relative.startswith(root + "/"):
            return True
    return False


@dataclass(frozen=True)
class FileChange:
    path: str
    action: str  # create | modify | delete
    before_sha256: str | None
    after_sha256: str | None


@dataclass(frozen=True)
class FileChangeSet:
    changes: tuple[FileChange, ...]

    def to_dict(self) -> dict:
        return {"changes": [change.__dict__ for change in self.changes]}


class FileBackend:
    """按 run/task 隔离的受限文件后端：读授权源码、写专属暂存、原子提交。"""

    def __init__(
        self,
        run_dir: str | Path,
        allowed_paths: list[str],
        *,
        max_file_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.staging = self.run_dir / STAGING_DIRNAME
        self.staging.mkdir(parents=True, exist_ok=True)
        self.allowed_paths = list(allowed_paths)
        self.max_file_bytes = max_file_bytes

    # ---- 读：只读授权源码/契约（不可越界） ----
    def read(self, relative: str) -> bytes:
        path = _safe_join(self.run_dir, relative)
        if relative == STAGING_DIRNAME or relative.startswith(STAGING_DIRNAME + "/"):
            raise AppException(ErrorCode.BAD_REQUEST, "暂存区不通过 read 读取。", status_code=422)
        if not path.is_file():
            raise AppException(ErrorCode.NOT_FOUND, f"文件不存在：{relative}", status_code=404)
        return path.read_bytes()

    def exists(self, relative: str) -> bool:
        return _safe_join(self.run_dir, relative).is_file()

    # ---- 写：只允许写入 allowed_paths 内的专属暂存 ----
    def write(self, relative: str, data: bytes) -> None:
        self._require_writable(relative)
        if len(data) > self.max_file_bytes:
            raise AppException(ErrorCode.BAD_REQUEST, f"文件过大：{relative}", status_code=422)
        destination = _safe_join(self.staging, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

    def delete(self, relative: str) -> None:
        self._require_writable(relative)
        marker = _safe_join(self.staging, relative)
        if marker.exists():
            marker.unlink()
        # 以空文件 + 删除标记表达删除意图（提交时据此执行）
        (self.staging / (relative + ".delete")).parent.mkdir(parents=True, exist_ok=True)
        (self.staging / (relative + ".delete")).write_bytes(b"")

    def _require_writable(self, relative: str) -> None:
        _rel_parts(relative)
        if _is_protected(relative):
            raise AppException(ErrorCode.FORBIDDEN, f"受保护路径不可修改：{relative}", status_code=403)
        if not _within_allowed(relative, self.allowed_paths):
            raise AppException(
                ErrorCode.FORBIDDEN, f"路径不在授权的 allowed_paths 内：{relative}", status_code=403
            )

    # ---- 变更集与原子提交 ----
    def _staged_entries(self) -> list[tuple[str, str]]:
        """返回 [(相对路径, 动作)]；``.delete`` 后缀表示删除。"""
        entries: list[tuple[str, str]] = []
        for path in sorted(self.staging.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(self.staging).as_posix()
            if relative.endswith(".delete"):
                entries.append((relative[: -len(".delete")], "delete"))
            else:
                entries.append((relative, "write"))
        return entries

    def build_change_set(self) -> FileChangeSet:
        changes: list[FileChange] = []
        for relative, action in self._staged_entries():
            target = _safe_join(self.run_dir, relative)
            before = _sha256(target.read_bytes()) if target.is_file() else None
            if action == "delete":
                after = None
            else:
                after = _sha256((self.staging / relative).read_bytes())
            if before == after:
                continue
            kind = "create" if before is None else ("delete" if after is None else "modify")
            changes.append(FileChange(relative, kind, before, after))
        return FileChangeSet(tuple(changes))

    def commit(self) -> FileChangeSet:
        """先整体校验（路径/受保护/大小），再原子应用；任一失败回滚，不污染正式源码。"""
        entries = self._staged_entries()
        # 1) 提交前再次校验（工具层已校验，提交再校验一遍）
        for relative, _action in entries:
            self._require_writable(relative)
            staged = self.staging / relative
            if staged.is_file() and staged.stat().st_size > self.max_file_bytes:
                raise AppException(ErrorCode.BAD_REQUEST, f"文件过大：{relative}", status_code=422)

        # 2) 先据暂存与源码树生成变更集（含 before/after 哈希），再应用。
        change_set = self.build_change_set()

        backups: dict[str, bytes | None] = {}
        applied: list[str] = []
        try:
            for relative, action in entries:
                target = _safe_join(self.run_dir, relative)
                backups[relative] = target.read_bytes() if target.is_file() else None
                if action == "delete":
                    if target.is_file():
                        target.unlink()
                else:
                    data = (self.staging / relative).read_bytes()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                applied.append(relative)
        except Exception:
            for relative in reversed(applied):  # 回滚已应用项
                target = _safe_join(self.run_dir, relative)
                before = backups.get(relative)
                if before is None:
                    if target.exists():
                        target.unlink()
                else:
                    target.write_bytes(before)
            raise
        for relative, _action in entries:  # 清理已提交的暂存
            staged = self.staging / relative
            if staged.is_file():
                staged.unlink()
            delete_marker = self.staging / (relative + ".delete")
            if delete_marker.is_file():
                delete_marker.unlink()
        return change_set


__all__ = [
    "MAX_FILE_BYTES",
    "PROTECTED_FILES",
    "PROTECTED_PREFIXES",
    "STAGING_DIRNAME",
    "FileBackend",
    "FileChange",
    "FileChangeSet",
]
