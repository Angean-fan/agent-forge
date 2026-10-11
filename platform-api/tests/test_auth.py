"""M04 认证与归属测试：HttpOnly 会话、CSRF、跨用户拒绝。"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from fastapi.testclient import TestClient

from agentforge_platform.db import session_scope
from agentforge_platform.models import PlatformSession, Project

ALICE_CREDS = {"username": "alice", "password": "alice-secret"}
BOB_CREDS = {"username": "bob", "password": "bob-secret"}


def _login(client: TestClient, creds: dict[str, str]) -> None:
    response = client.post("/api/auth/login", json=creds)
    assert response.status_code == 200, response.text


def _seed_project(factory, owner_id: int, name: str) -> int:
    with session_scope(factory) as session:
        project = Project(owner_id=owner_id, name=name)
        session.add(project)
        session.flush()
        return project.id


def test_login_sets_httponly_session_cookie(platform_app) -> None:
    app, _ = platform_app
    client = TestClient(app)

    response = client.post("/api/auth/login", json=ALICE_CREDS)

    assert response.status_code == 200
    assert response.json()["data"]["account"] == {"id": 1, "username": "alice"}
    set_cookie = response.headers.get("set-cookie", "")
    assert "af_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert client.cookies.get("af_session")
    assert client.cookies.get("af_csrf")


def test_login_rejects_wrong_password(platform_app) -> None:
    app, _ = platform_app
    client = TestClient(app)

    response = client.post("/api/auth/login", json={"username": "alice", "password": "nope"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert client.cookies.get("af_session") is None


def test_me_requires_session(platform_app) -> None:
    app, _ = platform_app

    response = TestClient(app).get("/api/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_me_returns_account_after_login(platform_app) -> None:
    app, _ = platform_app
    client = TestClient(app)
    _login(client, ALICE_CREDS)

    response = client.get("/api/auth/me")

    assert response.status_code == 200
    assert response.json()["data"]["account"]["username"] == "alice"


def test_invalid_session_token_is_rejected(platform_app) -> None:
    app, _ = platform_app
    client = TestClient(app)
    _login(client, ALICE_CREDS)
    client.cookies.set("af_session", "not-a-real-token")

    response = client.get("/api/auth/me")

    assert response.status_code == 401


def test_expired_session_is_rejected(platform_app) -> None:
    app, factory = platform_app
    client = TestClient(app)
    _login(client, ALICE_CREDS)
    with session_scope(factory) as session:
        row = session.scalars(sa.select(PlatformSession)).one()
        row.expires_at = datetime(2000, 1, 1)

    assert client.get("/api/auth/me").status_code == 401


def test_state_change_without_csrf_is_rejected(platform_app) -> None:
    app, _ = platform_app
    client = TestClient(app)
    _login(client, ALICE_CREDS)

    response = client.post("/api/auth/logout")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_INVALID"
    assert client.get("/api/auth/me").status_code == 200


def test_logout_with_csrf_revokes_session(platform_app) -> None:
    app, _ = platform_app
    client = TestClient(app)
    _login(client, ALICE_CREDS)
    session_cookie = client.cookies.get("af_session")
    csrf = client.cookies.get("af_csrf")

    response = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})

    assert response.status_code == 200
    assert response.json()["data"]["ok"] is True
    # 即使重新带上旧 Cookie，服务端也已吊销会话
    client.cookies.set("af_session", session_cookie)
    assert client.get("/api/auth/me").status_code == 401


def test_project_ownership_is_enforced(platform_app) -> None:
    app, factory = platform_app
    project_id = _seed_project(factory, owner_id=1, name="alice-project")

    alice = TestClient(app)
    _login(alice, ALICE_CREDS)
    bob = TestClient(app)
    _login(bob, BOB_CREDS)
    anonymous = TestClient(app)

    assert anonymous.get(f"/api/projects/{project_id}").status_code == 401
    assert alice.get(f"/api/projects/{project_id}").status_code == 200
    assert bob.get(f"/api/projects/{project_id}").status_code == 403
    assert alice.get("/api/projects/424242").status_code == 404
