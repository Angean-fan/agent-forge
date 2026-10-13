"""R07：Vue 3 + JavaScript Profile 分支。

覆盖：认证清单确定性判定（TS+JS 支持、未知/模板不匹配拒绝）；JS 规格可确认 → Resolver → 锁定；
JS 前端能力包渲染 .js/.vue 且无 TS 类型；旧 TS Profile 与锁原样重放。
"""

from __future__ import annotations

import json
from pathlib import Path

from agentforge_platform.asset_registry import default_registry
from agentforge_platform.capability_packs import render_pack_files
from agentforge_platform.contract import compile_from_spec
from agentforge_platform.resolver import AssetLock, resolve_assets, verify_lock_hash
from agentforge_platform.spec import (
    TechnicalProfile,
    certified_profiles,
    confirmation_issues,
    is_supported_profile,
    scope_issues,
)

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "schemas" / "task-management.spec.json"
TS_LOCK = REPO / "schemas" / "asset-lock.sample.json"
JS_LOCK = REPO / "schemas" / "asset-lock.js-r27.sample.json"
JS_PACK = REPO / "capability-packs" / "task-crud-frontend-js" / "0.1.0"


def _js_spec_dict() -> dict:
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    payload["technical_profile"] = {
        "profile_id": "demo-js-mp",
        "profile_version": "0.1",
        "template_version": "demo-0.2-js",
    }
    return payload


# --- 认证清单与 is_supported_profile -----------------------------------------

def test_certified_profiles_cover_ts_and_js() -> None:
    combos = certified_profiles()

    assert ("demo-ts-mp", "0.3", "demo-0.2") in combos
    assert ("demo-js-mp", "0.1", "demo-0.2-js") in combos


def test_certified_profiles_are_supported() -> None:
    assert is_supported_profile(TechnicalProfile(
        profile_id="demo-ts-mp", profile_version="0.3", template_version="demo-0.2"))
    assert is_supported_profile(TechnicalProfile(
        profile_id="demo-js-mp", profile_version="0.1", template_version="demo-0.2-js"))


def test_unknown_and_mismatched_profiles_are_rejected() -> None:
    # 未知 profile
    assert not is_supported_profile(TechnicalProfile(
        profile_id="demo-xx", profile_version="1.0", template_version="demo-0.2"))
    # 版本不匹配（demo-ts-mp 未认证 0.2）
    assert not is_supported_profile(TechnicalProfile(
        profile_id="demo-ts-mp", profile_version="0.2", template_version="demo-0.1"))
    # JS 组合但模板不匹配
    assert not is_supported_profile(TechnicalProfile(
        profile_id="demo-js-mp", profile_version="0.1", template_version="demo-0.2"))


# --- JS 规格：确认 → Resolver → 锁定 ------------------------------------------

def test_js_spec_is_confirmable_and_resolves() -> None:
    from agentforge_platform.spec import SoftwareSpecification

    spec = SoftwareSpecification.model_validate(_js_spec_dict())

    assert scope_issues(spec) == []
    assert confirmation_issues(spec) == []
    assert is_supported_profile(spec.technical_profile)

    lock = resolve_assets(default_registry(), "demo-js-mp")
    assert lock.profile == "demo-js-mp"
    assert {asset.id for asset in lock.assets} == {
        "base-template", "task-crud-backend", "task-crud-frontend-js"
    }
    assert next(a for a in lock.assets if a.id == "base-template").version == "demo-0.2-js"


def test_js_lock_matches_committed_sample() -> None:
    committed = AssetLock.model_validate_json(JS_LOCK.read_text(encoding="utf-8"))

    lock = resolve_assets(default_registry(), "demo-js-mp")
    assert lock.lock_hash == committed.lock_hash
    verify_lock_hash(committed)


def test_js_profile_rejects_template_mismatch() -> None:
    from agentforge_platform.spec import SoftwareSpecification

    payload = _js_spec_dict()
    payload["technical_profile"]["template_version"] = "demo-0.2"  # 未认证组合
    spec = SoftwareSpecification.model_validate(payload)

    assert any("Profile" in issue for issue in confirmation_issues(spec))


# --- JS 前端能力包渲染（去类型） ----------------------------------------------

def test_js_frontend_pack_renders_plain_javascript() -> None:
    from agentforge_platform.spec import SoftwareSpecification

    spec = SoftwareSpecification.model_validate(_js_spec_dict())
    contract = compile_from_spec(spec)
    rendered = dict(render_pack_files(JS_PACK, contract))

    targets = set(rendered)
    assert any(target.endswith(".js") and "/api/" in target for target in targets)
    assert any(target.endswith("-api.spec.js") for target in targets)

    api_source = next(content for target, content in rendered.items() if target.endswith(".js")
                      and "/api/" in target)
    assert "export function list" in api_source
    assert ": Promise" not in api_source and "import type" not in api_source

    view = next(content for target, content in rendered.items() if target.endswith("ListView.vue"))
    assert '<script setup>' in view
    assert 'lang="ts"' not in view
    assert "as " not in view  # 无 TS 断言


# --- 旧 TS Profile 与锁原样重放 ----------------------------------------------

def test_legacy_ts_profile_and_lock_replay() -> None:
    committed = AssetLock.model_validate_json(TS_LOCK.read_text(encoding="utf-8"))
    lock = resolve_assets(default_registry(), "demo-ts-mp", pins={"task-crud-backend": "0.3.0"})

    assert lock.lock_hash == committed.lock_hash
    # 新增 JS 资产不改变 TS 组合（后端 0.1.3 仅兼容 demo-js-mp）
    assert {a.id for a in lock.assets} == {"base-template", "task-crud-backend", "task-crud-frontend"}
    assert next(a for a in lock.assets if a.id == "task-crud-backend").version == "0.3.0"
    verify_lock_hash(lock)
