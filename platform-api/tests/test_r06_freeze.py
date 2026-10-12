"""R06：冻结验收场景与规格修改版本。

覆盖：规格版本差异、最终需求摘要（DeepSeek / 已验证 high 路径且不谎称）、
run 绑定冻结 hash 且跨版本稳定、人为篡改冻结验收断言在**提交**与**生成**两处被拒。
不触网：模型用脚本化假客户端。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from agentforge_platform.clients import Completion, DeepSeekClient, Usage
from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.models import ModelCall, Project, Run, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

ALICE_CREDS = {"username": "alice", "password": "alice-secret"}
BOB_CREDS = {"username": "bob", "password": "bob-secret"}
REQUIREMENT = "请帮我做一个个人任务管理系统，支持登录、任务增删改查、分页与搜索。"
SAMPLE_PATH = Path(__file__).resolve().parents[2] / "schemas" / "task-management.spec.json"

_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


def _sample() -> dict:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


def _complex_sample() -> dict:
    """业务规则+验收场景 ≥ 6（判定为“复杂汇总”）。"""
    spec = _sample()
    spec["business_rules"].append({"id": "BR03", "text": "任务删除后不可恢复", "source": "user_confirmed"})
    spec["acceptance_cases"].append(
        {"id": "AC03", "scenario": "管理员删除任务", "expected": "任务从列表消失", "source": "user_confirmed"}
    )
    return spec


def _auth_client(app, creds: dict[str, str]):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    assert client.post("/api/auth/login", json=creds).status_code == 200
    return client


def _create_project(client) -> int:
    response = client.post(
        "/api/projects",
        json={"name": "任务管理", "requirement": REQUIREMENT},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def _current(client, project_id: int) -> dict | None:
    response = client.get(f"/api/projects/{project_id}/spec")
    return response.json()["data"] if response.status_code == 200 else None


def _put(client, project_id: int, version: int, spec: dict):
    current = _current(client, project_id)
    revision = current["draft_revision"] if current else 0
    return client.put(
        f"/api/projects/{project_id}/spec",
        json={"version": version, "draft_revision": revision, "spec": spec},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    )


def _confirm(client, project_id: int, version: int):
    current = _current(client, project_id)
    revision = current["draft_revision"] if current else 0
    return client.post(
        f"/api/projects/{project_id}/confirm",
        json={"version": version, "draft_revision": revision},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    )


def _submit(client, project_id: int, key: str):
    return client.post(
        f"/api/projects/{project_id}/runs",
        json={},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf"), "Idempotency-Key": key},
    )


def _summary(client, project_id: int, version: int, revision: int):
    return client.post(
        f"/api/projects/{project_id}/summary",
        json={"version": version, "draft_revision": revision},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    )


class RecordingClient:
    """脚本化假 DeepSeek：记录载荷并回一段合法最终摘要 JSON。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete(
        self, *, model, prompt, thinking_mode=None, json_mode=False,
        max_output_tokens=None, reasoning_effort=None,
    ) -> Completion:
        self.calls.append({
            "model": model, "thinking_mode": thinking_mode, "reasoning_effort": reasoning_effort,
        })
        content = json.dumps({"summary": "本系统供成员管理属于自己的任务，支持增删改查与搜索。"}, ensure_ascii=False)
        return Completion(content=content, model=model, request_id="rid", usage=Usage(12, 6), finish_reason="stop")


def _patch_summary_client(monkeypatch, recorder: RecordingClient) -> None:
    monkeypatch.setattr(DeepSeekClient, "complete", recorder.complete)


# --- 规格版本差异 -------------------------------------------------------------

