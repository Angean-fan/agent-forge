"""F03：Module Interface Registry（项目内公共能力发布/发现/契约版本）。

覆盖（**确定性、零模型调用**）：

- 证据决定状态：无证据 → `DRAFT`；`source_committed+contract_consistent+runner_verified+ref` → `VERIFIED`；
  自报 `VERIFIED` 无证据 → 降级为 `DRAFT`；
- `DRAFT` 不可被消费者当作可调用能力（`lookup` 默认只返回 `VERIFIED`）；
- Order 只读获取 User 的签名/错误/权限，**不读其私有实现**；
- `JAVA_METHOD` 与 `HTTP` 分离：URL 不得混充 Java 类调用；
- 模糊签名、能力冲突、破坏性升级、越权修改一律阻塞；
- 与 R06A—R06D Method Unit/FTS5/asset-lock 无耦合。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentforge_platform.errors import AppException
from agentforge_platform.interface_registry import (
    KIND_HTTP,
    KIND_JAVA_METHOD,
    STATUS_DEPRECATED,
    STATUS_DRAFT,
    STATUS_VERIFIED,
    CapabilityEvidence,
    InterfaceRegistry,
    PublicCapability,
    breaking_change,
    deprecate,
    load_registry,
    lookup,
    register_capability,
    registry_issues,
    save_registry,
)

REPO = Path(__file__).resolve().parents[2]
_VERIFIED = CapabilityEvidence(source_committed=True, contract_consistent=True,
                               runner_verified=True, verification_ref="runner:verify#u1")


def _registry() -> InterfaceRegistry:
    return InterfaceRegistry(project_id="p", based_on_spec_hash="a" * 64)


def _user_find_by_id(*, version: int = 1, signature: str = "UserDTO findById(Long userId)") -> PublicCapability:
    return PublicCapability(
        capability_id="user.find_by_id", owner_module_id="user", interface_kind=KIND_JAVA_METHOD,
        symbol="UserQueryService.findById", signature=signature,
        args_schema_ref="schema:userId@1", returns_schema_ref="schema:UserDTO@1",
        errors=["USER_NOT_FOUND"], auth_constraints=["authenticated"], version=version,
    )


# --- 状态由证据决定 ------------------------------------------------------------

def test_evidence_decides_status() -> None:
    registry = register_capability(_registry(), _user_find_by_id(), evidence=CapabilityEvidence())
    assert registry.capabilities[0].status == STATUS_DRAFT

    registry = register_capability(registry, _user_find_by_id(version=2), evidence=_VERIFIED)
    verified = [c for c in registry.capabilities if c.status == STATUS_VERIFIED]
    assert len(verified) == 1 and verified[0].version == 2
    assert verified[0].verification_ref == "runner:verify#u1"


def test_self_declared_verified_without_evidence_is_demoted() -> None:
    declared = _user_find_by_id().model_copy(update={"status": STATUS_VERIFIED})
    registry = register_capability(_registry(), declared, evidence=CapabilityEvidence())

    assert registry.capabilities[0].status == STATUS_DRAFT  # 模型自报无效
    assert registry.capabilities[0].verification_ref is None


def test_draft_not_consumable_but_visible_for_planning() -> None:
    registry = register_capability(_registry(), _user_find_by_id(), evidence=CapabilityEvidence())

    assert lookup(registry, "user.find_by_id") is None  # DRAFT 不可当可调用能力
    draft = lookup(registry, "user.find_by_id", include_draft=True)
    assert draft is not None and draft.status == STATUS_DRAFT


# --- 跨模块消费（User 提供，Order 调用） --------------------------------------

def test_order_consumes_user_capability_by_id() -> None:
    registry = register_capability(_registry(), _user_find_by_id(), evidence=_VERIFIED)

    found = lookup(registry, "user.find_by_id")
    assert found.status == STATUS_VERIFIED
    # Order 只需签名/错误/权限即可编译调用，无须读取 User 私有实现
    assert found.signature == "UserDTO findById(Long userId)"
    assert found.errors == ["USER_NOT_FOUND"]
    assert found.auth_constraints == ["authenticated"]
    assert set(found.model_dump()) == {
        "capability_id", "owner_module_id", "interface_kind", "symbol", "signature",
        "args_schema_ref", "returns_schema_ref", "errors", "auth_constraints",
        "version", "status", "verification_ref", "http_method", "http_path",
    }


# --- 类型分离与合法性 ----------------------------------------------------------

def test_java_method_and_http_are_not_interchangeable() -> None:
    with pytest.raises(AppException):
        register_capability(_registry(), PublicCapability(
            capability_id="user.lookup", owner_module_id="user", interface_kind=KIND_JAVA_METHOD,
            symbol="https://api.example.invalid/user", signature="UserDTO lookup(Long id)"),
        evidence=_VERIFIED)

    with pytest.raises(AppException):
        register_capability(_registry(), PublicCapability(
            capability_id="user.lookup", owner_module_id="user", interface_kind=KIND_HTTP,
            symbol="UserController.lookup", signature="GET /user/{id}", http_method="GET",
            http_path="/user/{id}"),
        evidence=_VERIFIED)


def test_http_capability_is_accepted() -> None:
    registry = register_capability(_registry(), PublicCapability(
        capability_id="user.lookup", owner_module_id="user", interface_kind=KIND_HTTP,
        signature="GET /users/{id} -> UserDTO", http_method="GET", http_path="/users/{id}",
        returns_schema_ref="schema:UserDTO@1"),
        evidence=_VERIFIED)
    assert lookup(registry, "user.lookup").interface_kind == KIND_HTTP


def test_ambiguous_signature_rejected() -> None:
    with pytest.raises(AppException):
        register_capability(_registry(), _user_find_by_id(signature="findById(?)"), evidence=_VERIFIED)


def test_unsafe_capability_id_rejected() -> None:
    with pytest.raises(ValidationError):
        PublicCapability(capability_id="User.FindById", owner_module_id="user",
                         interface_kind=KIND_JAVA_METHOD, symbol="S.m", signature="void m()")


# --- 冲突与破坏性升级 ----------------------------------------------------------

def test_duplicate_id_version_conflict_detected() -> None:
    registry = register_capability(_registry(), _user_find_by_id(version=1), evidence=_VERIFIED)
    # 强制注入同 id+version（模拟脏数据）以验证全局校验
    dup = registry.model_copy(update={"capabilities": registry.capabilities + [registry.capabilities[0]]})
    assert any("ID+版本冲突" in i for i in registry_issues(dup))


def test_breaking_upgrade_is_blocked_then_allowed_with_migration() -> None:
    registry = register_capability(_registry(), _user_find_by_id(), evidence=_VERIFIED)
    breaking = _user_find_by_id(version=2, signature="UserDTO findById(String userId)")
    assert breaking_change(registry.capabilities[0], breaking)  # 签名变更

    with pytest.raises(AppException) as error:
        register_capability(registry, breaking, evidence=_VERIFIED)
    assert error.value.code == "INTERFACE_CONFLICT"

    upgraded = register_capability(registry, breaking, evidence=_VERIFIED, allow_breaking=True)
    assert lookup(upgraded, "user.find_by_id").version == 2  # 新版本可用
    assert any(c.status == STATUS_DEPRECATED for c in upgraded.capabilities)  # 旧版弃用 → 触发迁移


def test_non_owner_cannot_overwrite_or_deprecate() -> None:
    registry = register_capability(_registry(), _user_find_by_id(), evidence=_VERIFIED)
    impersonated = _user_find_by_id().model_copy(update={"owner_module_id": "order"})

    with pytest.raises(AppException):
        register_capability(registry, impersonated, evidence=_VERIFIED, allow_breaking=True)
    with pytest.raises(AppException):
        deprecate(registry, "user.find_by_id", by_module_id="order")
    assert deprecate(registry, "user.find_by_id", by_module_id="user").capabilities[0].status == STATUS_DEPRECATED


# --- 持久化与隔离 --------------------------------------------------------------

def test_persist_replay_and_independence(tmp_path: Path) -> None:
    registry = register_capability(_registry(), _user_find_by_id(), evidence=_VERIFIED)
    save_registry(registry, tmp_path)
    assert load_registry(tmp_path) == registry
    assert json.loads(registry.model_dump_json())["schema_version"] == "0.1"
    assert load_registry(tmp_path / "missing") is None


def test_no_coupling_to_method_unit_registry() -> None:
    """旧 Method Unit/FTS5/asset-lock 不受影响：本模块不引用它们。"""
    source = (REPO / "platform-api" / "src" / "agentforge_platform" / "interface_registry.py").read_text(
        encoding="utf-8")
    for module in ("method_index", "method_lock", "asset_finder", "resolver"):
        assert f"agentforge_platform.{module}" not in source
