"""A04：用户可自定义模型路由与多 Provider（OpenAI 兼容）网关适配测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

from agentforge_platform.clients import (
    Completion,
    OpenAICompatibleClient,
    ProviderPreflightError,
    Usage,
)
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.model_gateway import (
    FACTORY_ROLES,
    ROUTES,
    UNKNOWN,
    invoke,
    load_custom_routes,
    resolve_route,
    role_enabled,
)
from agentforge_platform.models import ModelCall, Project
from agentforge_platform.patch_engine import LedgerBinding
from agentforge_platform.production_routing import (
    build_client_for_role,
    build_ledger_model,
    build_production_routing,
    role_status,
)

PLATFORM_API_DIR = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = PLATFORM_API_DIR / "config" / "models.example.json"


def _write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "models.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_default_fallback_and_factory_role_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTFORGE_MODELS_CONFIG", raising=False)
    for role in ("technical_planning", "coding", "complex_repair", *FACTORY_ROLES):
        monkeypatch.delenv(f"AGENTFORGE_{role.upper()}_ENABLED", raising=False)

    assert load_custom_routes() == {}
    ui_route = resolve_route("user_interaction")
    assert ui_route.provider == "deepseek_official"
    assert ui_route.model == "deepseek-flash"

    # 默认未开启时，工厂角色与原角色均禁用
    for role in ("technical_planning", "coding", *FACTORY_ROLES):
        assert role_enabled(role) is False
        with pytest.raises(AppException) as exc_info:
            resolve_route(role)
        assert exc_info.value.code == ErrorCode.MODEL_ROLE_DISABLED

    # 开启 technical_planning 与 coding 后，工厂角色自动回退映射到对应基础角色
    monkeypatch.setenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "1")
    monkeypatch.setenv("AGENTFORGE_CODING_ENABLED", "1")
    assert resolve_route("chief_architect").model == ROUTES["technical_planning"].model
    assert resolve_route("dept_lead").model == ROUTES["technical_planning"].model
    assert resolve_route("dept_coder").model == ROUTES["coding"].model
    assert resolve_route("dept_tester").model == ROUTES["coding"].model


def test_example_config_loads_and_resolves_multi_provider_clients_and_ledger_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert EXAMPLE_CONFIG.is_file()
    routes = load_custom_routes(EXAMPLE_CONFIG)
    assert {
        "user_interaction",
        "technical_planning",
        "coding",
        "complex_repair",
        "chief_architect",
        "dept_lead",
        "dept_coder",
        "dept_tester",
    } <= set(routes)
    providers = {r.provider for r in routes.values()}
    assert {"deepseek_official", "dashscope", "openai", "ollama_local"} <= providers

    monkeypatch.setenv("AGENTFORGE_MODELS_CONFIG", str(EXAMPLE_CONFIG))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-ds-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dummy-qwen-key")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-oa-key")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-local")

    for role in (*ROUTES, *FACTORY_ROLES):
        client = build_client_for_role(role)
        assert isinstance(client, OpenAICompatibleClient)
        assert client.provider == routes[role].provider
        assert client.base_url == routes[role].base_url

        binding = LedgerBinding(
            session_factory=None,  # type: ignore[arg-type]
            project_id=1,
            run_id=1,
            task_id="t-a04",
            model_role=role,
        )
        ledger_model = build_ledger_model(role, binding)
        assert ledger_model.model_name == routes[role].model

    prod = build_production_routing(session_factory=None, work_root="/tmp/a04")  # type: ignore[arg-type]
    assert isinstance(prod.planning_client, OpenAICompatibleClient)
    assert prod.any_enabled() is True


def test_invalid_configs_and_direct_secret_injection_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. 配置文件路径不存在 -> CONFIG_REQUIRED
    with pytest.raises(AppException) as exc_info:
        load_custom_routes(tmp_path / "nonexistent.json")
    assert exc_info.value.code == ErrorCode.CONFIG_REQUIRED

    # 2. 直接写入 api_key 字段 -> VALIDATION_ERROR
    cfg_direct_field = _write_config(
        tmp_path,
        {
            "roles": {
                "coding": {
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4.1",
                    "api_key_env": "OPENAI_API_KEY",
                    "api_key": "sk-direct-secret-value",
                }
            }
        },
    )
    with pytest.raises(AppException) as exc_info:
        load_custom_routes(cfg_direct_field)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    # 3. 在 api_key_env 中直接写入疑似密钥明文（如 sk-...）而非环境变量名 -> VALIDATION_ERROR
    cfg_raw_secret = _write_config(
        tmp_path,
        {
            "roles": {
                "coding": {
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4.1",
                    "api_key_env": "sk-proj-1234567890abcdef",
                }
            }
        },
    )
    with pytest.raises(AppException) as exc_info:
        load_custom_routes(cfg_raw_secret)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    # 4. 缺少 base_url -> CONFIG_REQUIRED；无效 URL -> VALIDATION_ERROR
    cfg_no_url = _write_config(
        tmp_path,
        {
            "roles": {
                "dept_lead": {
                    "provider": "openai",
                    "model": "gpt-4.1",
                    "api_key_env": "OPENAI_API_KEY",
                }
            }
        },
    )
    with pytest.raises(AppException) as exc_info:
        load_custom_routes(cfg_no_url)
    assert exc_info.value.code == ErrorCode.CONFIG_REQUIRED

    cfg_bad_url = _write_config(
        tmp_path,
        {
            "roles": {
                "dept_lead": {
                    "provider": "openai",
                    "base_url": "ftp://invalid-endpoint",
                    "model": "gpt-4.1",
                    "api_key_env": "OPENAI_API_KEY",
                }
            }
        },
    )
    with pytest.raises(AppException) as exc_info:
        load_custom_routes(cfg_bad_url)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    # 5. 编码/测试角色声明 supports_tools=false -> VALIDATION_ERROR
    for tool_role in ("coding", "complex_repair", "dept_coder", "dept_tester"):
        cfg_no_tools = _write_config(
            tmp_path,
            {
                "roles": {
                    tool_role: {
                        "provider": "ollama_local",
                        "base_url": "http://127.0.0.1:11434/v1",
                        "model": "llama3",
                        "api_key_env": "OLLAMA_API_KEY",
                        "supports_tools": False,
                    }
                }
            },
        )
        with pytest.raises(AppException) as exc_info:
            load_custom_routes(cfg_no_tools)
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    # 6. 缺环境变量 Key 时，role_status 与构造器明确报告 CONFIG_REQUIRED
    monkeypatch.setenv("AGENTFORGE_MODELS_CONFIG", str(EXAMPLE_CONFIG))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    st = role_status("chief_architect")
    assert st.enabled is False
    assert st.reason == ErrorCode.CONFIG_REQUIRED.value

    with pytest.raises(AppException) as exc_info:
        build_client_for_role("chief_architect")
    assert exc_info.value.code == ErrorCode.CONFIG_REQUIRED

    with pytest.raises(AppException) as exc_info:
        build_ledger_model(
            "chief_architect",
            LedgerBinding(
                session_factory=None,  # type: ignore[arg-type]
                project_id=1,
                run_id=1,
                task_id="t1",
            ),
        )
    assert exc_info.value.code == ErrorCode.CONFIG_REQUIRED

    # OpenAICompatibleClient 调用时若环境变量被清除，前置抛 ProviderPreflightError
    raw_client = OpenAICompatibleClient(
        provider="openai",
        api_key_env="OPENAI_API_KEY",
        base_url="https://api.openai.com/v1",
    )
    with pytest.raises(ProviderPreflightError):
        raw_client.complete(model="gpt-4.1", prompt="ping")


def test_ledger_records_custom_provider_settled_and_unknown_without_zero_fake(
    platform_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = platform_app
    monkeypatch.setenv("AGENTFORGE_MODELS_CONFIG", str(EXAMPLE_CONFIG))
    monkeypatch.delenv("AGENTFORGE_PRICE_FILE", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-ds")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-oa")
    monkeypatch.setenv("OLLAMA_API_KEY", "dummy-ollama")

    with session_scope(factory) as session:
        proj = Project(owner_id=1, name="a04-ledger", requirement_text="测试自定义路由账本")
        session.add(proj)
        session.flush()
        project_id = proj.id

    class _FakeClient:
        def complete(
            self,
            *,
            model: str,
            prompt: str,
            thinking_mode: str | None = None,
            json_mode: bool = False,
            max_output_tokens: int | None = None,
            reasoning_effort: str | None = None,
        ) -> Completion:
            _ = (prompt, thinking_mode, json_mode, max_output_tokens, reasoning_effort)
            return Completion(
                content='{"ok": true}',
                model=model,
                request_id="req-custom-1",
                usage=Usage(input_tokens=1000, output_tokens=500),
            )

    # 1. 配置了 price_micro 的自定义角色（user_interaction -> deepseek-chat: 1e6/2e6）-> 精确 SETTLED
    with session_scope(factory) as session:
        invoke(
            session,
            call_id="call-priced-custom",
            model_role="user_interaction",
            client=_FakeClient(),
            prompt="hello",
            project_id=project_id,
            max_input_tokens=2000,
            max_output_tokens=1000,
        )

    # 2. 未配置 price_micro 的自定义角色（chief_architect -> openai/gpt-4.1）-> 记 UNKNOWN，绝不伪算为 0
    with session_scope(factory) as session:
        invoke(
            session,
            call_id="call-unpriced-custom",
            model_role="chief_architect",
            client=_FakeClient(),
            prompt="blueprint",
            project_id=project_id,
            max_input_tokens=2000,
            max_output_tokens=1000,
        )

    # 3. 工厂编码角色（dept_tester -> ollama_local/qwen2.5-coder:14b）经 LedgerChatModel 内部调用入账
    def _fake_generate(_self, _messages, stop=None, run_manager=None, **_kwargs):
        _ = (stop, run_manager)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="ok"))],
            llm_output={"token_usage": {"prompt_tokens": 200, "completion_tokens": 100}},
        )

    monkeypatch.setattr(ChatOpenAI, "_generate", _fake_generate)
    tester_model = build_ledger_model(
        "dept_tester",
        LedgerBinding(
            session_factory=factory,
            project_id=project_id,
            run_id=None,  # type: ignore[arg-type]
            task_id=None,  # type: ignore[arg-type]
            model_role="dept_tester",
        ),
    )
    tester_model._generate([AIMessage(content="test")])

    with session_scope(factory) as session:
        priced = session.get(ModelCall, "call-priced-custom")
        assert priced is not None
        assert priced.provider == "deepseek_official"
        assert priced.model == "deepseek-chat"
        assert priced.price_version == "a04-example-1"
        assert priced.status == "SETTLED"
        assert priced.cost_micro == 2000  # 1000*1 + 500*2

        unpriced = session.get(ModelCall, "call-unpriced-custom")
        assert unpriced is not None
        assert unpriced.provider == "openai"
        assert unpriced.model == "gpt-4.1"
        assert unpriced.price_version == UNKNOWN
        assert unpriced.status == UNKNOWN
        assert unpriced.cost_micro is None
        assert unpriced.reserved_micro > 0

        tester_rows = (
            session.query(ModelCall)
            .filter(ModelCall.model_role == "dept_tester")
            .all()
        )
        assert len(tester_rows) == 1
        assert tester_rows[0].provider == "ollama_local"
        assert tester_rows[0].model == "qwen2.5-coder:14b"
        assert tester_rows[0].status == UNKNOWN
        assert tester_rows[0].cost_micro is None
