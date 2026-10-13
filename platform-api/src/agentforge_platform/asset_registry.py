"""资产 manifest 与只读本地注册表（M13；R06A 扩展 Method Unit 分类）。

统一模板、能力包与**方法单元（Method Unit）**的 manifest schema，并提供按
``kind + id + version`` 定位、校验内容 checksum、按 Profile 判兼容的只读检索。

- 旧 Template/Capability Pack 的字段与读取语义**保持不变**（:class:`AssetManifest`）；
- R06A 新增 :class:`MethodUnitManifest`：方法/类/片段的稳定 ID + 不可变版本 + 结构化调用契约，
  其 ``symbol/signature/layer/source_provenance`` 为**必需**字段（缺失即拒，不用默认值掩盖签名/来源），
  并按 kind 分支解析。方法单元是否**可安全植入**由 :func:`method_unit_issues` 按 Profile/依赖/冲突/
  扩展点确定性判定；源码仓库与索引归 R06B。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from agentforge_platform.errors import AppException, ErrorCode

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ROOTS = (REPO_ROOT / "templates", REPO_ROOT / "capability-packs")
# E01：Integration Pack（第三方适配清单）独立只读根；**不进入** ``DEFAULT_ROOTS``，
# 以免改变生成链路的资产选择与既有 0.1 锁（显式请求时才由 Resolver 锁定）。
DEFAULT_INTEGRATION_ROOT = REPO_ROOT / "integration-packs"
_EXCLUDED_DIRS = {"node_modules", "target", "dist", "build", "__pycache__", ".git"}
_MANIFEST_NAME = "manifest.json"
_EXCLUDED_FILES = {_MANIFEST_NAME, ".DS_Store", "Thumbs.db"}

# R06A：Method Unit 的层归属与来源/许可证约束（与 Profile/契约同名概念保持一致）。
METHOD_UNIT_LAYERS = frozenset({
    "controller", "service", "repository", "domain", "dto", "config",
    "util", "frontend", "scheduler", "integration",
})
_FORBIDDEN_LICENSES = frozenset({"", "unknown", "unlicensed", "none", "proprietary-unknown"})
_SAFE_SYMBOL = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*\Z")
_SAFE_IMPORT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*\Z")
_SAFE_TECH_TOKEN = re.compile(r"[a-z0-9][a-z0-9+.\-]*\Z")

_STRICT = ConfigDict(extra="forbid")

# E01：Integration Pack 的发布约束。E01 只发布**无真实凭据的模拟服务**（Mock-only），
# live/真实秘密留待 E04/E05；这里把"未审核/需真实密钥"的清单在 parse 阶段即拒。
INTEGRATION_MOCK_ONLY = "mock-only"
_ALLOWED_INTEGRATION_MODES = frozenset({INTEGRATION_MOCK_ONLY})
# E03：外部待办的运行模式（Mock 不需要真实凭据；Live 需用户配置）。
_INTEGRATION_MODES = frozenset({"mock", "live"})
# E02：Adapter 有两类——`mock`（确定性测试行为）与 `stub`（实现真实第三方协议的适配器，
# 经注入传输层确定性地跑，不持有真实凭据）。live/SDK 类仍拒（E04/E05 再引入）。
_MOCK_ADAPTER_KIND = "mock"
_ALLOWED_ADAPTER_KINDS = frozenset({_MOCK_ADAPTER_KIND, "stub"})
_SAFE_PROVIDER = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_SAFE_ENV = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_SAFE_REL_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\Z")


def is_safe_owned_path(path: str) -> bool:
    """``owned_paths`` 只描述**资产可安装的目标范围**：相对、无 ``..`` / 绝对路径 / 盘符 / 反斜杠。

    该字段不赋予 Agent 任意目录权限；危险路径一律拒绝。
    """
    if not path or path.startswith(("/", "~")) or "\\" in path:
        return False
    stripped = path.rstrip("/")
    if not stripped or re.match(r"^[A-Za-z]:", stripped):
        return False
    return all(part not in ("", ".", "..") for part in stripped.split("/"))


def compute_checksum(asset_dir: str | Path) -> str:
    """资产内容 checksum：对（相对路径 + 文件内容 hash）排序后取 SHA-256。

    排除资产自身的 manifest.json 与构建产物目录，保证与 git 状态可复现。
    """
    asset_dir = Path(asset_dir)
    hasher = hashlib.sha256()
    for path in sorted(asset_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(asset_dir)
        if relative.name in _EXCLUDED_FILES:
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


class _AssetBase(BaseModel):
    """模板/能力包/方法单元共有的 manifest 字段（顺序与原 ``AssetManifest`` 一致）。"""

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

    @field_validator("owned_paths")
    @classmethod
    def _safe_owned_paths(cls, value: list[str]) -> list[str]:
        unsafe = [path for path in value if not is_safe_owned_path(path)]
        if unsafe:
            raise ValueError(f"危险的 owned_paths（绝对路径/越界/盘符）：{unsafe}")
        return value


class AssetManifest(_AssetBase):
    """模板 / 能力包 manifest。契约字段全部保留（MVP 语义不变）。"""

    provided_capabilities: list[str] = []
    target_dir: str = ""
    renders: list[RenderEntry] = []
    dependencies: list[dict[str, str]] = []


class MethodUnitManifest(_AssetBase):
    """R06A/R06B：可复用方法/类/片段的结构化调用契约。

    ``symbol/signature/layer/source_provenance/language/framework`` **必需**（缺失即 parse 失败，不用默认值掩盖）；
    ``description/tags`` 供 R06B 索引检索；其余按需声明。是否能**安全植入**由 :func:`method_unit_issues`
    依据 Profile/依赖/冲突/扩展点判定。
    """

    provided_capabilities: list[str] = []
    target_dir: str = ""
    renders: list[RenderEntry] = []
    dependencies: list[dict[str, str]] = []

    symbol: str
    signature: str
    layer: str
    source_provenance: str
    language: str
    framework: str
    description: str = ""
    tags: list[str] = []
    imports: list[str] = []
    preconditions: list[str] = []
    postconditions: list[str] = []
    side_effects: list[str] = []
    security_constraints: list[str] = []
    acceptance_ids: list[str] = []

    @field_validator("language", "framework")
    @classmethod
    def _safe_tech_token(cls, value: str) -> str:
        if _SAFE_TECH_TOKEN.fullmatch(value) is None:
            raise ValueError(f"不安全的 language/framework 标识：{value!r}")
        return value

    @field_validator("tags")
    @classmethod
    def _safe_tags(cls, value: list[str]) -> list[str]:
        unsafe = [tag for tag in value if _SAFE_TECH_TOKEN.fullmatch(tag) is None]
        if unsafe:
            raise ValueError(f"不安全的 tags：{unsafe}")
        return list(dict.fromkeys(value))

    @field_validator("symbol")
    @classmethod
    def _safe_symbol(cls, value: str) -> str:
        if _SAFE_SYMBOL.fullmatch(value) is None:
            raise ValueError(f"不安全的 symbol：{value!r}")
        return value

    @field_validator("signature")
    @classmethod
    def _non_empty_signature(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("signature 不能为空")
        return value

    @field_validator("layer")
    @classmethod
    def _known_layer(cls, value: str) -> str:
        if value not in METHOD_UNIT_LAYERS:
            raise ValueError(f"未知 layer：{value!r}")
        return value

    @field_validator("source_provenance")
    @classmethod
    def _non_empty_provenance(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_provenance（来源证明）不能为空")
        return value

    @field_validator("imports")
    @classmethod
    def _safe_imports(cls, value: list[str]) -> list[str]:
        unsafe = [item for item in value if _SAFE_IMPORT.fullmatch(item) is None]
        if unsafe:
            raise ValueError(f"不安全的 imports：{unsafe}")
        return value


class IntegrationPort(BaseModel):
    """业务侧只依赖的**内部 Port**（E02 将由此生成 Service 的依赖点）。"""

    model_config = _STRICT

    name: str
    methods: list[str]

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        if _SAFE_SYMBOL.fullmatch(value) is None:
            raise ValueError(f"不安全的 Port 名：{value!r}")
        return value

    @field_validator("methods")
    @classmethod
    def _declared_methods(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("Port 至少声明一个方法")
        for method in value:
            if not method.strip() or len(method) > 200 or "\n" in method:
                raise ValueError(f"非法 Port 方法签名：{method!r}")
        return value


class IntegrationAdapter(BaseModel):
    """Port 的实现；E02 允许 ``mock``（确定性）与 ``stub``（真实协议 + 注入传输，无凭据）。

    ``implements`` 声明它实现哪个 Port（E02 契约链路用；E01 清单可留空）。
    """

    model_config = _STRICT

    name: str
    kind: str
    implements: str = ""

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        if _SAFE_SYMBOL.fullmatch(value) is None:
            raise ValueError(f"不安全的 Adapter 名：{value!r}")
        return value

    @field_validator("implements")
    @classmethod
    def _safe_implements(cls, value: str) -> str:
        if value and _SAFE_SYMBOL.fullmatch(value) is None:
            raise ValueError(f"不安全的 Port 绑定：{value!r}")
        return value

    @field_validator("kind")
    @classmethod
    def _allowed_kind(cls, value: str) -> str:
        if value not in _ALLOWED_ADAPTER_KINDS:
            raise ValueError(
                f"仅允许 mock/stub adapter（live/SDK 留待 E04/E05），收到：{value!r}"
            )
        return value


class MockBehavior(BaseModel):
    """Mock 行为契约：E01 要求**确定性**，坏路径场景由 E02 的契约测试消费。"""

    model_config = _STRICT

    deterministic: bool
    scenarios: list[str] = []


class ExternalConfigItem(BaseModel):
    """外部待办（E01 声明；E03 结构化）——用户需**申请/填写**的账号、标识、密钥、回调、白名单等。

    ``secret`` 与 ``required`` **独立**；AppID/公钥不默认当秘密。``required_in_modes`` 指明在哪些
    运行模式下必需（默认 mock+live）；``env_name`` 与 ``config_path`` 绑定代码配置（1:1）。
    E01/E02 的极简写法（仅 ``env_name/required/secret/description``）仍可解析。
    """

    model_config = _STRICT

    env_name: str
    required: bool = False
    secret: bool = False
    description: str = ""
    # E03 结构化字段（均可选；缺失即不生成对应指南段落）。
    requirement_id: str = ""
    name: str = ""
    kind: str = "config"
    required_in_modes: list[str] = ["mock", "live"]
    config_path: str = ""
    target_file: str = ".env.example"
    acquisition_url: str = ""
    acquisition_steps: list[str] = []
    validation_rule: str = ""
    docs_verified_at: str = ""

    @field_validator("env_name")
    @classmethod
    def _safe_env(cls, value: str) -> str:
        if _SAFE_ENV.fullmatch(value) is None:
            raise ValueError(f"不安全的配置变量名：{value!r}")
        return value

    @field_validator("requirement_id")
    @classmethod
    def _safe_requirement_id(cls, value: str) -> str:
        if value and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value) is None:
            raise ValueError(f"不安全的 requirement_id：{value!r}")
        return value

    @field_validator("kind")
    @classmethod
    def _safe_kind(cls, value: str) -> str:
        if re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", value) is None:
            raise ValueError(f"不安全的 kind：{value!r}")
        return value

    @field_validator("required_in_modes")
    @classmethod
    def _known_modes(cls, value: list[str]) -> list[str]:
        unknown = [mode for mode in value if mode not in _INTEGRATION_MODES]
        if unknown:
            raise ValueError(f"未知运行模式：{unknown}")
        return list(dict.fromkeys(value))

    @field_validator("config_path")
    @classmethod
    def _safe_config_path(cls, value: str) -> str:
        if value and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*(\.[A-Za-z_][A-Za-z0-9_-]*)*", value) is None:
            raise ValueError(f"不安全的 config_path：{value!r}")
        return value

    @field_validator("target_file")
    @classmethod
    def _safe_target_file(cls, value: str) -> str:
        # 允许 dotfile（如 `.env.example`）；仍禁止绝对路径与目录穿越。
        if (
            not value
            or value.startswith(("/", "~"))
            or "\\" in value
            or ".." in value.split("/")
            or re.fullmatch(r"[A-Za-z0-9._][A-Za-z0-9._/-]*", value) is None
        ):
            raise ValueError(f"不安全的 target_file：{value!r}")
        return value

    @field_validator("acquisition_url")
    @classmethod
    def _safe_acquisition_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or "@" in parsed.netloc:
            raise ValueError(f"不安全的 acquisition_url（须为 http(s) 且不得内嵌凭据）：{value!r}")
        return value


class IntegrationPackManifest(_AssetBase):
    """E01/E02：第三方适配只读清单——pack ID/版本/兼容 Profile/Port/Adapter/配置/Mock/契约测试/checksum/许可证。

    复用 :class:`_AssetBase` 的寻址与校验字段；额外声明 ``provider/mode/ports/adapters/mock/
    config/source_provenance/target_dir``，以及 E02 的契约链路声明 ``internal_types/contract_tests``。
    **Mock-only**：拒绝 live 模式、非 mock/stub adapter、模拟不可确定、以及"必需的真实秘密"，
    从源头保证无真实凭据；契约链路的完整校验见 :mod:`agentforge_platform.integration_contract`。
    """

    provider: str
    mode: str
    ports: list[IntegrationPort]
    adapters: list[IntegrationAdapter]
    mock: MockBehavior
    config: list[ExternalConfigItem] = []
    source_provenance: str
    target_dir: str = ""
    # E02：Port 契约只允许引用**内部类型**（供应商 DTO 不得进入 Port）；契约测试为已发布资产的一部分。
    internal_types: list[str] = []
    contract_tests: list[str] = []

    @field_validator("provider")
    @classmethod
    def _safe_provider(cls, value: str) -> str:
        if _SAFE_PROVIDER.fullmatch(value) is None:
            raise ValueError(f"不安全的 provider 标识：{value!r}")
        return value

    @field_validator("internal_types")
    @classmethod
    def _safe_internal_types(cls, value: list[str]) -> list[str]:
        for item in value:
            if _SAFE_SYMBOL.fullmatch(item) is None:
                raise ValueError(f"不安全的内部类型名：{item!r}")
        return list(dict.fromkeys(value))

    @field_validator("contract_tests")
    @classmethod
    def _safe_contract_tests(cls, value: list[str]) -> list[str]:
        for item in value:
            stripped = item.rstrip("/")
            if not stripped or not _SAFE_REL_PATH.fullmatch(item) or ".." in item.split("/"):
                raise ValueError(f"不安全的契约测试路径：{item!r}")
        return value

    @field_validator("source_provenance")
    @classmethod
    def _non_empty_provenance(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_provenance（来源证明）不能为空")
        return value

    @field_validator("license")
    @classmethod
    def _clear_license(cls, value: str) -> str:
        if value.strip().lower() in _FORBIDDEN_LICENSES:
            raise ValueError(f"许可证/来源不明，拒绝发布：license={value!r}")
        return value

    @property
    def effective_paths(self) -> list[str]:
        """``target_dir/owned_path``：清单在工程内的实际落点（供路径冲突检测）。"""
        return [
            f"{self.target_dir}/{path}".strip("/") if self.target_dir else path
            for path in self.owned_paths
        ]

    @model_validator(mode="after")
    def _mock_only_constraints(self) -> IntegrationPackManifest:
        if self.mode not in _ALLOWED_INTEGRATION_MODES:
            raise ValueError(
                f"E01 仅允许 mode='{INTEGRATION_MOCK_ONLY}'，收到：{self.mode!r}"
            )
        if not self.ports:
            raise ValueError("Integration Pack 至少声明一个 Port")
        if not self.adapters:
            raise ValueError("Integration Pack 至少声明一个 Adapter")
        if not self.mock.deterministic:
            raise ValueError("Mock-only 清单必须声明确定性的 mock（deterministic=true）")
        for item in self.config:
            # E03：秘密/必需独立；只在"秘密且在 mock 模式下必需"时拒绝（live-only 秘密允许声明）。
            if item.secret and item.required and "mock" in item.required_in_modes:
                raise ValueError(
                    f"Mock-only 清单不得在 mock 模式下要求真实秘密：{item.env_name}"
                )
        return self


# 注册表读接口可返回的 manifest 类型（按 kind 分支解析）。
Manifest = AssetManifest | MethodUnitManifest | IntegrationPackManifest
_MODEL_BY_KIND: dict[str, type[Manifest]] = {
    "method-unit": MethodUnitManifest,
    "integration-pack": IntegrationPackManifest,
}


class AssetRegistry:
    """只读本地资产注册表：扫描根目录与其一级子目录中的 manifest.json。

    manifest 按 ``kind`` 分支解析：``method-unit`` → :class:`MethodUnitManifest`，其余 → :class:`AssetManifest`。
    """

    def __init__(self, roots: list[str | Path] | None = None) -> None:
        self._assets: dict[tuple[str, str, str], tuple[Manifest, Path]] = {}
        for root in roots if roots is not None else DEFAULT_ROOTS:
            self._scan(Path(root))

    def _scan(self, root: Path) -> None:
        """递归扫描 root 下的 manifest.json（支持同一资产多版本目录），剪枝构建产物目录。"""
        if not root.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name not in _EXCLUDED_DIRS)
            if _MANIFEST_NAME not in filenames:
                continue
            directory = Path(dirpath)
            payload = json.loads((directory / _MANIFEST_NAME).read_text(encoding="utf-8"))
            model: type[Manifest] = _MODEL_BY_KIND.get(payload.get("kind"), AssetManifest)
            manifest = model.model_validate(payload)
            self._assets[(manifest.kind, manifest.id, manifest.version)] = (manifest, directory)

    def all(self) -> list[Manifest]:
        return sorted(
            (manifest for manifest, _ in self._assets.values()),
            key=lambda m: (m.kind, m.id, m.version),
        )

    def all_with_paths(self) -> list[tuple[Manifest, Path]]:
        return sorted(
            self._assets.values(),
            key=lambda item: (item[0].kind, item[0].id, item[0].version),
        )

    def get(self, kind: str, asset_id: str, version: str) -> tuple[Manifest, Path]:
        key = (kind, asset_id, version)
        if key not in self._assets:
            raise AppException(
                ErrorCode.ASSET_NOT_FOUND,
                f"资产不存在：{kind}/{asset_id}@{version}",
                status_code=404,
            )
        return self._assets[key]

    def verify_checksum(self, kind: str, asset_id: str, version: str) -> Manifest:
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
    ) -> tuple[Manifest, Path]:
        manifest, directory = self.get(kind, asset_id, version)
        if profile not in manifest.compatible_profiles:
            raise AppException(
                ErrorCode.ASSET_INCOMPATIBLE,
                f"资产 {asset_id}@{version} 与 Profile '{profile}' 不兼容",
                status_code=422,
            )
        return manifest, directory


def _capability_name(requirement: str) -> str:
    return requirement.split(":", 1)[-1]


def known_capabilities(registry: AssetRegistry, profile: str) -> set[str]:
    """某 Profile 下已存在的安全能力集合（模板 ``provided_capabilities`` + 各资产 ``provides``）。"""
    names: set[str] = set()
    for manifest in registry.all():
        if profile in manifest.compatible_profiles:
            names.update(manifest.provided_capabilities)
            names.update(manifest.provides)
    return names


def known_extension_points(registry: AssetRegistry, profile: str) -> set[str]:
    """某 Profile 下兼容资产已声明的扩展点集合（方法单元只允许挂载到这些扩展点）。"""
    points: set[str] = set()
    for manifest in registry.all():
        if profile in manifest.compatible_profiles:
            points.update(manifest.extension_points)
    return points


def method_unit_issues(
    manifest: MethodUnitManifest,
    *,
    known_profiles: set[str],
    available_capabilities: set[str],
    present_capabilities: set[str] | None = None,
    registered_extension_points: set[str] | None = None,
) -> list[str]:
    """确定性判定方法单元是否可发布/安全植入；返回拒绝理由（空 = 通过）。

    覆盖：许可证/来源、Profile 兼容、依赖是否合法、是否与现有能力冲突、扩展点是否已注册。
    签名/符号/层/危险路径等结构性问题在 :class:`MethodUnitManifest` parse 阶段即被拒绝。
    """
    issues: list[str] = []
    if manifest.license.strip().lower() in _FORBIDDEN_LICENSES:
        issues.append(f"许可证/来源不明，拒绝发布：license={manifest.license!r}")
    if not manifest.source_provenance.strip():
        issues.append("缺少来源证明（source_provenance）")
    if known_profiles and not (set(manifest.compatible_profiles) & known_profiles):
        issues.append("未知 Profile：" + "、".join(manifest.compatible_profiles))
    illegal = [
        requirement
        for requirement in manifest.requires
        if _capability_name(requirement) not in available_capabilities
    ]
    if illegal:
        issues.append("非法依赖：" + "、".join(illegal))
    conflicts = sorted(set(manifest.conflicts) & set(present_capabilities or ()))
    if conflicts:
        issues.append("与现有能力冲突：" + "、".join(conflicts))
    if registered_extension_points is not None:
        # 只允许声明已存在于 Profile/契约的扩展点；未提供上下文时（None）不做此判定。
        unknown = [p for p in manifest.extension_points if p not in registered_extension_points]
        if unknown:
            issues.append("声明了未注册的扩展点：" + "、".join(unknown))
    return issues


def require_valid_method_unit(manifest: MethodUnitManifest, **kwargs: object) -> MethodUnitManifest:
    """校验方法单元；不通过则抛 ``ASSET_CONFLICT``（422），理由可追溯。"""
    issues = method_unit_issues(manifest, **kwargs)  # type: ignore[arg-type]
    if issues:
        raise AppException(
            ErrorCode.ASSET_CONFLICT,
            "Method Unit 校验未通过：" + "；".join(issues),
            status_code=422,
        )
    return manifest


def default_registry() -> AssetRegistry:
    return AssetRegistry(list(DEFAULT_ROOTS))


# E01：覆盖模板/能力包/**集成包** 的注册表（生成默认注册表不含集成包，见 ``DEFAULT_INTEGRATION_ROOT``）。
ALL_ROOTS = (*DEFAULT_ROOTS, DEFAULT_INTEGRATION_ROOT)


def integration_registry() -> AssetRegistry:
    """含 Integration Pack 只读根的注册表：复用同一寻址/校验机制，不另起检索系统。"""
    return AssetRegistry(list(ALL_ROOTS))


def _write_checksums() -> None:
    registry = AssetRegistry(list(ALL_ROOTS))
    for manifest, directory in registry.all_with_paths():
        manifest_path = directory / _MANIFEST_NAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["checksum"] = compute_checksum(directory)
        manifest_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"updated checksum: {manifest.id}@{manifest.version}")


def main(argv: list[str]) -> int:
    if "--update" in argv:
        _write_checksums()
        return 0
    registry = integration_registry()
    for manifest in registry.all():
        print(f"{manifest.kind:16} {manifest.id}@{manifest.version} checksum={manifest.checksum[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