def test_spec_diff_reports_added_changed_removed(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    _put(client, project_id, 0, _sample())
    _confirm(client, project_id, 1)

    edited = _sample()
    edited["exclusions"] = ["不做注册、找回密码等账号流程"]  # 少一项排除
    edited["business_rules"].append({"id": "BR03", "text": "任务标题不可为空", "source": "user_confirmed"})
    next(c for c in edited["acceptance_cases"] if c["id"] == "AC02")["expected"] = "请求被拒且不泄露他人数据"
    assert _put(client, project_id, 1, edited).status_code == 200

    response = client.get(f"/api/projects/{project_id}/spec/diff?from_version=1&to_version=2")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["from"]["version"] == 1 and data["to"]["version"] == 2
    keys = {(c["section"], c["key"], c["kind"]) for c in data["changes"]}
    assert ("business_rules", "BR03", "added") in keys
    assert ("acceptance_cases", "AC02", "changed") in keys
    assert any(section == "exclusions" and kind == "removed" for section, _, kind in keys)


def test_spec_diff_unknown_version_is_not_found(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    _put(client, project_id, 0, _sample())

    response = client.get(f"/api/projects/{project_id}/spec/diff?from_version=1&to_version=9")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SPEC_NOT_FOUND"


# --- 旧 run 指向旧 spec 的 hash ----------------------------------------------

def test_run_binds_frozen_hash_and_keeps_old_version(platform_app) -> None:
    app, _ = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    _put(client, project_id, 0, _sample())
    v1 = _confirm(client, project_id, 1).json()["data"]

    run1 = _submit(client, project_id, "k1").json()["data"]
    assert run1["spec_hash"] == v1["hash"]

    changed = _sample()
    changed["business_rules"].append({"id": "BR03", "text": "任务标题不可为空", "source": "user_confirmed"})
    _put(client, project_id, 1, changed)
    v2 = _confirm(client, project_id, 2).json()["data"]
    assert v2["hash"] != v1["hash"]
    run2 = _submit(client, project_id, "k2").json()["data"]

    assert run2["spec_hash"] == v2["hash"]
    # 旧 run 仍指向旧 spec 的 hash（不随新版本漂移）
    assert client.get(f"/api/runs/{run1['id']}").json()["data"]["spec_hash"] == v1["hash"]


# --- 篡改冻结验收断言被拒 -----------------------------------------------------

def test_tampering_frozen_acceptance_is_rejected_at_submit(platform_app) -> None:
    app, factory = platform_app
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    _put(client, project_id, 0, _sample())
    confirmed = _confirm(client, project_id, 1).json()["data"]

    with session_scope(factory) as session:
        row = session.get(SpecVersion, confirmed["spec_id"])
        body = json.loads(row.body_json)
        body["acceptance_cases"][0]["expected"] = "任何角色都可读取所有任务"  # 篡改冻结验收断言
        row.body_json = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        # 故意不更新 row.hash

    response = _submit(client, project_id, "k1")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SPEC_TAMPERED"


def test_tampering_after_run_creation_fails_generation(platform_app, tmp_path: Path) -> None:
    _app, factory = platform_app
    with session_scope(factory) as session:
        spec = SoftwareSpecification.model_validate(_sample())
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        row = SpecVersion(
            project_id=project.id, version=1,
            body_json=canonical_json(spec), hash=content_hash(spec),
            confirmed_at=datetime(2026, 1, 1),
        )
        session.add(row)
        session.flush()
        project.active_spec_id = row.id
        run = create_run(session, project, row.id, idempotency_key="k1", spec_hash=content_hash(spec))
        run_id, spec_id = run.id, row.id

    # run 创建之后篡改冻结规格（含验收断言）
    with session_scope(factory) as session:
        target = session.get(SpecVersion, spec_id)
        body = json.loads(target.body_json)
        body["acceptance_cases"][0]["expected"] = "无人可读取他人任务（已被篡改）"
        target.body_json = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    from agentforge_platform.asset_registry import default_registry

    class _PassRunner:
        def verify(self, artifact_dir: str) -> dict:
            return _PASS_VERIFY

        def accept(self, artifact_dir: str) -> dict:
            return _PASS_ACCEPT

    context = GenerationContext(
        session_factory=factory, runner=_PassRunner(), work_root=tmp_path / "runs",
        registry=default_registry(),
    )
    result = run_generation(context, run_id)

    assert result["ok"] is False
    with session_scope(factory) as session:
        assert session.get(Run, run_id).error_code == "SPEC_TAMPERED"


# --- 最终需求摘要 -------------------------------------------------------------

def test_final_summary_simple_uses_disabled_and_persists(platform_app, monkeypatch) -> None:
    app, _ = platform_app
    monkeypatch.setenv("AGENTFORGE_VERIFIED_REASONING_EFFORTS", "low,high")
    recorder = RecordingClient()
    _patch_summary_client(monkeypatch, recorder)
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    created = _put(client, project_id, 0, _sample()).json()["data"]

    response = _summary(client, project_id, created["version"], created["draft_revision"])

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["summary"]
    assert data["thinking_mode"] == "disabled" and data["reasoning_effort"] is None
    assert recorder.calls[0]["model"] == "deepseek-flash"
    assert client.get(f"/api/projects/{project_id}/spec").json()["data"]["summary"] == data["summary"]


def test_final_summary_complex_uses_verified_high(platform_app, monkeypatch) -> None:
    app, factory = platform_app
    monkeypatch.setenv("AGENTFORGE_VERIFIED_REASONING_EFFORTS", "low,high")
    recorder = RecordingClient()
    _patch_summary_client(monkeypatch, recorder)
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    created = _put(client, project_id, 0, _complex_sample()).json()["data"]

    response = _summary(client, project_id, created["version"], created["draft_revision"])

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["thinking_mode"] == "enabled" and data["reasoning_effort"] == "high"
    assert recorder.calls[0]["thinking_mode"] == "enabled"
    assert recorder.calls[0]["reasoning_effort"] == "high"
    with session_scope(factory) as session:
        row = session.scalars(select(ModelCall)).first()
        assert row.thinking_mode == "enabled" and row.reasoning_effort == "high"


def test_final_summary_complex_without_high_probe_falls_back(platform_app, monkeypatch) -> None:
    app, _ = platform_app
    monkeypatch.setenv("AGENTFORGE_VERIFIED_REASONING_EFFORTS", "low")  # high 未验证
    recorder = RecordingClient()
    _patch_summary_client(monkeypatch, recorder)
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    created = _put(client, project_id, 0, _complex_sample()).json()["data"]

    response = _summary(client, project_id, created["version"], created["draft_revision"])

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    # 降级到已验证的 low，绝不谎称使用 high
    assert data["reasoning_effort"] == "low"
    assert data["thinking_mode"] == "enabled"


def test_final_summary_with_pending_is_rejected_without_calling_model(platform_app, monkeypatch) -> None:
    app, _ = platform_app
    recorder = RecordingClient()
    _patch_summary_client(monkeypatch, recorder)
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    pending = _sample()
    pending["business_rules"][0]["source"] = "pending"
    created = _put(client, project_id, 0, pending).json()["data"]

    response = _summary(client, project_id, created["version"], created["draft_revision"])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SPEC_NOT_READY"
    assert recorder.calls == []  # 未确认项未清零：不调用模型


def test_final_summary_on_confirmed_spec_conflicts(platform_app, monkeypatch) -> None:
    app, _ = platform_app
    recorder = RecordingClient()
    _patch_summary_client(monkeypatch, recorder)
    client = _auth_client(app, ALICE_CREDS)
    project_id = _create_project(client)
    created = _put(client, project_id, 0, _sample()).json()["data"]
    _confirm(client, project_id, created["version"])

    response = _summary(client, project_id, created["version"], created["draft_revision"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SPEC_CONFLICT"


# --- 权限/CSRF/归属 -----------------------------------------------------------

def test_r06_endpoints_require_auth_csrf_and_ownership(platform_app) -> None:
    from fastapi.testclient import TestClient

    app, _ = platform_app
    alice = _auth_client(app, ALICE_CREDS)
    bob = _auth_client(app, BOB_CREDS)
    project_id = _create_project(alice)
    created = _put(alice, project_id, 0, _sample()).json()["data"]

    anonymous = TestClient(app)
    assert anonymous.get(f"/api/projects/{project_id}/spec/diff?from_version=1&to_version=1").status_code == 401
    assert anonymous.post(
        f"/api/projects/{project_id}/summary",
        json={"version": created["version"], "draft_revision": created["draft_revision"]},
    ).status_code == 401

    # 已登录但缺 CSRF
    no_csrf = alice.post(
        f"/api/projects/{project_id}/summary",
        json={"version": created["version"], "draft_revision": created["draft_revision"]},
    )
    assert no_csrf.status_code == 403

    # 跨用户：diff 读取与 summary 均被拒
    assert bob.get(f"/api/projects/{project_id}/spec/diff?from_version=1&to_version=1").status_code == 403
    cross = bob.post(
        f"/api/projects/{project_id}/summary",
        json={"version": created["version"], "draft_revision": created["draft_revision"]},
        headers={"X-CSRF-Token": bob.cookies.get("af_csrf")},
    )
    assert cross.status_code == 403
