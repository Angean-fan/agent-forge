"""回归测试：P0/P1/P2 平台实机就绪性修复（PG 访谈检查点、自定义 user_interaction 路由、多实体提取、worker 续租、compose 配置挂载、PatchEngine.model）。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from agentforge_platform.asset_registry import REPO_ROOT
from agentforge_platform.clients import Completion, DeepSeekClient, OpenAICompatibleClient, Usage
from agentforge_platform.db import session_scope
from agentforge_platform.factory_pipeline import FACTORY_PIPELINE_ENV
from agentforge_platform.interview_graph import _sqlite_path
from agentforge_platform.models import Project, Run, SpecVersion, Worker
from agentforge_platform.production_routing import default_user_interaction_client
from agentforge_platform.requirement_extraction import extract_requirement
from agentforge_platform.runs import create_run, renew_run_lease
from agentforge_platform.spec_service import load_spec
from agentforge_platform.worker import run_worker


def test_interview_checkpoint_supports_postgres_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix 1：PostgreSQL 平台库 URL 不再触发 InterviewError，而是落到独立本地检查点 SQLite 文件。"""
    pg_url = "postgresql+psycopg://user:pass@127.0.0.1:15432/agentforge"
    explicit = tmp_path / "cp" / "custom.sqlite"
    monkeypatch.setenv("AGENTFORGE_INTERVIEW_CHECKPOINT_DB", str(explicit))
    assert _sqlite_path(pg_url) == str(explicit)
    assert explicit.parent.is_dir()

    monkeypatch.delenv("AGENTFORGE_INTERVIEW_CHECKPOINT_DB", raising=False)
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    auto_path = Path(_sqlite_path(pg_url))
    assert auto_path.parent == tmp_path
    assert auto_path.name.startswith("interview-checkpoints-")
    assert auto_path.suffix == ".sqlite"


def test_user_interaction_respects_custom_models_config_and_omits_thinking_disabled_for_openai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix 2：user_interaction 优先走 A04 models.json，且非 DeepSeek 端点不发 thinking=disabled。"""
    monkeypatch.delenv("AGENTFORGE_MODELS_CONFIG", raising=False)
    assert isinstance(default_user_interaction_client(), DeepSeekClient)

    cfg = {
        "version": "custom-ui-v1",
        "currency": "CNY",
        "roles": {
            "user_interaction": {
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4.1-mini",
                "api_key_env": "TEST_CUSTOM_UI_KEY",
                "enabled": True,
                "max_output_tokens": 4096,
                "supports_tools": True,
            }
        },
    }
    cfg_path = tmp_path / "models.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("AGENTFORGE_MODELS_CONFIG", str(cfg_path))
    monkeypatch.setenv("TEST_CUSTOM_UI_KEY", "sk-test-dummy-key")

    client = default_user_interaction_client()
    assert isinstance(client, OpenAICompatibleClient)
    assert not isinstance(client, DeepSeekClient)
    assert client.provider == "openai"
    assert client.base_url == "https://api.openai.com/v1"

    captured_payloads: list[dict[str, Any]] = []

    class _FakeResp:
        def __init__(self) -> None:
            self.headers = {"x-request-id": "req-1"}

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "id": "resp-1",
                    "model": "gpt-4.1-mini",
                    "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            ).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        captured_payloads.append(json.loads(req.data.decode("utf-8")))
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client.complete(model="gpt-4.1-mini", prompt="hello", thinking_mode="disabled", json_mode=True)
    assert "thinking" not in captured_payloads[0]

    # DeepSeek 仍保持透传 thinking={"type": "disabled"}（零回归）
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-dummy")
    DeepSeekClient().complete(
        model="deepseek-chat", prompt="hello", thinking_mode="disabled", json_mode=True
    )
    assert captured_payloads[1]["thinking"] == {"type": "disabled"}


def test_factory_mode_requirement_extraction_supports_multi_entity_ecommerce(
    platform_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix 3：工厂开关开启时，需求提取支持 5 实体电商规格 + 页面，自动补齐 id 主键并全标 pending。"""
    _app, factory = platform_app
    monkeypatch.setenv(FACTORY_PIPELINE_ENV, "1")
    with session_scope(factory) as db_session:
        project = Project(
            owner_id=1,
            name="电商沙箱",
            requirement_text="登录注册、首页商品展示、购物车、订单与本地模拟支付",
        )
        db_session.add(project)
        db_session.commit()
        db_session.refresh(project)

        fake_payload = {
            "summary": "5 实体电商模拟沙箱草稿",
            "missing_fields": ["订单超时时间"],
            "questions": ["未支付订单多久自动取消？"],
            "goal": "提供登录注册、商品展示、购物车、下单和本地模拟支付能力",
            "actors": ["ADMIN", "USER"],
            "entities": [
                {
                    "name": "UserAccount",
                    "source": "user_confirmed",
                    "fields": [{"name": "username", "type": "string", "required": True}],
                },
                {
                    "name": "Product",
                    "source": "pending",
                    "fields": [
                        {"name": "id", "type": "long", "required": True},
                        {"name": "title", "type": "string", "required": True},
                        {"name": "price", "type": "decimal", "required": True},
                    ],
                },
                {
                    "name": "CartItem",
                    "source": "pending",
                    "fields": [{"name": "quantity", "type": "int", "required": True}],
                },
                {
                    "name": "PurchaseOrder",
                    "source": "pending",
                    "fields": [{"name": "status", "type": "string", "required": True}],
                },
                {
                    "name": "PaymentRecord",
                    "source": "pending",
                    "fields": [{"name": "amount", "type": "decimal", "required": True}],
                },
            ],
            "pages": [
                {"name": "ProductList", "entity": "Product", "path": "/products"},
                {"name": "CartPage", "entity": "CartItem", "path": "/cart"},
            ],
            "permissions": [
                {"role": "ADMIN", "actions": ["create", "read", "update", "delete"], "source": "user_confirmed"},
                {"role": "USER", "actions": ["create", "read"], "source": "pending"},
            ],
            "business_rules": [{"id": "BR01", "text": "库存不足禁止下单", "source": "user_confirmed"}],
            "acceptance_cases": [
                {"id": "AC01", "scenario": "沙箱支付", "expected": "订单状态变为已支付", "source": "user_confirmed"}
            ],
            "constraints": ["支付仅走本地沙箱模拟"],
            "exclusions": ["不接真实银行卡扣款"],
        }

        class _FakeExtractorClient:
            def complete(self, **_kwargs: Any) -> Completion:
                return Completion(
                    content=json.dumps(fake_payload, ensure_ascii=False),
                    model="deepseek-chat",
                    request_id="req-ext-1",
                    usage=Usage(200, 300),
                    finish_reason="stop",
                )

        row = extract_requirement(db_session, project, expected_version=0, client=_FakeExtractorClient())
        spec = load_spec(row)
        assert len(spec.entities) == 5
        assert [e.name for e in spec.entities] == [
            "UserAccount",
            "Product",
            "CartItem",
            "PurchaseOrder",
            "PaymentRecord",
        ]
        assert len(spec.pages) == 2
        # 自动补齐缺失的 id 主键，且所有提取项强制标为 pending（模型不可替用户确认）
        for ent in spec.entities:
            assert any(f.name == "id" and f.type == "long" and f.required for f in ent.fields)
            assert ent.source.value == "pending"
        assert all(p.source.value == "pending" for p in spec.permissions)


