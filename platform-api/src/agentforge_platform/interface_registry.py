"""F03：Module Interface Registry —— 项目内公共能力发布、发现与契约版本。

为**当前生成项目运行/版本**的**已实现接口**建立独立发现层（区别于 R06A—R06D 的**跨项目**
Method Unit Registry，不修改其发布语义）：

- 各模块完成后**发布公共契约**（方法/参数/返回/异常/权限/副作用/版本/验证证据）；
- 其他模块按 ``capability_id`` **只读查询**并复用，**不读取提供者的私有源码/记忆**；
- **草稿** ``DRAFT`` 可供规划，但**不得当作已验证可调用能力**；只有源码已提交、签名/DTO/错误/权限
  一致性通过、并有 Runner 验证证据后才 ``VERIFIED``；
- 导出 Java 服务接口时明确 ``JAVA_METHOD``；HTTP 接口才用 ``HTTP``（**不得用 URL 混充 Java 类调用**）；
- 模糊签名、能力 ID 冲突、破坏性变更、过期验证记录一律阻塞；仅所有者可改（弃用）自身接口。

纯确定性、零模型调用；本模块**不**改动 Method Unit/FTS5/asset-lock/既有 Registry。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from agentforge_platform.errors import AppException, ErrorCode

INTERFACE_REGISTRY_VERSION = "0.1"
INTERFACES_FILE = "interfaces.json"

STATUS_DRAFT = "DRAFT"
# A06：开工前契约冻结态（项目总管在并发开工前锁定，用于生成只读 Facade Stub 供跨部门编译/Mock）
STATUS_APPROVED_FOR_BUILD = "APPROVED_FOR_BUILD"
STATUS_VERIFIED = "VERIFIED"
STATUS_DEPRECATED = "DEPRECATED"
_ALLOWED_STATUSES = frozenset(
    {STATUS_DRAFT, STATUS_APPROVED_FOR_BUILD, STATUS_VERIFIED, STATUS_DEPRECATED}
)

KIND_JAVA_METHOD = "JAVA_METHOD"
KIND_HTTP = "HTTP"

HTTP_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH"})
_CAPABILITY_ID = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\Z")
_SAFE = re.compile(r"[A-Za-z0-9._\-]{1,80}\Z")
_URL = re.compile(r"https?://", re.IGNORECASE)
# 模糊/占位签名特征 → 阻塞（不含 `<`/`>`：签名常含 `->` 箭头）。
_AMBIGUOUS = ("*", "?", "tbd", "todo", "undefined", "unknown")

_STRICT = ConfigDict(extra="forbid")


class InterfaceKind(StrEnum):
    JAVA_METHOD = KIND_JAVA_METHOD
    HTTP = KIND_HTTP


class PublicCapability(BaseModel):
    """一条对外可见的公共能力契约（``status`` 由注册过程依据证据决定，非自报）。"""

    model_config = _STRICT

    capability_id: str
    owner_module_id: str
    interface_kind: str
    symbol: str | None = None
    signature: str
    args_schema_ref: str = ""
    returns_schema_ref: str = ""
    errors: list[str] = []
    auth_constraints: list[str] = []
    version: int = 1
    status: str = STATUS_DRAFT
    verification_ref: str | None = None
    http_method: str | None = None
    http_path: str | None = None

    @field_validator("capability_id")
    @classmethod
    def _id_format(cls, value: str) -> str:
        if _CAPABILITY_ID.fullmatch(value) is None:
            raise ValueError(f"非法 capability_id：{value!r}（形如 module.name）")
        return value


@dataclass(frozen=True)
class CapabilityEvidence:
    """由**程序**提供的验证证据（模型自报无效）。"""

    source_committed: bool = False
    contract_consistent: bool = False
    runner_verified: bool = False
    verification_ref: str | None = None

    @property
    def complete(self) -> bool:
        return (
            self.source_committed and self.contract_consistent
            and self.runner_verified and bool(self.verification_ref)
        )


class InterfaceRegistry(BaseModel):
    model_config = _STRICT

    schema_version: str = INTERFACE_REGISTRY_VERSION
    project_id: str
    based_on_spec_hash: str
    capabilities: list[PublicCapability] = []


def _kind_issues(capability: PublicCapability) -> list[str]:
    issues: list[str] = []
    if capability.interface_kind not in {KIND_JAVA_METHOD, KIND_HTTP}:
        issues.append(f"{capability.capability_id} 未知接口类型：{capability.interface_kind}")
        return issues
    for token in _AMBIGUOUS:
        if token in capability.signature.lower():
            issues.append(f"{capability.capability_id} 模糊签名：{capability.signature!r}")
            break
    if capability.interface_kind == KIND_JAVA_METHOD:
        if not capability.symbol or "." not in capability.symbol:
            issues.append(f"{capability.capability_id} JAVA_METHOD 缺少类.方法 symbol")
        if capability.symbol and _URL.search(capability.symbol):
            issues.append(f"{capability.capability_id} 不得用 URL 混充 Java 类调用：{capability.symbol}")
        if capability.http_method or capability.http_path:
            issues.append(f"{capability.capability_id} JAVA_METHOD 不应带 HTTP 字段")
    else:  # HTTP
        if capability.symbol:
            issues.append(f"{capability.capability_id} HTTP 能力不应声明 Java symbol")
        if (capability.http_method or "").upper() not in HTTP_METHODS:
            issues.append(f"{capability.capability_id} HTTP 方法非法：{capability.http_method}")
        if not capability.http_path or not capability.http_path.startswith("/"):
            issues.append(f"{capability.capability_id} HTTP 路径非法：{capability.http_path}")
    if not _SAFE.fullmatch(capability.owner_module_id):
        issues.append(f"{capability.capability_id} 非法 owner：{capability.owner_module_id}")
    return issues


def breaking_change(old: PublicCapability, new: PublicCapability) -> list[str]:
    """破坏性变更：签名/参数/返回/错误码/类型/所有者变化。"""
    reasons: list[str] = []
    if new.version <= old.version:
        reasons.append(f"版本未递增：{old.version} → {new.version}")
    for field_name in ("interface_kind", "symbol", "signature",
                       "args_schema_ref", "returns_schema_ref", "owner_module_id"):
        if getattr(old, field_name) != getattr(new, field_name):
            reasons.append(f"{field_name} 变更：{getattr(old, field_name)!r} → {getattr(new, field_name)!r}")
    if set(old.errors) - set(new.errors):
        reasons.append("移除既有错误码：" + "、".join(sorted(set(old.errors) - set(new.errors))))
    if set(old.auth_constraints) - set(new.auth_constraints):
        reasons.append("移除既有鉴权约束：" + "、".join(sorted(set(old.auth_constraints) - set(new.auth_constraints))))
    return reasons


def register_capability(
    registry: InterfaceRegistry,
    capability: PublicCapability,
    *,
    evidence: CapabilityEvidence,
    allow_breaking: bool = False,
) -> InterfaceRegistry:
    """注册/升级公共能力：状态由证据决定；冲突、模糊签名、破坏性变更一律阻塞。"""
    issues = _kind_issues(capability)
    if issues:
        raise AppException(ErrorCode.VALIDATION_ERROR, "；".join(issues), status_code=422)

    existing = next(
        (c for c in registry.capabilities if c.capability_id == capability.capability_id), None
    )
    capabilities = list(registry.capabilities)
    if existing is not None:
        if existing.owner_module_id != capability.owner_module_id:
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"能力 {capability.capability_id} 属 {existing.owner_module_id}，他人不得覆盖。",
                status_code=403,
            )
        reasons = breaking_change(existing, capability)
        if reasons and not allow_breaking:
            raise AppException(
                ErrorCode.INTERFACE_CONFLICT,  # 语义：需要依赖迁移/显式允许
                f"{capability.capability_id} 破坏性升级被阻塞：" + "；".join(reasons),
                status_code=409,
            )
        if not reasons and existing.status == STATUS_VERIFIED and not evidence.complete:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"{capability.capability_id} 升级未提供完整验证证据，不得标记 VERIFIED。",
                status_code=422,
            )
        capabilities = [c for c in registry.capabilities if c.capability_id != capability.capability_id]
        if reasons:  # 显式允许的破坏性升级：旧版标记弃用，提示依赖迁移
            capabilities.append(existing.model_copy(update={"status": STATUS_DEPRECATED}))

    status = STATUS_VERIFIED if evidence.complete else STATUS_DRAFT
    capabilities.append(capability.model_copy(update={
        "status": status, "verification_ref": evidence.verification_ref if evidence.complete else None,
    }))
    capabilities.sort(key=lambda c: (c.capability_id, c.version, c.status))
    return registry.model_copy(update={"capabilities": capabilities})


def approve_for_build(
    registry: InterfaceRegistry,
    capability: PublicCapability,
    *,
    allow_breaking: bool = False,
) -> InterfaceRegistry:
    """A06：开工前冻结公共能力契约（状态置为 ``APPROVED_FOR_BUILD``，用于生成只读 Facade Stub）。"""
    issues = _kind_issues(capability)
    if issues:
        raise AppException(ErrorCode.VALIDATION_ERROR, "；".join(issues), status_code=422)

    existing = next(
        (c for c in registry.capabilities if c.capability_id == capability.capability_id), None
    )
    capabilities = list(registry.capabilities)
    if existing is not None:
        if existing.owner_module_id != capability.owner_module_id:
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"能力 {capability.capability_id} 属 {existing.owner_module_id}，他人不得覆盖。",
                status_code=403,
            )
        reasons = breaking_change(existing, capability)
        if reasons and not allow_breaking:
            raise AppException(
                ErrorCode.INTERFACE_CONFLICT,
                f"{capability.capability_id} 破坏性升级被阻塞：" + "；".join(reasons),
                status_code=409,
            )
        capabilities = [c for c in registry.capabilities if c.capability_id != capability.capability_id]
        if reasons:
            capabilities.append(existing.model_copy(update={"status": STATUS_DEPRECATED}))

    capabilities.append(
        capability.model_copy(
            update={"status": STATUS_APPROVED_FOR_BUILD, "verification_ref": None}
        )
    )
    capabilities.sort(key=lambda c: (c.capability_id, c.version, c.status))
    return registry.model_copy(update={"capabilities": capabilities})


def registry_issues(registry: InterfaceRegistry) -> list[str]:
    """全局校验：同 id+version 冲突、同 id 多个 VERIFIED/APPROVED_FOR_BUILD、非法字段。"""
    issues: list[str] = []
    seen: set[tuple[str, int]] = set()
    verified_count: dict[str, int] = {}
    approved_count: dict[str, int] = {}
    for capability in registry.capabilities:
        key = (capability.capability_id, capability.version)
        if key in seen:
            issues.append(f"能力 ID+版本冲突：{capability.capability_id}@{capability.version}")
        seen.add(key)
        if capability.status not in _ALLOWED_STATUSES:
            issues.append(f"{capability.capability_id} 未知契约状态：{capability.status}")
        issues.extend(_kind_issues(capability))
        if capability.status == STATUS_VERIFIED:
            verified_count[capability.capability_id] = (
                verified_count.get(capability.capability_id, 0) + 1
            )
        elif capability.status == STATUS_APPROVED_FOR_BUILD:
            approved_count[capability.capability_id] = (
                approved_count.get(capability.capability_id, 0) + 1
            )
    for capability_id, count in verified_count.items():
        if count > 1:
            issues.append(f"{capability_id} 存在多个 VERIFIED 版本：{count}")
    for capability_id, count in approved_count.items():
        if count > 1:
            issues.append(f"{capability_id} 存在多个 APPROVED_FOR_BUILD 版本：{count}")
    return issues


def lookup(
    registry: InterfaceRegistry,
    capability_id: str,
    *,
    include_draft: bool = False,
    include_approved: bool = False,
) -> PublicCapability | None:
    """按能力 ID 取**最新**契约；默认只返回 ``VERIFIED``（规划可含 ``DRAFT``，并行构建可含 ``APPROVED_FOR_BUILD``）。"""
    allowed = {STATUS_VERIFIED}
    if include_draft:
        allowed.add(STATUS_DRAFT)
    if include_approved:
        allowed.add(STATUS_APPROVED_FOR_BUILD)
    matches = [
        c for c in registry.capabilities
        if c.capability_id == capability_id and c.status in allowed
    ]
    if not matches:
        return None
    return max(matches, key=lambda c: c.version)


def deprecate(
    registry: InterfaceRegistry, capability_id: str, *, by_module_id: str
) -> InterfaceRegistry:
    """仅**所有者**可弃用自身接口（消费者只读，禁止越权修改提供者接口）。"""
    target = next((c for c in registry.capabilities if c.capability_id == capability_id), None)
    if target is None:
        raise AppException(ErrorCode.NOT_FOUND, f"未知能力：{capability_id}", status_code=404)
    if target.owner_module_id != by_module_id:
        raise AppException(
            ErrorCode.FORBIDDEN,
            f"{by_module_id} 非 {capability_id} 的所有者，禁止修改。",
            status_code=403,
        )
    updated = [
        c.model_copy(update={"status": STATUS_DEPRECATED})
        if c.capability_id == capability_id else c
        for c in registry.capabilities
    ]
    return registry.model_copy(update={"capabilities": updated})


def save_registry(registry: InterfaceRegistry, run_dir: str | Path) -> Path:
    path = Path(run_dir) / INTERFACES_FILE
    path.write_text(registry.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def load_registry(run_dir: str | Path) -> InterfaceRegistry | None:
    path = Path(run_dir) / INTERFACES_FILE
    if not path.is_file():
        return None
    return InterfaceRegistry.model_validate_json(path.read_text(encoding="utf-8"))


__all__ = [
    "INTERFACES_FILE",
    "INTERFACE_REGISTRY_VERSION",
    "KIND_HTTP",
    "KIND_JAVA_METHOD",
    "STATUS_APPROVED_FOR_BUILD",
    "STATUS_DEPRECATED",
    "STATUS_DRAFT",
    "STATUS_VERIFIED",
    "CapabilityEvidence",
    "InterfaceKind",
    "InterfaceRegistry",
    "PublicCapability",
    "approve_for_build",
    "breaking_change",
    "deprecate",
    "load_registry",
    "lookup",
    "register_capability",
    "registry_issues",
    "save_registry",
]
