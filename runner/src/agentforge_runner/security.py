"""路径安全：工件目录必须落在 Runner 工作根之内，拒绝越界与符号链接逃逸。"""

from __future__ import annotations

from pathlib import Path


class UnsafePathError(Exception):
    pass


def resolve_artifact_dir(work_root: str | Path, requested: str) -> Path:
    if not requested or requested.startswith("/") or "\\" in requested:
        raise UnsafePathError(f"非法路径：{requested!r}")
    if any(part in ("", ".", "..") for part in Path(requested).parts):
        raise UnsafePathError(f"非法路径：{requested!r}")

    root = Path(work_root).resolve()
    candidate = (root / requested).resolve()
    if candidate != root and root not in candidate.parents:
        raise UnsafePathError(f"路径越界：{requested}")
    if not candidate.is_dir():
        raise UnsafePathError(f"目录不存在：{requested}")
    return candidate
