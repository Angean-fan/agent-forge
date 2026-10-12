"""确定性代码装配器（M15）。

``spec + contract + asset-lock → 独立 run 目录的完整源码``：
基础工程 COPY，业务文件用 Jinja2 渲染进暂存目录；核对文件数/大小与哈希后原子提交。

约束：安全路径、文件数量/大小上限、非法文件名与符号链接逃逸一律拒绝；同一路径禁止
重复写入（不做覆盖）；不修改原版模板或固定测试文件。
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict

from agentforge_platform.asset_registry import (
    AssetRegistry,
    MethodUnitManifest,
    default_registry,
)
from agentforge_platform.capability_packs import inject_dependencies, render_pack_files
from agentforge_platform.contract import BusinessContract, compile_from_spec
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.method_index import DEFAULT_METHODS_ROOT
from agentforge_platform.resolver import AssetLock, resolve_assets
from agentforge_platform.spec import SoftwareSpecification

_STRICT = ConfigDict(extra="forbid")
_EXCLUDED_COPY = {
    "manifest.json", "node_modules", "target", "dist", "build", "__pycache__", ".git",
    ".DS_Store", "Thumbs.db",
}
_TEMPLATE_README = "README.md"
_ASSET_LOCK_NAME = "asset-lock.json"
_README_NAME = "README.md"
_SPEC_NAME = "spec.json"
_CONTRACT_NAME = "contract.json"


@dataclass(frozen=True)
class AssemblyLimits:
    max_files: int = 2000
    max_file_bytes: int = 2 * 1024 * 1024


class AssemblyError(AppException):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.ASSEMBLY_ERROR, message, status_code=422, retryable=False)


class AssembledFile(BaseModel):
    model_config = _STRICT

    path: str
    sha256: str
    size: int


class AssemblyResult(BaseModel):
    model_config = _STRICT

    output_dir: str
    lock_hash: str
    manifest_hash: str
    files: list[AssembledFile]


def _validate_rel(rel: str) -> None:
    if not rel or rel.startswith("/") or "\\" in rel:
        raise AssemblyError(f"非法路径：{rel!r}")
    parts = PurePosixPath(rel).parts
    if any(part in ("", ".", "..") for part in parts):
        raise AssemblyError(f"非法路径：{rel!r}")
    for part in parts:
        if any(ord(char) < 0x20 for char in part):
            raise AssemblyError(f"非法文件名：{part!r}")


def _is_within(root: Path, dest: Path) -> bool:
    resolved_root = root.resolve()
    resolved_dest = dest.resolve()
    return resolved_dest == resolved_root or resolved_root in resolved_dest.parents


class _Writer:
    def __init__(self, root: Path, limits: AssemblyLimits) -> None:
        self.root = root
        self.limits = limits
        self.seen: set[str] = set()

    def write(self, subdir: str, rel: str, data: bytes) -> None:
        if subdir:
            _validate_rel(subdir)
        _validate_rel(rel)
        key = f"{subdir}/{rel}" if subdir else rel
        if key in self.seen:
            raise AssemblyError(f"路径重复写入（禁止覆盖）：{key}")
        if len(data) > self.limits.max_file_bytes:
            raise AssemblyError(f"文件过大：{key}")
        destination = (self.root / subdir / rel) if subdir else (self.root / rel)
        if not _is_within(self.root, destination):
            raise AssemblyError(f"路径越界：{key}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        self.seen.add(key)
        if len(self.seen) > self.limits.max_files:
            raise AssemblyError("文件数超过上限")


def _copy_template(template_dir: Path, writer: _Writer) -> None:
    for path in sorted(template_dir.rglob("*")):
        rel = path.relative_to(template_dir).as_posix()
        # 先跳过排除项（构建产物目录可能含符号链接），再做符号链接与越界检查。
        if any(part in _EXCLUDED_COPY for part in PurePosixPath(rel).parts):
            continue
        if path.is_symlink():
            raise AssemblyError(f"模板包含符号链接，拒绝：{path}")
        if path.is_dir():
            continue
        if rel == _TEMPLATE_README:
            continue
        writer.write("", rel, path.read_bytes())


def _render_packs(
    contract: BusinessContract, lock: AssetLock, registry: AssetRegistry, writer: _Writer
) -> None:
    # R10：多实体逐实体渲染；共享文件（如 PageResult）内容一致，去重写入一次。
    entity_names = [matrix.entity for matrix in contract.permissions] or [None]
    for asset in lock.assets:
        if asset.kind != "capability-pack":
            continue
        manifest, pack_dir = registry.get("capability-pack", asset.id, asset.version)
        rendered: dict[str, bytes] = {}
        for entity_name in entity_names:
            per_entity: dict[str, bytes] = {}
            for rel, content in render_pack_files(pack_dir, contract, entity_name=entity_name):
                if rel in per_entity:  # 同一实体内重复目标：能力包编写错误，拒绝
                    raise AssemblyError(f"能力包渲染出重复目标：{rel}")
                per_entity[rel] = content.encode("utf-8")
            for rel, data in per_entity.items():
                # 跨实体同路径：内容一致（如 PageResult）则去重，不一致即冲突。
                if rel in rendered and rendered[rel] != data:
                    raise AssemblyError(f"能力包渲染冲突（跨实体同路径不同内容）：{rel}")
                rendered[rel] = data
        for rel, data in rendered.items():
            writer.write(manifest.target_dir, rel, data)
        if manifest.target_dir and manifest.dependencies:
            inject_dependencies(
                writer.root / manifest.target_dir / "pom.xml", manifest.dependencies
            )


def _copy_method_units(lock: AssetLock, writer: _Writer, methods_root: Path) -> None:
    """把锁定的方法单元 ``src/`` 文件 COPY 到其 ``target_dir``/``owned_paths[0]``。

    只做 COPY（不改写业务代码）；路径写重由 ``_Writer`` 拒绝（不覆盖原能力）。
    """
    for unit in lock.method_units:
        directory = Path(methods_root) / unit.id / unit.version
        if not directory.is_dir():
            raise AssemblyError(f"方法单元缺失：{unit.id}@{unit.version}")
        manifest = MethodUnitManifest.model_validate_json(
            (directory / "manifest.json").read_text(encoding="utf-8")
        )
        target = manifest.target_dir or (manifest.owned_paths[0] if manifest.owned_paths else "")
        target = target.rstrip("/")
        if not target:
            raise AssemblyError(f"方法单元未声明安装目标：{unit.id}@{unit.version}")
        source = directory / "src"
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise AssemblyError(f"方法单元包含符号链接，拒绝：{path}")
            if not path.is_file():
                continue
            rel = path.relative_to(source).as_posix()
            writer.write(target, rel, path.read_bytes())


def _readme(spec: SoftwareSpecification, contract: BusinessContract, lock: AssetLock) -> str:
    entity = contract.permissions[0].entity if contract.permissions else "未知实体"
    lines = [
        f"# {entity} 生成项目（草稿）",
        "",
        f"目标：{spec.goal}",
        "",
        "## 结构",
        "- `backend/`：Spring Boot + MyBatis-Plus",
        "- `frontend/`：Vue 3 + TypeScript",
        "- `compose.yaml` / `.env.example`",
        f"- `{_ASSET_LOCK_NAME}`：本次装配的资产与 checksum",
        "",
        "## 接口",
    ]
    lines += [f"- `{route.method} {route.path}`（{route.operation}）" for route in contract.routes]
    lines += [
        "",
        "## 运行",
        "- 后端：`cd backend && mvn spring-boot:run`（MySQL 见 `compose.yaml`）",
        "- 前端：`cd frontend && npm ci && npm run dev`",
        "",
        f"- profile：`{lock.profile}`",
        "",
    ]
    return "\n".join(lines)


def _hash_file(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _collect_manifest(root: Path) -> list[AssembledFile]:
    files: list[AssembledFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        files.append(
            AssembledFile(
                path=path.relative_to(root).as_posix(),
                sha256=_hash_file(data),
                size=len(data),
            )
        )
    return files


def _manifest_hash(files: list[AssembledFile]) -> str:
    hasher = hashlib.sha256()
    for entry in files:
        hasher.update(entry.path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(entry.sha256.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def assemble(
    contract: BusinessContract,
    lock: AssetLock,
    registry: AssetRegistry,
    output_dir: str | Path,
    *,
    spec: SoftwareSpecification,
    limits: AssemblyLimits | None = None,
    methods_root: str | Path | None = None,
) -> AssemblyResult:
    limits = limits or AssemblyLimits()
    final = Path(output_dir)
    staging = final.with_name(f"{final.name}.staging")
    if staging.exists():
        shutil.rmtree(staging)
    if final.exists():
        shutil.rmtree(final)
    staging.mkdir(parents=True)

    templates = [asset for asset in lock.assets if asset.kind == "template"]
    if len(templates) != 1:
        raise AssemblyError("锁清单模板数量异常")
    _, template_dir = registry.get("template", templates[0].id, templates[0].version)

    writer = _Writer(staging, limits)
    _copy_template(template_dir, writer)
    _render_packs(contract, lock, registry, writer)
    if lock.method_units:
        _copy_method_units(
            lock, writer,
            Path(methods_root) if methods_root is not None else Path(DEFAULT_METHODS_ROOT),
        )
    writer.write("", _ASSET_LOCK_NAME, (lock.model_dump_json(indent=2) + "\n").encode("utf-8"))
    writer.write("", _SPEC_NAME, (spec.model_dump_json(indent=2) + "\n").encode("utf-8"))
    writer.write(
        "", _CONTRACT_NAME, (contract.model_dump_json(indent=2) + "\n").encode("utf-8")
    )
    writer.write("", _README_NAME, _readme(spec, contract, lock).encode("utf-8"))

    files = _collect_manifest(staging)
    staging.replace(final)  # 暂存目录核对后原子提交
    return AssemblyResult(
        output_dir=str(final),
        lock_hash=lock.lock_hash,
        manifest_hash=_manifest_hash(files),
        files=files,
    )


def assemble_from_spec(spec: SoftwareSpecification, output_dir: str | Path) -> AssemblyResult:
    registry = default_registry()
    # 资产兼容性按 profile_id 匹配；具体 profile_version 由 spec 固定。
    lock = resolve_assets(registry, spec.technical_profile.profile_id)
    contract = compile_from_spec(spec)
    return assemble(contract, lock, registry, output_dir, spec=spec)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: python -m agentforge_platform.assembler <spec.json> <output-dir>",
            file=sys.stderr,
        )
        return 2
    spec = SoftwareSpecification.model_validate_json(Path(argv[0]).read_text(encoding="utf-8"))
    result = assemble_from_spec(spec, argv[1])
    print(f"assembled {len(result.files)} files -> {result.output_dir}")
    print(f"lock_hash={result.lock_hash}")
    print(f"manifest_hash={result.manifest_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
