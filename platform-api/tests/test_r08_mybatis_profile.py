"""R08：Spring Boot + MyBatis（非 Plus）分支。

覆盖：认证清单纳入 demo-ts-mb；MyBatis 规格可确认 → Resolver → 锁定；MB 能力包渲染 XML 显式 SQL
且**不含 Plus 专属 API**；Plus/MB 组合按 Profile 严格隔离；旧 Plus 锁原样重放。
"""

from __future__ import annotations

import json
from pathlib import Path

from agentforge_platform.asset_registry import default_registry
from agentforge_platform.capability_packs import render_pack_files
from agentforge_platform.contract import compile_from_spec
from agentforge_platform.resolver import AssetLock, resolve_assets, verify_lock_hash
from agentforge_platform.spec import (
    SoftwareSpecification,
    TechnicalProfile,
    certified_profiles,
    confirmation_issues,
    is_supported_profile,
    scope_issues,
)

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "schemas" / "task-management.spec.json"
TS_LOCK = REPO / "schemas" / "asset-lock.sample.json"
MB_LOCK = REPO / "schemas" / "asset-lock.mb.sample.json"
MB_PACK = REPO / "capability-packs" / "task-crud-backend-mb" / "0.1.0"

_PLUS_LEAKS = ("mybatisplus", "com.baomidou", "BaseMapper", "QueryWrapper", "@TableName", "@TableId")


def _mb_spec() -> SoftwareSpecification:
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    payload["technical_profile"] = {
        "profile_id": "demo-ts-mb",
        "profile_version": "0.1",
        "template_version": "demo-0.2-mb",
    }
    return SoftwareSpecification.model_validate(payload)


# --- 认证清单 / is_supported_profile -----------------------------------------

def test_certified_profiles_include_mybatis() -> None:
    assert ("demo-ts-mb", "0.1", "demo-0.2-mb") in certified_profiles()


def test_mybatis_profile_supported_and_mismatch_rejected() -> None:
    assert is_supported_profile(TechnicalProfile(
        profile_id="demo-ts-mb", profile_version="0.1", template_version="demo-0.2-mb"))
    # 组合但模板不匹配（未认证）
    assert not is_supported_profile(TechnicalProfile(
        profile_id="demo-ts-mb", profile_version="0.1", template_version="demo-0.2"))


# --- MyBatis 规格：确认 → Resolver → 锁定 -------------------------------------

def test_mybatis_spec_confirmable_and_resolves() -> None:
    spec = _mb_spec()

    assert scope_issues(spec) == []
    assert confirmation_issues(spec) == []

    lock = resolve_assets(default_registry(), "demo-ts-mb")
    assert {a.id for a in lock.assets} == {
        "base-template", "task-crud-backend-mb", "task-crud-frontend"
    }
    assert next(a for a in lock.assets if a.id == "base-template").version == "demo-0.2-mb"
    assert next(a for a in lock.assets if a.id == "task-crud-backend-mb").version == "0.2.0"


def test_mybatis_lock_matches_committed_sample() -> None:
    committed = AssetLock.model_validate_json(MB_LOCK.read_text(encoding="utf-8"))

    lock = resolve_assets(default_registry(), "demo-ts-mb")
    assert lock.lock_hash == committed.lock_hash
    verify_lock_hash(committed)


# --- MB 能力包渲染：XML 显式 SQL，无 Plus ------------------------------------

def test_mybatis_pack_renders_explicit_xml_without_plus() -> None:
    contract = compile_from_spec(_mb_spec())
    rendered = dict(render_pack_files(MB_PACK, contract))

    xml = next(content for target, content in rendered.items() if target.endswith("TaskMapper.xml"))
    assert "<select id=\"findByIdAndOwner\"" in xml
    assert "#{ownerId}" in xml and "LIMIT #{limit} OFFSET #{offset}" in xml

    entity = next(content for target, content in rendered.items() if target.endswith("entity/Task.java"))
    assert "@TableName" not in entity

    mapper = next(content for target, content in rendered.items() if target.endswith("mapper/TaskMapper.java"))
    assert "@Param" in mapper and "findByIdAndOwner" in mapper

    combined = "\n".join(rendered.values()).lower()
    assert all(leak.lower() not in combined for leak in _PLUS_LEAKS)


# --- Profile 严格隔离 ---------------------------------------------------------

def test_plus_and_mybatis_backends_are_not_mixed() -> None:
    plus = resolve_assets(default_registry(), "demo-ts-mp")
    plus_ids = {a.id for a in plus.assets}
    assert "task-crud-backend-mb" not in plus_ids
    assert next(a for a in plus.assets if a.id == "task-crud-backend").version == "0.2.0"

    mb_ids = {a.id for a in resolve_assets(default_registry(), "demo-ts-mb").assets}
    assert "task-crud-backend" not in mb_ids  # 不复用 Plus 后端


# --- 旧 Plus 锁原样重放 -------------------------------------------------------

def test_legacy_plus_lock_replay_unchanged() -> None:
    committed = AssetLock.model_validate_json(TS_LOCK.read_text(encoding="utf-8"))
    lock = resolve_assets(default_registry(), "demo-ts-mp")

    assert lock.lock_hash == committed.lock_hash
    verify_lock_hash(lock)
