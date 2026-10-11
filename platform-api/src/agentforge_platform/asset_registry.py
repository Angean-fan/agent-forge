"""资产 manifest 与只读本地注册表（M13）。

统一模板与能力包的 manifest schema，并提供按 ``kind + id + version`` 定位、校验
内容 checksum、按 Profile 判兼容的只读检索。资产内容只读，不做后台资产市场。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agentforge_platform.errors import AppException, ErrorCode

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ROOTS = (REPO_ROOT / "templates", REPO_ROOT / "capability-packs")
_EXCLUDED_DIRS = {"node_modules", "target", "dist", "build", "__pycache__", ".git"}
_MANIFEST_NAME = "manifest.json"

_STRICT = ConfigDict(extra="forbid")


def compute_checksum(asset_dir: str | Path) -> str:
    """资产内容 checksum：对（相对路径 + 文件内容 hash）排序后取 SHA-256。

    排除资产自身的 manifest.json 与构建产物目录，保证与 git 状态可复现。
    """
    asset_dir = Path(asset_dir)
    hasher = hashlib.sha256()
    for path in sorted(asset_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(asset_dir)
        if relative.name == _MANIFEST_NAME:
            continue
        if any(part in _EXCLUDED_DIRS for part in relative.parts):
            continue
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        hasher.update(relative.as_posix().encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(file_hash.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


class RenderEntry(BaseModel):
    model_config = _STRICT

    template: str
    target: str


class AssetManifest(BaseModel):
    """模板 / 能力包统一 manifest。契约字段全部保留。"""

    model_config = _STRICT

    id: str
    version: str
    kind: str
    compatible_profiles: list[str]
    requires: list[str]
    provides: list[str]
    conflicts: list[str]
    parameter_schema: dict[str, str]
    extension_points: list[str]
    owned_paths: list[str]
    checks: list[str]
    checksum: str
    license: str
    provided_capabilities: list[str] = []
    renders: list[RenderEntry] = []
    dependencies: list[dict[str, str]] = []


class AssetRegistry:
    """只读本地资产注册表：扫描根目录与其一级子目录中的 manifest.json。"""

    def __init__(self, roots: list[str | Path] | None = None) -> None:
        self._assets: dict[tuple[str, str, str], tuple[AssetManifest, Path]] = {}
        for root in roots if roots is not None else DEFAULT_ROOTS:
            self._scan(Path(root))

    def _scan(self, root: Path) -> None:
        candidates: list[Path] = []
        if (root / _MANIFEST_NAME).is_file():
            candidates.append(root)
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir() and (child / _MANIFEST_NAME).is_file():
                    candidates.append(child)
        for directory in candidates:
            manifest = AssetManifest.model_validate_json(
                (directory / _MANIFEST_NAME).read_text(encoding="utf-8")
            )
            self._assets[(manifest.kind, manifest.id, manifest.version)] = (manifest, directory)

    def all(self) -> list[AssetManifest]:
        return sorted(
            (manifest for manifest, _ in self._assets.values()),
            key=lambda m: (m.kind, m.id, m.version),
        )

    def get(self, kind: str, asset_id: str, version: str) -> tuple[AssetManifest, Path]:
        key = (kind, asset_id, version)
        if key not in self._assets:
            raise AppException(
                ErrorCode.ASSET_NOT_FOUND,
                f"资产不存在：{kind}/{asset_id}@{version}",
                status_code=404,
            )
        return self._assets[key]

    def verify_checksum(self, kind: str, asset_id: str, version: str) -> AssetManifest:
        manifest, directory = self.get(kind, asset_id, version)
        actual = compute_checksum(directory)
        if actual != manifest.checksum:
            raise AppException(
                ErrorCode.ASSET_CHECKSUM_MISMATCH,
                f"资产 {asset_id}@{version} checksum 不匹配",
                status_code=409,
            )
        return manifest

    def resolve(
        self, kind: str, asset_id: str, version: str, profile: str
    ) -> tuple[AssetManifest, Path]:
        manifest, directory = self.get(kind, asset_id, version)
        if profile not in manifest.compatible_profiles:
            raise AppException(
                ErrorCode.ASSET_INCOMPATIBLE,
                f"资产 {asset_id}@{version} 与 Profile '{profile}' 不兼容",
                status_code=422,
            )
        return manifest, directory


def default_registry() -> AssetRegistry:
    return AssetRegistry(list(DEFAULT_ROOTS))


def _write_checksums() -> None:
    for root in DEFAULT_ROOTS:
        for directory in [root, *sorted(p for p in root.iterdir() if p.is_dir())]:
            manifest_path = directory / _MANIFEST_NAME
            if not manifest_path.is_file():
                continue
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["checksum"] = compute_checksum(directory)
            manifest_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(f"updated checksum: {directory.name}")


def main(argv: list[str]) -> int:
    if "--update" in argv:
        _write_checksums()
        return 0
    registry = default_registry()
    for manifest in registry.all():
        print(f"{manifest.kind:16} {manifest.id}@{manifest.version} checksum={manifest.checksum[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
