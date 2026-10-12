"""R05 访谈问答 API 测试（假客户端，不触网）。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from agentforge_platform.clients import Completion, Usage
from agentforge_platform.db import session_scope
from agentforge_platform.models import Project, SpecVersion

ALICE = {"username": "alice", "password": "alice-secret"}
BOB = {"username": "bob", "password": "bob-secret"}


def _login(app, creds=ALICE) -> TestClient:
    client = TestClient(app)
    assert client.post("/api/auth/login", json=creds).status_code == 200
    return client


def _seed(factory, *, owner_id: int = 1) -> int:
    with session_scope(factory) as session:
        project = Project(owner_id=owner_id, name="p", requirement_text="校园活动报名")
        session.add(project)
        session.flush()
        session.add(
            SpecVersion(project_id=project.id, version=1, body_json="{}", hash="x")
        )
        session.flush()
        return project.id


def _q(question_id: str, affects: list[str], prompt: str = "成员如何操作该记录？") -> dict:
    return {
        "question_id": question_id, "type": "single_choice", "prompt": prompt,
        "options": ["可以", "不可以"], "allow_custom": False, "required": True,
        "affects": affects,
    }


def _payload(questions: list[dict]) -> str:
    return json.dumps({"unresolved_fields": [], "questions": questions}, ensure_ascii=False)


def _install_client(monkeypatch, responses: list[str]) -> list[dict]:
    calls: list[dict] = []
    counter = {"i": 0}

    def fake_complete(self, **kwargs):
        calls.append(kwargs)
        content = responses[min(counter["i"], len(responses) - 1)]
        counter["i"] += 1
        return Completion(content, "deepseek-flash", "rid", Usage(10, 5), "stop")

    monkeypatch.setattr("agentforge_platform.clients.DeepSeekClient.complete", fake_complete)
    return calls


def _post(client: TestClient, project_id: int, body: dict):
    return client.post(
        f"/api/projects/{project_id}/interview",
        json=body,
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    )


def _answer(question_id: str, selected=("可以",)) -> dict:
    return {"question_id": question_id, "question_version": 1, "selected": list(selected)}


def test_interview_two_rounds_then_ready_and_resume(platform_app, monkeypatch) -> None:
    app, factory = platform_app
    calls = _install_client(monkeypatch, [
        _payload([_q("q1", ["actors", "entities"])]),
        _payload([_q("q2", ["permissions", "business_rules"])]),
    ])
    project_id = _seed(factory)
    client = _login(app)

    assert client.get(f"/api/projects/{project_id}/interview").json()["data"]["status"] == "NEW"

    first = _post(client, project_id, {}).json()["data"]
    assert first["status"] == "CLARIFYING" and first["waiting"] is True
    assert [q["question_id"] for q in first["questions"]] == ["q1"]
    assert calls[0]["model"] == "deepseek-flash"

    second = _post(client, project_id, {"answers": [_answer("q1")]}).json()["data"]
    assert [q["question_id"] for q in second["questions"]] == ["q2"]
    assert second["round"] == 2

    final = _post(client, project_id, {"answers": [_answer("q2")]}).json()["data"]
    assert final["status"] == "READY_TO_CONFIRM" and final["waiting"] is False

    # 离开后再回来：状态持久化
    again = client.get(f"/api/projects/{project_id}/interview").json()["data"]
    assert again["status"] == "READY_TO_CONFIRM"
    assert len(calls) == 2  # 每轮一次生成


def test_post_without_answers_is_idempotent(platform_app, monkeypatch) -> None:
    app, factory = platform_app
    calls = _install_client(monkeypatch, [_payload([_q("q1", ["actors", "entities"])])])
    project_id = _seed(factory)
    client = _login(app)

    first = _post(client, project_id, {}).json()["data"]
    second = _post(client, project_id, {}).json()["data"]

    assert first["questions"] == second["questions"]
    assert len(calls) == 1  # 已在等待，不重复生成/计费


def test_stale_answers_are_reported_without_progress(platform_app, monkeypatch) -> None:
    app, factory = platform_app
    _install_client(monkeypatch, [
        _payload([_q("q1", ["actors", "entities"])]),
        _payload([_q("q2", ["permissions", "business_rules"])]),
    ])
    project_id = _seed(factory)
    client = _login(app)
    _post(client, project_id, {})
    _post(client, project_id, {"answers": [_answer("q1")]})  # 进入第 2 轮（q2）

    # 提交属于第 1 轮的过期答案
    stale = _post(client, project_id, {"answers": [_answer("q1")]}).json()["data"]

    assert stale["waiting"] is True
    assert [q["question_id"] for q in stale["questions"]] == ["q2"]  # 仍停在当前轮
    assert stale["issues"]  # 有清晰的“过期/不匹配”提示


def test_answers_require_pending_interview(platform_app, monkeypatch) -> None:
    app, factory = platform_app
    _install_client(monkeypatch, [_payload([_q("q1", ["actors", "entities"])])])
    project_id = _seed(factory)
    client = _login(app)

    # 未开始访谈就提交答案
    response = _post(client, project_id, {"answers": [_answer("q1")]})

    assert response.status_code == 409


def test_interview_requires_owner_and_csrf(platform_app, monkeypatch) -> None:
    app, factory = platform_app
    _install_client(monkeypatch, [_payload([_q("q1", ["actors", "entities"])])])
    project_id = _seed(factory)

    # 未登录取不到（401）
    assert TestClient(app).get(f"/api/projects/{project_id}/interview").status_code == 401
    # 非属主 → 403
    bob = _login(app, BOB)
    assert bob.get(f"/api/projects/{project_id}/interview").status_code == 403
    # 属主但缺 CSRF → 403
    alice = _login(app)
    assert alice.post(f"/api/projects/{project_id}/interview", json={}).status_code == 403


def test_interview_without_draft_is_404(platform_app) -> None:
    app, factory = platform_app
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        project_id = project.id
    client = _login(app)

    assert client.get(f"/api/projects/{project_id}/interview").status_code == 404
