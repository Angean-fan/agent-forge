"""R09：四组合 Profile 认证、兼容矩阵、Method Unit 兼容筛选与选型通俗解释。

覆盖：四组合独立锁与确定性矩阵；js-mb 第 4 组合；四组合各验证一次 Method Unit 兼容/不兼容筛选；
`GET /api/profiles`（鉴权）与 `POST /api/projects/{id}/profile-explanation`（DeepSeek 假客户端，
路由只命中 deepseek-flash）；旧三组合锁原样重放。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from agentforge_platform.asset_finder import Candidate, CapabilityQuery, find_assets
from agentforge_platform.asset_registry import default_registry
from agentforge_platform.clients import Completion, DeepSeekClient, Usage
from agentforge_platform.db import session_scope
from agentforge_platform.models import ModelCall
from agentforge_platform.profiles_matrix import list_profile_options
from agentforge_platform.resolver import AssetLock, resolve_assets, verify_lock_hash
from agentforge_platform.spec import TechnicalProfile, is_supported_profile

REPO = Path(__file__).resolve().parents[2]
SAMPLES = {
    "demo-ts-mp": REPO / "schemas" / "asset-lock.r27.sample.json",
    "demo-js-mp": REPO / "schemas" / "asset-lock.js-r27.sample.json",
    "demo-ts-mb": REPO / "schemas" / "asset-lock.mb.sample.json",
    "demo-js-mb": REPO / "schemas" / "asset-lock.js-mb.sample.json",
}
EXPECTED_ASSETS = {
    "demo-ts-mp": {"task-crud-backend", "task-crud-frontend", "base-template"},
    "demo-js-mp": {"task-crud-backend", "task-crud-frontend-js", "base-template"},
    "demo-ts-mb": {"task-crud-backend-mb", "task-crud-frontend", "base-template"},
    "demo-js-mb": {"task-crud-backend-mb", "task-crud-frontend-js", "base-template"},
}
ALICE = {"username": "alice", "password": "alice-secret"}
BOB = {"username": "bob", "password": "bob-secret"}
REQUIREMENT = "请帮我做一个个人任务管理系统，支持登录、任务增删改查、分页与搜索。"


def _auth_client(app, creds):
    client = TestClient(app)
    assert client.post("/api/auth/login", json=creds).status_code == 200
    return client


# --- 四组合独立锁 + 矩阵 ------------------------------------------------------

def test_four_combinations_resolve_to_expected_assets() -> None:
    for profile_id, expected in EXPECTED_ASSETS.items():
        lock = resolve_assets(default_registry(), profile_id)
        assert {a.id for a in lock.assets} == expected, profile_id
        assert lock.profile == profile_id


def test_four_locks_match_committed_samples_and_replay() -> None:
    for profile_id, sample in SAMPLES.items():
        committed = AssetLock.model_validate_json(sample.read_text(encoding="utf-8"))
        assert resolve_assets(default_registry(), profile_id).lock_hash == committed.lock_hash
        verify_lock_hash(committed)


def test_matrix_options_are_deterministic() -> None:
    options = list_profile_options()

    assert len(options) == 4
    recommended = [o for o in options if o["recommended"]]
    assert len(recommended) == 1 and recommended[0]["profile_id"] == "demo-ts-mp"
    assert recommended[0]["profile_version"] == "0.4"
    js_mb = next(o for o in options if o["profile_id"] == "demo-js-mb")
    assert js_mb["frontend_language"] == "javascript" and js_mb["orm"] == "mybatis"
    assert all(o["notes"] for o in options)


def test_fourth_combination_is_supported() -> None:
    assert is_supported_profile(TechnicalProfile(
        profile_id="demo-js-mb", profile_version="0.1", template_version="demo-0.2-js-mb"))
    assert not is_supported_profile(TechnicalProfile(
        profile_id="demo-js-mb", profile_version="0.1", template_version="demo-0.2-mb"))


# --- 四组合各验证一次 Method Unit 兼容/不兼容筛选 -----------------------------

def _query(profile_id: str) -> CapabilityQuery:
    return CapabilityQuery(
        profile=profile_id, stacks=(("java", "spring-boot"),),
        capability_ids=("crud",), tags=("crud",), required_acceptance_ids=(),
    )


def _candidate() -> Candidate:
    return Candidate(
        kind="method-unit", id="crud-method", version="1.0.0", provides=("crud",),
        requires=(), conflicts=(), extension_points=(), compatible_profiles=("demo-ts-mp",),
        license="internal", parameter_schema={}, language="java", framework="spring-boot",
    )


def test_method_unit_compat_filtered_per_combination() -> None:
    for profile_id in EXPECTED_ASSETS:
        result = find_assets(
            _query(profile_id), registry=default_registry(), candidates=[_candidate()],
            methods_root_path=Path("/nonexistent"),
        )
        reasons = result.filter_reasons.get("method-unit:crud-method@1.0.0", [])
        if profile_id == "demo-ts-mp":
            assert "method-unit:crud-method@1.0.0" in result.selected_refs
            assert reasons == []
        else:
            assert "profile_incompatible" in reasons


# --- 选型接口 + 通俗解释（假客户端） ------------------------------------------

class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete(self, *, model, prompt, thinking_mode=None, json_mode=False,
                 max_output_tokens=None, reasoning_effort=None) -> Completion:
        self.calls.append({"model": model, "thinking_mode": thinking_mode})
        return Completion(
            content=json.dumps({"explanation": "该组合前端为 JavaScript，后端为 MyBatis，SQL 更透明。"}, ensure_ascii=False),
            model=model, request_id="rid", usage=Usage(10, 5), finish_reason="stop",
        )


def test_list_profiles_requires_auth(platform_app) -> None:
    app, _ = platform_app
    assert TestClient(app).get("/api/profiles").status_code == 401

    client = _auth_client(app, ALICE)
    body = client.get("/api/profiles").json()["data"]
    assert {o["profile_id"] for o in body} == set(EXPECTED_ASSETS)


def test_profile_explanation_uses_deepseek_flash_and_records_ledger(platform_app, monkeypatch) -> None:
    app, factory = platform_app
    recorder = RecordingClient()
    monkeypatch.setattr(DeepSeekClient, "complete", recorder.complete)
    client = _auth_client(app, ALICE)
    project_id = client.post(
        "/api/projects",
        json={"name": "任务管理", "requirement": REQUIREMENT},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    ).json()["data"]["id"]

    response = client.post(
        f"/api/projects/{project_id}/profile-explanation",
        json={"profile_id": "demo-js-mb"},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["profile_id"] == "demo-js-mb" and data["explanation"]
    assert data["thinking_mode"] == "disabled"
    assert recorder.calls[0]["model"] == "deepseek-flash"
    with session_scope(factory) as session:
        row = session.scalars(select(ModelCall)).first()
        assert row.model_role == "user_interaction" and row.model == "deepseek-flash"


def test_profile_explanation_rejects_unknown_profile(platform_app, monkeypatch) -> None:
    app, _ = platform_app
    recorder = RecordingClient()
    monkeypatch.setattr(DeepSeekClient, "complete", recorder.complete)
    client = _auth_client(app, ALICE)
    project_id = client.post(
        "/api/projects",
        json={"name": "p", "requirement": REQUIREMENT},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    ).json()["data"]["id"]

    response = client.post(
        f"/api/projects/{project_id}/profile-explanation",
        json={"profile_id": "demo-nonexistent"},
        headers={"X-CSRF-Token": client.cookies.get("af_csrf")},
    )

    assert response.status_code == 404
    assert recorder.calls == []  # 未知组合不调用模型


def test_profile_explanation_requires_csrf_and_ownership(platform_app, monkeypatch) -> None:
    app, _ = platform_app
    monkeypatch.setattr(DeepSeekClient, "complete", RecordingClient().complete)
    alice = _auth_client(app, ALICE)
    bob = _auth_client(app, BOB)
    project_id = alice.post(
        "/api/projects",
        json={"name": "p", "requirement": REQUIREMENT},
        headers={"X-CSRF-Token": alice.cookies.get("af_csrf")},
    ).json()["data"]["id"]

    no_csrf = alice.post(
        f"/api/projects/{project_id}/profile-explanation", json={"profile_id": "demo-ts-mp"}
    )
    assert no_csrf.status_code == 403

    cross = bob.post(
        f"/api/projects/{project_id}/profile-explanation",
        json={"profile_id": "demo-ts-mp"},
        headers={"X-CSRF-Token": bob.cookies.get("af_csrf")},
    )
    assert cross.status_code == 403
