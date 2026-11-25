"""项目删除接口（DELETE /api/projects/{project_id}）测试：归属校验、CSRF 与级联清理。"""

from __future__ import annotations

from conftest import ALICE, BOB
from fastapi.testclient import TestClient
from sqlalchemy import select

from agentforge_platform.models import Run, RunEvent, SpecVersion


def _login(client: TestClient, account: dict[str, str]) -> str:
    resp = client.post(
        "/api/auth/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert resp.status_code == 200
    csrf = resp.cookies.get("af_csrf")
    assert csrf
    return csrf


def test_delete_project_owner_cascade_and_isolation(platform_app):
    app, session_factory = platform_app

    # 1) ALICE 创建项目，并插入规格与运行记录
    with TestClient(app) as alice_client:
        alice_csrf = _login(alice_client, ALICE)
        create_resp = alice_client.post(
            "/api/projects",
            headers={"X-CSRF-Token": alice_csrf},
            json={
                "name": "待删除测试项目",
                "requirement": "这是一个用于验证项目删除与级联清理功能的完整测试需求描述。",
            },
        )
        assert create_resp.status_code == 201
        project_id = create_resp.json()["data"]["id"]

        with session_factory() as db:
            spec = SpecVersion(
                project_id=project_id,
                version=1,
                draft_revision=1,
                body_json="{}",
                hash="a" * 64,
            )
            db.add(spec)
            db.flush()
            run = Run(project_id=project_id, spec_id=spec.id, status="FAILED")
            db.add(run)
            db.flush()
            db.add(RunEvent(run_id=run.id, sequence=1, type="queued", payload="{}"))
            db.commit()

        # 2) BOB 不能删除 ALICE 的项目（跨用户隔离）
        with TestClient(app) as bob_client:
            bob_csrf = _login(bob_client, BOB)
            forbid_resp = bob_client.delete(
                f"/api/projects/{project_id}",
                headers={"X-CSRF-Token": bob_csrf},
            )
            assert forbid_resp.status_code in (403, 404)

        # 3) 缺少 CSRF 令牌被拒绝
        no_csrf_resp = alice_client.delete(f"/api/projects/{project_id}")
        assert no_csrf_resp.status_code == 403

        # 4) ALICE 携带 CSRF 删除成功并级联清理
        del_resp = alice_client.delete(
            f"/api/projects/{project_id}",
            headers={"X-CSRF-Token": alice_csrf},
        )
        assert del_resp.status_code == 200
        assert del_resp.json()["data"] == {"deleted": True, "project_id": project_id}

        # 列表和详情均不可再查到
        assert alice_client.get(f"/api/projects/{project_id}").status_code == 404
        list_ids = [p["id"] for p in alice_client.get("/api/projects").json()["data"]["items"]]
        assert project_id not in list_ids

        with session_factory() as db:
            assert db.scalars(select(SpecVersion).where(SpecVersion.project_id == project_id)).all() == []
            assert db.scalars(select(Run).where(Run.project_id == project_id)).all() == []
