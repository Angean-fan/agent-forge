"""FP03：只读后端公共底座装配与跨模块 ``api/*Facade.java`` 骨架自动生成（``factory_base_builder.py``）。

核心职责（全程确定性执行，**0 模型调用**）：
1. 通过 A03 :class:`AssetMaterializer` 从本地 PostgreSQL（或 SQLite 单测库）物化锁定的已认证
   Spring Boot 基础模板到本地只读缓存，并核验 ``AssetLock`` 哈希；
2. 复用 ``assembler.py`` 安全路径校验与上限护栏，在 ``run_dir/backend_base/`` 下原子装配只读后端底座
   （含 ``pom.xml``、``common/``、``security/``、``config/``、``exception/``、基础 Flyway SQL 与模板基座测试）；
3. 调用 A06 :func:`generate_java_facade_stubs` 为冻结蓝图中的每个部门确定性生成只读
   ``modules/<dept_id>/api/*Facade.java`` 与公共 DTO 骨架；
4. 产出只读文件哈希清单 ``readonly-manifest.json``（登记全部底座文件与 Facade Stub 的 SHA-256），
   提供 :func:`verify_base_integrity` 与 :func:`assert_base_integrity` 供总装拦截任何底座/跨模块接口篡改。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict

from agentforge_platform.agent_files import PROTECTED_PREFIXES
from agentforge_platform.assembler import (
    _EXCLUDED_COPY,
    AssemblyError,
    AssemblyLimits,
    _is_within,
    _validate_rel,
)
from agentforge_platform.asset_cache import AssetMaterializer, remove_readonly_tree
from agentforge_platform.blueprint_contracts import (
    ArchitectureBlueprint,
    generate_java_facade_stubs,
)
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.resolver import AssetLock, verify_lock_hash

_STRICT = ConfigDict(extra="forbid")

BACKEND_BASE_DIRNAME = "backend_base"
READONLY_MANIFEST_NAME = "readonly-manifest.json"
_BUILD_OUTPUT_DIRS = frozenset({"target", "build", ".git", ".DS_Store"})


class BaseManifestEntry(BaseModel):
    """只读底座与跨模块 Facade Stub 清单条目。"""

    model_config = _STRICT

    path: str
    sha256: str
    size: int
    category: str  # base_template | base_common | facade_stub | shared_dto
    protected: bool = True


class BaseManifest(BaseModel):
    """写入 ``backend_base/readonly-manifest.json`` 的确定性只读哈希清单。"""

    model_config = _STRICT

    schema_version: str = "1.0"
    blueprint_hash: str
    blueprint_version: int
    lock_hash: str
    template_ref: str
    base_package: str
    protected_prefixes: list[str]
    facade_stub_paths: list[str]
    shared_dto_paths: list[str]
    manifest_hash: str
    files: list[BaseManifestEntry]


class BaseBuildResult(BaseModel):
    """``build_backend_base`` 返回的结构化结果。"""

    model_config = _STRICT

    base_dir: str
    backend_project_dir: str
    manifest_path: str
    blueprint_hash: str
    lock_hash: str
    template_ref: str
    manifest_hash: str
    facade_stub_paths: list[str]
    shared_dto_paths: list[str]
    protected_prefixes: list[str]
    files: list[BaseManifestEntry]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compute_manifest_hash(entries: Sequence[BaseManifestEntry]) -> str:
    hasher = hashlib.sha256()
    for entry in sorted(entries, key=lambda e: e.path):
        hasher.update(entry.path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(entry.sha256.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _render_common_api_response_java(base_package: str) -> str:
    pkg = f"{base_package}.common"
    return (
        f"package {pkg};\n"
        "\n"
        "/**\n"
        " * Read-only uniform API response wrapper provided by the factory backend base.\n"
        " * DO NOT MODIFY inside individual department staging.\n"
        " */\n"
        "public record ApiResponse<T>(String code, String message, T data) {\n"
        "\n"
        "    public static <T> ApiResponse<T> ok(T data) {\n"
        '        return new ApiResponse<>("OK", "success", data);\n'
        "    }\n"
        "\n"
        "    public static <T> ApiResponse<T> error(String code, String message) {\n"
        "        return new ApiResponse<>(code, message, null);\n"
        "    }\n"
        "}\n"
    )


def _render_fallback_base_sql() -> str:
    return (
        "-- Read-only factory backend base migration (generated when template lacks V1/V001).\n"
        "CREATE TABLE IF NOT EXISTS app_user (\n"
        "    id            BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,\n"
        "    username      VARCHAR(64)  NOT NULL,\n"
        "    password_hash VARCHAR(100) NOT NULL,\n"
        "    role          VARCHAR(16)  NOT NULL,\n"
        "    enabled       BOOLEAN      NOT NULL DEFAULT TRUE,\n"
        "    created_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
        "    CONSTRAINT uq_app_user_username UNIQUE (username)\n"
        ");\n"
    )


def _copy_backend_template(
    template_dir: Path,
    dest_root: Path,
    limits: AssemblyLimits,
) -> dict[str, str]:
    """安全拷贝模板的 ``backend/`` 子树到 ``dest_root/backend/``，返回 ``{rel_path: category}``。"""
    source_backend = template_dir / "backend"
    if not source_backend.is_dir() or source_backend.is_symlink():
        raise AssemblyError(f"已认证模板缺少合法 backend/ 目录：{template_dir}")
    if not (source_backend / "pom.xml").is_file():
        raise AssemblyError(f"已认证模板缺少 backend/pom.xml：{template_dir}")

    categories: dict[str, str] = {}
    for path in sorted(source_backend.rglob("*")):
        rel_in_backend = path.relative_to(source_backend).as_posix()
        if any(part in _EXCLUDED_COPY for part in PurePosixPath(rel_in_backend).parts):
            continue
        if path.is_symlink():
            raise AssemblyError(f"模板包含符号链接，拒绝：{path}")
        if path.is_dir():
            continue

        rel = f"backend/{rel_in_backend}"
        _validate_rel(rel)
        if rel in categories:
            raise AssemblyError(f"底座路径重复写入：{rel}")

        data = path.read_bytes()
        if len(data) > limits.max_file_bytes:
            raise AssemblyError(f"底座文件超过大小上限：{rel}")

        target = dest_root / rel
        if not _is_within(dest_root, target):
            raise AssemblyError(f"底座文件路径越界：{rel}")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        categories[rel] = "base_template"
        if len(categories) > limits.max_files:
            raise AssemblyError("底座文件数超过上限")

    return categories


def _ensure_common_and_base_migration(
    dest_root: Path,
    blueprint: ArchitectureBlueprint,
    categories: dict[str, str],
) -> None:
    """补齐共享 ``common/ApiResponse.java`` 与底座基础迁移文件（绝不与模板 ``V1__*.sql`` 冲突）。"""
    pkg_rel = blueprint.base_package.replace(".", "/")
    common_rel = f"backend/src/main/java/{pkg_rel}/common/ApiResponse.java"
    if common_rel not in categories:
        _validate_rel(common_rel)
        target = dest_root / common_rel
        if not _is_within(dest_root, target):
            raise AssemblyError(f"共享 common 文件路径越界：{common_rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_render_common_api_response_java(blueprint.base_package), encoding="utf-8")
        categories[common_rel] = "base_common"

    migration_dir = dest_root / "backend" / "src" / "main" / "resources" / "db" / "migration"
    has_base_v1 = migration_dir.is_dir() and any(
        p.is_file() and (p.name.startswith("V1__") or p.name.startswith("V001__"))
        for p in migration_dir.iterdir()
    )
    if not has_base_v1:
        sql_rel = "backend/src/main/resources/db/migration/V001__base.sql"
        target_sql = dest_root / sql_rel
        target_sql.parent.mkdir(parents=True, exist_ok=True)
        target_sql.write_text(_render_fallback_base_sql(), encoding="utf-8")
        categories[sql_rel] = "base_common"


def _set_files_readonly(root: Path) -> None:
    """将底座内全部文件设为只读（``0o444``），保留目录遍历/编译输出能力。"""
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        here = Path(dirpath)
        for fname in filenames:
            fpath = here / fname
            if not fpath.is_symlink():
                fpath.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def _build_protected_prefixes(blueprint: ArchitectureBlueprint) -> list[str]:
    pkg_rel = blueprint.base_package.replace(".", "/")
    prefixes: set[str] = {p for p in PROTECTED_PREFIXES if p.startswith("backend/")}
    prefixes.update(
        {
            "backend/pom.xml",
            "backend/Dockerfile",
            "backend/src/main/resources/application",
            "backend/src/main/resources/db/migration/V1__",
            "backend/src/main/resources/db/migration/V001__",
            f"backend/src/main/java/{pkg_rel}/common/",
            f"backend/src/main/java/{pkg_rel}/security/",
            f"backend/src/main/java/{pkg_rel}/config/",
            f"backend/src/main/java/{pkg_rel}/exception/",
        }
    )
    for mod in blueprint.modules:
        prefixes.add(f"backend/src/main/java/{pkg_rel}/modules/{mod.module_id}/api/")
    return sorted(prefixes)


def build_backend_base(
    run_dir: str | Path,
    blueprint: ArchitectureBlueprint,
    lock: AssetLock,
    materializer: AssetMaterializer,
    *,
    limits: AssemblyLimits | None = None,
) -> BaseBuildResult:
    """FP03 主入口：确定性装配只读后端底座与全量跨模块 ``api/*Facade.java`` 骨架（0 模型调用）。"""
    if blueprint.phase != "backend":
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"build_backend_base 仅接受 phase='backend' 蓝图，收到：{blueprint.phase!r}",
            status_code=422,
        )

    verify_lock_hash(lock)
    templates = [asset for asset in lock.assets if asset.kind == "template"]
    if len(templates) != 1:
        raise AssemblyError(f"AssetLock 模板数量必须为 1，实际为 {len(templates)}")
    tpl = templates[0]
    template_ref = f"{tpl.id}@{tpl.version}"

    template_dir = materializer.materialize_release("template", tpl.id, tpl.version)
    effective_limits = limits or AssemblyLimits()

    run_root = Path(run_dir).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    final_dir = run_root / BACKEND_BASE_DIRNAME
    tmp_dir = run_root / f".{BACKEND_BASE_DIRNAME}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if tmp_dir.exists():
        remove_readonly_tree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        categories = _copy_backend_template(template_dir, tmp_dir, effective_limits)
        _ensure_common_and_base_migration(tmp_dir, blueprint, categories)

        # 生成跨模块只读 DTO 与 api/*Facade.java 骨架（A06 确定性生成器）
        stub_paths = generate_java_facade_stubs(blueprint, tmp_dir)
        pkg_rel = blueprint.base_package.replace(".", "/")
        dto_path_set = {
            f"backend/src/main/java/{pkg_rel}/modules/{dto.owner_module_id}/api/{dto.name}.java"
            for dto in blueprint.shared_dtos
        }
        facade_stub_paths: list[str] = []
        shared_dto_paths: list[str] = []

        for rel in stub_paths:
            if rel in categories:
                raise AssemblyError(f"生成的 Facade/DTO 骨架与底座模板文件冲突：{rel}")
            if rel in dto_path_set:
                categories[rel] = "shared_dto"
                shared_dto_paths.append(rel)
            else:
                categories[rel] = "facade_stub"
                facade_stub_paths.append(rel)

        entries: list[BaseManifestEntry] = []
        for rel in sorted(categories):
            fpath = tmp_dir / rel
            data = fpath.read_bytes()
            entries.append(
                BaseManifestEntry(
                    path=rel,
                    sha256=_sha256_bytes(data),
                    size=len(data),
                    category=categories[rel],
                    protected=True,
                )
            )

        manifest_hash = _compute_manifest_hash(entries)
        protected_prefixes = _build_protected_prefixes(blueprint)
        bp_hash = blueprint.blueprint_hash()

        manifest = BaseManifest(
            blueprint_hash=bp_hash,
            blueprint_version=blueprint.blueprint_version,
            lock_hash=lock.lock_hash,
            template_ref=template_ref,
            base_package=blueprint.base_package,
            protected_prefixes=protected_prefixes,
            facade_stub_paths=sorted(facade_stub_paths),
            shared_dto_paths=sorted(shared_dto_paths),
            manifest_hash=manifest_hash,
            files=entries,
        )
        manifest_file = tmp_dir / READONLY_MANIFEST_NAME
        manifest_file.write_text(
            json.dumps(manifest.model_dump(mode="json"), indent=2, ensure_ascii=False, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

        _set_files_readonly(tmp_dir)

        if final_dir.exists():
            remove_readonly_tree(final_dir)
        os.replace(tmp_dir, final_dir)

        return BaseBuildResult(
            base_dir=str(final_dir),
            backend_project_dir=str(final_dir / "backend"),
            manifest_path=str(final_dir / READONLY_MANIFEST_NAME),
            blueprint_hash=bp_hash,
            lock_hash=lock.lock_hash,
            template_ref=template_ref,
            manifest_hash=manifest_hash,
            facade_stub_paths=sorted(facade_stub_paths),
            shared_dto_paths=sorted(shared_dto_paths),
            protected_prefixes=protected_prefixes,
            files=entries,
        )
    except Exception:
        if tmp_dir.exists():
            remove_readonly_tree(tmp_dir)
        raise


def load_base_manifest(manifest_or_path: BaseManifest | str | Path) -> BaseManifest:
    if isinstance(manifest_or_path, BaseManifest):
        return manifest_or_path
    path = Path(manifest_or_path)
    if path.is_dir():
        path = path / READONLY_MANIFEST_NAME
    if not path.is_file():
        raise AppException(
            ErrorCode.NOT_FOUND,
            f"只读底座清单不存在：{path}",
            status_code=404,
        )
    return BaseManifest.model_validate_json(path.read_text(encoding="utf-8"))


def verify_base_integrity(
    target_dir: str | Path,
    manifest_or_path: BaseManifest | str | Path,
) -> list[str]:
    """核验目标目录中的全部受保护底座文件与 ``api/*Facade.java`` 骨架是否零缺失、零篡改。

    返回违规列表（空列表 = 完全一致）。供 FP06 总装门禁直接拦截任何篡改底座或跨模块签名的部门产物。
    """
    manifest = load_base_manifest(manifest_or_path)
    root = Path(target_dir).resolve()
    violations: list[str] = []

    expected_hash = _compute_manifest_hash(manifest.files)
    if expected_hash != manifest.manifest_hash:
        violations.append(
            f"只读清单自身哈希不一致（expected={manifest.manifest_hash}, actual={expected_hash}）"
        )

    recorded_paths = {entry.path for entry in manifest.files}
    for entry in manifest.files:
        candidate = root / entry.path
        if candidate.is_symlink():
            violations.append(f"底座/Facade 受保护路径出现非法符号链接：{entry.path}")
            continue
        if not candidate.is_file():
            violations.append(f"受保护底座或 Facade 文件缺失：{entry.path}")
            continue
        if not _is_within(root, candidate):
            violations.append(f"受保护文件路径越界：{entry.path}")
            continue
        actual_sha = _sha256_bytes(candidate.read_bytes())
        if actual_sha != entry.sha256:
            violations.append(
                f"检测到受保护底座或 Facade 签名被篡改：{entry.path} "
                f"(category={entry.category}, expected={entry.sha256[:12]}, actual={actual_sha[:12]})"
            )

    # 额外防护：拦截单部门向 modules/*/api/ 目录私自新增未经冻结的 Java 接口文件
    pkg_rel = manifest.base_package.replace(".", "/")
    modules_dir = root / "backend" / "src" / "main" / "java" / pkg_rel / "modules"
    if modules_dir.is_dir():
        for path in sorted(modules_dir.rglob("*.java")):
            if any(part in _BUILD_OUTPUT_DIRS for part in path.parts):
                continue
            rel = path.relative_to(root).as_posix()
            parts = PurePosixPath(rel).parts
            if "modules" in parts and "api" in parts:
                mod_idx = parts.index("modules")
                if (
                    mod_idx + 2 < len(parts)
                    and parts[mod_idx + 2] == "api"
                    and rel not in recorded_paths
                ):
                    violations.append(
                        f"检测到未经冻结的跨模块 api/ 文件写入：{rel}"
                    )

    return violations