def test_worker_renews_lease_and_heartbeat_during_long_running_handler(
    platform_app,
) -> None:
    """Fix 4：长耗时运行期间，worker 后台守护线程持续续租 lease_until 并刷新 heartbeat。"""
    _app, session_factory = platform_app
    with session_scope(session_factory) as db:
        project = Project(owner_id=1, name="长任务测试", requirement_text="测试续租")
        db.add(project)
        db.flush()
        spec_row = SpecVersion(project_id=project.id, version=1, body_json="{}", hash="h" * 64)
        db.add(spec_row)
        db.flush()
        create_run(db, project, spec_row.id, idempotency_key="lease-keep-1")

    observed_leases: list[Any] = []
    observed_heartbeats: list[Any] = []

    def slow_handler(rid: int) -> None:
        with session_scope(session_factory) as db:
            r0 = db.get(Run, rid)
            w0 = db.get(Worker, "worker-lease-test")
            assert r0 is not None and w0 is not None
            observed_leases.append(r0.lease_until)
            observed_heartbeats.append(w0.last_heartbeat)
        time.sleep(0.55)
        with session_scope(session_factory) as db:
            r1 = db.get(Run, rid)
            w1 = db.get(Worker, "worker-lease-test")
            assert r1 is not None and w1 is not None
            observed_leases.append(r1.lease_until)
            observed_heartbeats.append(w1.last_heartbeat)
            assert renew_run_lease(db, rid, "wrong-worker", lease_seconds=60) is False

    run_worker(
        session_factory=session_factory,
        handler=slow_handler,
        worker_id="worker-lease-test",
        iterations=1,
        poll_interval=0.01,
        lease_seconds=1,
        heartbeat_interval=0.15,
    )

    assert len(observed_leases) == 2
    assert observed_leases[1] > observed_leases[0]
    assert observed_heartbeats[1] > observed_heartbeats[0]


def test_compose_mounts_config_on_worker() -> None:
    """Fix 5：deploy/compose.yaml 的 worker 服务必须挂载 ../platform-api/config:/config:ro。"""
    compose_text = (REPO_ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    worker_block = compose_text.split("  worker:\n", 1)[1].split("  maintenance:\n", 1)[0]
    assert "../platform-api/config:/config:ro" in worker_block


def test_patch_engine_stores_resolved_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix 7：build_patch_engine 默认构造模型时应把 resolved 写入 PatchEngine.model（不为 None）。"""
    from agentforge_platform import patch_engine
    from agentforge_platform.agent_files import FileBackend

    class _DummyModel:
        pass

    dummy = _DummyModel()
    monkeypatch.setattr(patch_engine, "build_coding_model", lambda **_kw: dummy)
    monkeypatch.setattr(patch_engine, "register_restricted_harness", lambda _m: "openai:dummy")
    monkeypatch.setattr(patch_engine, "create_deep_agent", lambda **_kw: object())
    monkeypatch.setattr(patch_engine, "assert_restricted_tools", lambda *_a, **_kw: set())

    binding = patch_engine.LedgerBinding(
        session_factory=None,  # type: ignore[arg-type]
        project_id=1,
        run_id=1,
        task_id="t1",
    )
    engine = patch_engine.build_patch_engine(
        ledger_binding=binding,
        file_backend=FileBackend(tmp_path, ["backend/"]),
        system_prompt="sys",
        model=None,
    )
    assert engine.model is dummy
