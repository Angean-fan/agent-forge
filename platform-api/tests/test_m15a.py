"""M15A 生成资产安全补强测试（角色/分页/令牌/dockerignore/版本化）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentforge_platform.asset_registry import default_registry
from agentforge_platform.capability_packs import PackRenderError, render_pack_files
from agentforge_platform.contract import BusinessContract, compile_from_spec
from agentforge_platform.resolver import resolve_assets
from agentforge_platform.spec import SoftwareSpecification

REPO = Path(__file__).resolve().parents[2]
BACKEND_PACK = REPO / "capability-packs" / "task-crud-backend" / "0.1.2"
TEMPLATE_FRONTEND = REPO / "templates" / "demo-0.2" / "frontend"
SAMPLE_SPEC = REPO / "schemas" / "task-management.spec.json"


def _spec(**overrides) -> SoftwareSpecification:
    raw = json.loads(SAMPLE_SPEC.read_text(encoding="utf-8"))
    raw.update(overrides)
    return SoftwareSpecification.model_validate(raw)


def _controller(contract: BusinessContract) -> str:
    for relative, content in render_pack_files(BACKEND_PACK, contract):
        if relative.endswith("TaskController.java"):
            return content
    raise AssertionError("TaskController.java not rendered")


READ_ONLY_MEMBER = [
    {"role": "ADMIN", "actions": ["create", "read", "update"], "source": "user_confirmed"},
    {"role": "MEMBER", "actions": ["read"], "source": "user_confirmed"},
]


def test_registry_keeps_multiple_asset_versions() -> None:
    keys = {(m.kind, m.id, m.version) for m in default_registry().all()}

    assert ("template", "base-template", "demo-0.1") in keys
    assert ("template", "base-template", "demo-0.2") in keys
    assert ("capability-pack", "task-crud-backend", "0.1.0") in keys
    assert ("capability-pack", "task-crud-backend", "0.1.1") in keys
    assert ("capability-pack", "task-crud-backend", "0.1.2") in keys


def test_resolver_selects_latest_and_locks_mysql_digest() -> None:
    lock = resolve_assets(default_registry(), "demo-ts-mp")

    versions = {(a.id, a.version) for a in lock.assets}
    assert ("base-template", "demo-0.2") in versions
    assert ("task-crud-backend", "0.3.0") in versions
    assert lock.mysql_image.reference == "mysql:8.0.43"
    assert lock.mysql_image.digest.startswith("sha256:")


def test_old_versions_and_pinned_lock_still_valid(tmp_path: Path) -> None:
    registry = default_registry()
    old_lock = resolve_assets(
        registry,
        "demo-ts-mp",
        pins={
            "base-template": "demo-0.1",
            "task-crud-backend": "0.1.0",
            "task-crud-frontend": "0.1.0",
        },
    )

    assert {a.version for a in old_lock.assets} == {"demo-0.1", "0.1.0"}
    for asset in old_lock.assets:
        registry.verify_checksum(asset.kind, asset.id, asset.version)


def test_controller_enforces_route_roles() -> None:
    controller = _controller(compile_from_spec(_spec()))

    assert "requireAnyRole" in controller
    assert 'List.of("ADMIN", "MEMBER")' in controller


def test_readonly_role_contract_narrows_roles() -> None:
    controller = _controller(compile_from_spec(_spec(permissions=READ_ONLY_MEMBER)))

    assert 'List.of("ADMIN")' in controller  # create/update 仅 ADMIN
    assert "validatePaging" in controller


def test_empty_allowed_roles_renders_empty_list() -> None:
    no_delete = [
        {"role": "ADMIN", "actions": ["create", "read", "update"], "source": "user_confirmed"},
        {"role": "MEMBER", "actions": ["read"], "source": "user_confirmed"},
    ]
    controller = _controller(compile_from_spec(_spec(permissions=no_delete)))

    assert "List.of()" in controller  # delete 无角色 → 拒绝


def test_generated_api_fixture_uses_values_matching_field_types() -> None:
    raw = json.loads(SAMPLE_SPEC.read_text(encoding="utf-8"))
    raw["entities"][0]["fields"].extend([
        {"name": "priority", "type": "int", "required": True},
        {"name": "due_date", "type": "date", "required": False},
        {"name": "done", "type": "boolean", "required": False},
    ])
    contract = compile_from_spec(SoftwareSpecification.model_validate(raw))
    api_test = next(
        content for relative, content in render_pack_files(BACKEND_PACK, contract)
        if relative.endswith("TaskApiTest.java")
    )
    assert 'map.put("priority", 1);' in api_test
    assert 'map.put("due_date", "2026-01-01");' in api_test
    assert 'map.put("done", true);' in api_test


def test_controller_has_pagination_bounds() -> None:
    controller = _controller(compile_from_spec(_spec()))

    assert "MAX_PAGE_SIZE" in controller
    assert "分页参数越界" in controller


def test_route_roles_mismatch_is_rejected() -> None:
    raw = compile_from_spec(_spec()).model_dump()
    raw["routes"][1]["allowed_roles"] = ["MEMBER"]  # create 路由与权限矩阵不一致

    with pytest.raises(PackRenderError) as error:
        render_pack_files(BACKEND_PACK, BusinessContract.model_validate(raw))

    assert error.value.code == "CONTRACT_CONFLICT"


def test_generated_frontend_has_no_persistent_token_storage() -> None:
    client = (TEMPLATE_FRONTEND / "src" / "api" / "client.ts").read_text("utf-8")

    assert "localStorage.setItem" not in client
    assert "localStorage.getItem" not in client
    assert "sessionStorage" not in client.replace("绝不写入 localStorage/sessionStorage", "")


def _dockerignore_patterns(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_dockerignore_excludes_secrets_but_keeps_lockfiles() -> None:
    backend = _dockerignore_patterns(
        (REPO / "templates" / "demo-0.2" / "backend" / ".dockerignore").read_text("utf-8")
    )
    frontend = _dockerignore_patterns(
        (REPO / "templates" / "demo-0.2" / "frontend" / ".dockerignore").read_text("utf-8")
    )

    assert ".env" in backend and "target" in backend
    assert ".env" in frontend and "node_modules" in frontend and "dist" in frontend
    assert "package-lock.json" not in frontend  # 锁文件必须留在构建上下文
    assert "pom.xml" not in backend