def assert_base_integrity(
    target_dir: str | Path,
    manifest_or_path: BaseManifest | str | Path,
) -> None:
    """断言底座与跨模块 ``api/*Facade.java`` 零篡改；若发现任何篡改直接抛出 409 ``CONTRACT_CONFLICT``。"""
    violations = verify_base_integrity(target_dir, manifest_or_path)
    if violations:
        raise AppException(
            ErrorCode.CONTRACT_CONFLICT,
            "只读底座或跨模块 Facade 签名完整性校验失败：" + "；".join(violations),
            status_code=409,
        )


def check_department_paths_against_base(
    changed_paths: Sequence[str],
    manifest_or_path: BaseManifest | str | Path,
    *,
    allowed_test_paths: Sequence[str] = (),
) -> list[str]:
    """在合并前快速拦截触及受保护底座或 ``api/*Facade.java`` 路径的部门变更清单。"""
    from agentforge_platform.agent_files import _within_allowed

    manifest = load_base_manifest(manifest_or_path)
    protected_files = {entry.path for entry in manifest.files}
    test_whitelist = [p for p in allowed_test_paths if p]
    violations: list[str] = []
    for raw in changed_paths:
        normalized = raw.lstrip("/")
        if normalized in protected_files:
            violations.append(f"禁止修改只读底座或已冻结 Facade 文件：{normalized}")
            continue
        matched_prefix = False
        for prefix in manifest.protected_prefixes:
            if not normalized.startswith(prefix):
                continue
            if prefix == "backend/src/test/" and test_whitelist and _within_allowed(normalized, test_whitelist):
                continue
            matched_prefix = True
            break
        if matched_prefix:
            violations.append(f"禁止写入受保护底座/跨模块接口前缀路径：{normalized}")
            continue
        parts = PurePosixPath(normalized).parts
        if "modules" in parts and "api" in parts and normalized.endswith(".java"):
            mod_idx = parts.index("modules")
            if mod_idx + 2 < len(parts) and parts[mod_idx + 2] == "api":
                violations.append(f"禁止修改或新增跨模块 api/ 接口文件：{normalized}")
    return violations


__all__ = [
    "BACKEND_BASE_DIRNAME",
    "READONLY_MANIFEST_NAME",
    "BaseBuildResult",
    "BaseManifest",
    "BaseManifestEntry",
    "assert_base_integrity",
    "build_backend_base",
    "check_department_paths_against_base",
    "load_base_manifest",
    "verify_base_integrity",
]
