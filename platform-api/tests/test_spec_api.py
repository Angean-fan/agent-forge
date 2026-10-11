"""M07 规格草稿/确认 API 测试。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from agentforge_platform.db import session_scope
from agentforge_platform.models import SpecVersion
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import content_hash

ALICE_CREDS = {"username": "alice", "password": "alice-secret"}
BOB_CREDS = {"username": "bob", "password": "bob-secret"}
REQUIREMENT = "请帮我做一个个人任务管理系统，支持登录、任务增删改查、分页与搜索。"
SAMPLE_PATH = Path(__file__).resolve().parents[2] / "schemas" / "task-management.spec.json"


def _sample() -> dict:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


def _auth_client(app, creds: dict[str, str]) -> TestClient:
    client = TestClient(app)
    assert client.post("/api/auth/login", json=creds).status_code == 200
    return client


def _create_project(client: TestClient) -> int:
    response = client.post(
        "/api/projects",
        json={"name": "任务管理", "requirement": REQUIREMENT},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def _put_spec(client: TestClient, project_id: int, version: int, spec: dict):
    return client.put(
        f"/api/projects/{project_id}/spec",
        json={"version": version, "spec": spec},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    )


def _confirm(client: TestClient, project_id: int, version: int):
    return client.post(
        f"/api/projects/{project_id}/confirm",
        json={"version": version},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    )


def test_get_spec_before_draft_is_not_found(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)

    response = client.get(f"/api/projects/{project_id}/spec")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SPEC_NOT_FOUND"


def test_put_creates_draft_and_get_reads_it(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)

    created = _put_spec(client, project_id, 0, _sample())
    assert created.status_code == 200, created.text
    data = created.json()["data"]
    assert data["version"] == 1
    assert data["confirmed"] is False
    assert data["hash"]
    assert data["issues"] == []

    fetched = client.get(f"/api/projects/{project_id}/spec").json()["data"]
    assert fetched["spec"]["entities"][0]["name"] == "Task"
    assert fetched["version"] == 1


def test_put_with_stale_version_conflicts(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    _put_spec(client, project_id, 0, _sample())

    response = _put_spec(client, project_id, 0, _sample())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SPEC_CONFLICT"


def test_confirm_freezes_spec_and_sets_active_spec(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    _put_spec(client, project_id, 0, _sample())

    response = _confirm(client, project_id, 1)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["spec_id"] > 0
    assert data["version"] == 1
    assert data["hash"]
    assert data["confirmed_at"]

    project = client.get(f"/api/projects/{project_id}").json()["data"]
    assert project["active_spec_id"] == data["spec_id"]

    fetched = client.get(f"/api/projects/{project_id}/spec").json()["data"]
    assert fetched["confirmed"] is True
    assert fetched["confirmed_at"]


def test_editing_confirmed_spec_creates_new_version(platform_app) -> None:
    app, factory = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    _put_spec(client, project_id, 0, _sample())
    confirmed = _confirm(client, project_id, 1).json()["data"]

    edited = _put_spec(client, project_id, 1, _sample())

    assert edited.status_code == 200
    data = edited.json()["data"]
    assert data["version"] == 2
    assert data["confirmed"] is False
    assert data["spec_id"] != confirmed["spec_id"]

    # 已确认版本被冻结，未原地修改
    with session_scope(factory) as session:
        frozen = session.get(SpecVersion, confirmed["spec_id"])
        assert frozen is not None
        assert frozen.version == 1
        assert frozen.confirmed_at is not None


def test_confirm_with_issues_is_rejected(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    incomplete = _sample()
    incomplete["entities"] = []
    _put_spec(client, project_id, 0, incomplete)

    response = _confirm(client, project_id, 1)

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "SPEC_NOT_READY"
    assert "实体" in body["message"]


def test_confirm_with_pending_is_rejected(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    pending = _sample()
    pending["business_rules"] = [{"id": "BR01", "text": "标题必填", "source": "pending"}]
    _put_spec(client, project_id, 0, pending)

    response = _confirm(client, project_id, 1)

    assert response.status_code == 422
    assert "pending" in response.json()["error"]["message"]


def test_confirm_rejects_spec_that_cannot_build_task_pack(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    incomplete = _sample()
    incomplete["entities"][0]["fields"] = [
        field for field in incomplete["entities"][0]["fields"] if field["name"] != "owner_id"
    ]
    _put_spec(client, project_id, 0, incomplete)

    response = _confirm(client, project_id, 1)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SPEC_NOT_READY"


def test_confirm_rejects_unsafe_field_identifier(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    unsafe = _sample()
    unsafe["entities"][0]["fields"][1]["name"] = "title; Runtime.getRuntime()"
    _put_spec(client, project_id, 0, unsafe)

    response = _confirm(client, project_id, 1)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SPEC_NOT_READY"


def test_confirm_rejects_java_keyword_and_unsearchable_task(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    keyword = _sample()
    keyword["entities"][0]["fields"].append(
        {"name": "boolean", "type": "boolean", "required": False}
    )
    assert _put_spec(client, project_id, 0, keyword).status_code == 200
    assert _confirm(client, project_id, 1).status_code == 422

    unsearchable = _sample()
    for field in unsearchable["entities"][0]["fields"]:
        if field["type"] == "string":
            field["type"] = "int"
    assert _put_spec(client, project_id, 1, unsearchable).status_code == 200
    assert _confirm(client, project_id, 1).status_code == 422


def test_confirm_requires_creator_reader_role(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    spec = _sample()
    spec["permissions"] = [
        {"role": "MEMBER", "actions": ["read"], "source": "user_confirmed"}
    ]
    assert _put_spec(client, project_id, 0, spec).status_code == 200
    assert _confirm(client, project_id, 1).status_code == 422


def test_confirm_rejects_wrong_fixed_column_types(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    spec = _sample()
    next(field for field in spec["entities"][0]["fields"] if field["name"] == "owner_id")["type"] = "string"
    assert _put_spec(client, project_id, 0, spec).status_code == 200
    assert _confirm(client, project_id, 1).status_code == 422


def test_confirm_with_version_mismatch_conflicts(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    _put_spec(client, project_id, 0, _sample())

    response = _confirm(client, project_id, 7)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SPEC_CONFLICT"


def test_confirm_without_draft_is_not_found(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    _put_spec(client, project_id, 0, _sample())
    _confirm(client, project_id, 1)

    response = _confirm(client, project_id, 1)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SPEC_NOT_FOUND"


def test_hash_is_stable_for_identical_content(platform_app) -> None:
    app, _ = platform_app
    alice = _auth_client(app, ALICE_CREDS)
    first = _create_project(alice)
    second = _create_project(alice)

    hash_one = _put_spec(alice, first, 0, _sample()).json()["data"]["hash"]
    hash_two = _put_spec(alice, second, 0, _sample()).json()["data"]["hash"]

    assert hash_one == hash_two
    # 键顺序不同但内容相同 -> hash 稳定
    raw = _sample()
    reordered = {key: raw[key] for key in reversed(list(raw))}
    model_a = SoftwareSpecification.model_validate(raw)
    model_b = SoftwareSpecification.model_validate(reordered)
    assert content_hash(model_a) == content_hash(model_b)


def test_spec_endpoints_require_auth_and_csrf(platform_app) -> None:
    app, _ = platform_app
    alice = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(alice)

    anonymous = TestClient(app)
    assert anonymous.get(f"/api/projects/{project_id}/spec").status_code == 401
    assert (
        anonymous.put(
            f"/api/projects/{project_id}/spec",
            json={"version": 0, "spec": _sample()},
        ).status_code
        == 401
    )

    # 已登录但缺 CSRF
    no_csrf = alice.put(
        f"/api/projects/{project_id}/spec", json={"version": 0, "spec": _sample()}
    )
    assert no_csrf.status_code == 403
    assert no_csrf.json()["error"]["code"] == "CSRF_INVALID"


def test_cross_user_spec_access_is_denied(platform_app) -> None:
    app, _ = platform_app
    alice = _auth_client(app, ALICE_CREDS)
    bob = _auth_client(app, BOB_CREDS)
    project_id = _create_project(alice)
    _put_spec(alice, project_id, 0, _sample())

    assert bob.get(f"/api/projects/{project_id}/spec").status_code == 403
    put = bob.put(
        f"/api/projects/{project_id}/spec",
        json={"version": 1, "spec": _sample()},
        headers={"X-CSRF-Token": bob.cookies.get("af_csrf")},
    )
    assert put.status_code == 403
