"""模型配置接口（/api/models/config）测试：脱敏读取、受控写入与 A04 安全红线。"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ALICE
from fastapi.testclient import TestClient

from agentforge_platform.model_gateway import resolve_route


def _login(client: TestClient) -> str:
    resp = client.post(
        "/api/auth/login",
        json={"username": ALICE["username"], "password": ALICE["password"]},
    )
    assert resp.status_code == 200
    csrf = resp.cookies.get("af_csrf")
    assert csrf
    return csrf


def test_get_model_config_requires_auth(platform_app):
    app, _ = platform_app
    with TestClient(app) as client:
        resp = client.get("/api/models/config")
        assert resp.status_code == 401


def test_get_and_update_model_config_roundtrip(
    platform_app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app, _ = platform_app
    cfg_path = tmp_path / "models.custom.json"
    monkeypatch.setenv("AGENTFORGE_MODELS_CONFIG", str(cfg_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-demo-secret-not-exposed")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    with TestClient(app) as client:
        csrf = _login(client)

        get_resp = client.get("/api/models/config")
        assert get_resp.status_code == 200
        body = get_resp.json()["data"]
        roles = {r["role"]: r for r in body["roles"]}
        assert "user_interaction" in roles
        assert "dept_coder" in roles
        assert roles["user_interaction"]["api_key_present"] is True
        assert roles["dept_coder"]["api_key_present"] is False
        # 绝不暴露环境变量密钥值
        assert "ds-demo-secret-not-exposed" not in get_resp.text

        put_resp = client.put(
            "/api/models/config",
            headers={"X-CSRF-Token": csrf},
            json={
                "roles": [
                    {
                        "role": "dept_coder",
                        "provider": "deepseek_official",
                        "model": "deepseek-chat",
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key_env": "DEEPSEEK_API_KEY",
                        "enabled": True,
                        "supports_tools": True,
                        "max_output_tokens": 8192,
                        "timeout_seconds": 180,
                    }
                ]
            },
        )
        assert put_resp.status_code == 200
        updated = {r["role"]: r for r in put_resp.json()["data"]["roles"]}
        assert updated["dept_coder"]["model"] == "deepseek-chat"
        assert updated["dept_coder"]["base_url_host"] == "api.deepseek.com"
        assert updated["dept_coder"]["api_key_present"] is True
        assert updated["dept_coder"]["source"] == "custom"
        assert cfg_path.is_file()

        resolved = resolve_route("dept_coder")
        assert resolved.model == "deepseek-chat"
        assert resolved.provider == "deepseek_official"


def test_update_model_config_rejects_raw_secret_and_no_tools(
    platform_app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app, _ = platform_app
    cfg_path = tmp_path / "models.reject.json"
    monkeypatch.setenv("AGENTFORGE_MODELS_CONFIG", str(cfg_path))

    with TestClient(app) as client:
        csrf = _login(client)

        # 禁止直接写明文密钥到 api_key_env
        resp_secret = client.put(
            "/api/models/config",
            headers={"X-CSRF-Token": csrf},
            json={
                "roles": [
                    {
                        "role": "user_interaction",
                        "provider": "deepseek_official",
                        "model": "deepseek-flash",
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key_env": "sk-1234567890abcdef",
                        "enabled": True,
                        "supports_tools": True,
                    }
                ]
            },
        )
        assert resp_secret.status_code == 422
        assert not cfg_path.exists()

        # 编码角色禁止 supports_tools=False
        resp_tools = client.put(
            "/api/models/config",
            headers={"X-CSRF-Token": csrf},
            json={
                "roles": [
                    {
                        "role": "dept_coder",
                        "provider": "deepseek_official",
                        "model": "deepseek-reasoner",
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key_env": "DEEPSEEK_API_KEY",
                        "enabled": True,
                        "supports_tools": False,
                    }
                ]
            },
        )
        assert resp_tools.status_code == 422
        assert not cfg_path.exists()


def test_custom_model_presets_crud_and_secret_guard(
    platform_app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app, _ = platform_app
    cfg_path = tmp_path / "models.presets.json"
    monkeypatch.setenv("AGENTFORGE_MODELS_CONFIG", str(cfg_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-secret-never-return")
    monkeypatch.setenv("QWEN_API_KEY", "qwen-secret-never-return")

    with TestClient(app) as client:
        csrf = _login(client)

        # 拒绝在预设中直写明文密钥
        bad_resp = client.post(
            "/api/models/presets",
            headers={"X-CSRF-Token": csrf},
            json={
                "name": "Bad Secret Model",
                "provider": "openai_compatible",
                "model": "qwen3-coder-plus",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "api_key_env": "sk-plaintext-secret-99999",
                "supports_tools": True,
            },
        )
        assert bad_resp.status_code == 422

        # 正常添加一个自定义模型到模型库
        add_resp = client.post(
            "/api/models/presets",
            headers={"X-CSRF-Token": csrf},
            json={
                "name": "Qwen3 Coder",
                "provider": "openai_compatible",
                "model": "qwen3-coder-plus",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "api_key_env": "QWEN_API_KEY",
                "supports_tools": True,
                "max_output_tokens": 16384,
                "timeout_seconds": 240,
            },
        )
        assert add_resp.status_code == 200
        assert "qwen-secret-never-return" not in add_resp.text
        presets = add_resp.json()["data"]["custom_models"]
        added = next(p for p in presets if p["model"] == "qwen3-coder-plus")
        assert added["name"] == "Qwen3 Coder"
        assert added["api_key_present"] is True
        assert added["removable"] is True
        preset_id = added["id"]

        # 为角色切换该自定义模型后，custom_models 依然持久化保留
        put_resp = client.put(
            "/api/models/config",
            headers={"X-CSRF-Token": csrf},
            json={
                "roles": [
                    {
                        "role": "dept_coder",
                        "provider": added["provider"],
                        "model": added["model"],
                        "base_url": added["base_url"],
                        "api_key_env": added["api_key_env"],
                        "enabled": True,
                        "supports_tools": True,
                        "max_output_tokens": 16384,
                        "timeout_seconds": 240,
                    }
                ]
            },
        )
        assert put_resp.status_code == 200
        assert any(p["id"] == preset_id for p in put_resp.json()["data"]["custom_models"])
        assert resolve_route("dept_coder").model == "qwen3-coder-plus"

        # 删除自定义预设成功且不影响已保存的角色配置
        del_resp = client.delete(
            f"/api/models/presets/{preset_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert del_resp.status_code == 200
        assert not any(
            p["id"] == preset_id and p["removable"] for p in del_resp.json()["data"]["custom_models"]
        )
        assert resolve_route("dept_coder").model == "qwen3-coder-plus"
