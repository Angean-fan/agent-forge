"""M05 项目创建与需求草稿测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

ALICE_CREDS = {"username": "alice", "password": "alice-secret"}
BOB_CREDS = {"username": "bob", "password": "bob-secret"}

VALID_NAME = "我的任务管理"
VALID_REQUIREMENT = "请帮我做一个个人任务管理系统，支持登录、任务增删改查、分页和搜索。"


def _auth_client(app, creds: dict[str, str]) -> TestClient:
    client = TestClient(app)
    response = client.post("/api/auth/login", json=creds)
    assert response.status_code == 200, response.text
    return client


def _create(client: TestClient, name: str, requirement: str):
    csrf = client.cookies.get("af_csrf")
    return client.post(
        "/api/projects",
        json={"name": name, "requirement": requirement},
        headers={"X-CSRF-Token": csrf},
    )


def test_create_project_persists_requirement(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)

    response = _create(client, VALID_NAME, VALID_REQUIREMENT)

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["name"] == VALID_NAME
    assert data["requirement"] == VALID_REQUIREMENT
    assert data["active_spec_id"] is None
    assert data["id"] > 0
    assert data["created_at"]


def test_create_project_requires_login(platform_app) -> None:
    app, _ = platform_app

    response = TestClient(app).post(
        "/api/projects", json={"name": VALID_NAME, "requirement": VALID_REQUIREMENT}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_create_project_requires_csrf(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)

    response = client.post(
        "/api/projects", json={"name": VALID_NAME, "requirement": VALID_REQUIREMENT}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_INVALID"


def test_create_project_rejects_name_out_of_range(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)

    assert _create(client, "甲" * 61, VALID_REQUIREMENT).status_code == 422
    assert _create(client, "   ", VALID_REQUIREMENT).status_code == 422
    assert _create(client, "甲", VALID_REQUIREMENT).status_code == 201


def test_create_project_rejects_requirement_out_of_range(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)

    too_short = _create(client, VALID_NAME, "甲" * 19)
    too_long = _create(client, VALID_NAME, "甲" * 5001)
    boundary = _create(client, VALID_NAME, "甲" * 20)

    assert too_short.status_code == 422
    assert too_long.status_code == 422
    assert boundary.status_code == 201


def test_list_returns_only_own_projects(platform_app) -> None:
    app, _ = platform_app
    alice = _auth_client(app, ALICE_CREDS)
    bob = _auth_client(app, BOB_CREDS)

    _create(alice, "alice-1", VALID_REQUIREMENT)
    _create(alice, "alice-2", VALID_REQUIREMENT)
    _create(bob, "bob-1", VALID_REQUIREMENT)

    alice_items = alice.get("/api/projects").json()["data"]["items"]
    bob_items = bob.get("/api/projects").json()["data"]["items"]

    assert {item["name"] for item in alice_items} == {"alice-1", "alice-2"}
    assert [item["name"] for item in bob_items] == ["bob-1"]


def test_get_project_detail_returns_requirement(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    created = _create(client, VALID_NAME, VALID_REQUIREMENT).json()["data"]

    response = client.get(f"/api/projects/{created['id']}")

    assert response.status_code == 200
    assert response.json()["data"]["requirement"] == VALID_REQUIREMENT


def test_get_project_detail_requires_login(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    created = _create(client, VALID_NAME, VALID_REQUIREMENT).json()["data"]

    response = TestClient(app).get(f"/api/projects/{created['id']}")

    assert response.status_code == 401


def test_get_project_detail_denies_cross_user_and_missing(platform_app) -> None:
    app, _ = platform_app
    alice = _auth_client(app, ALICE_CREDS)
    bob = _auth_client(app, BOB_CREDS)
    created = _create(alice, VALID_NAME, VALID_REQUIREMENT).json()["data"]

    assert bob.get(f"/api/projects/{created['id']}").status_code == 403
    assert alice.get("/api/projects/99999").status_code == 404
