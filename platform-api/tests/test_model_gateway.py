"""M21 模型网关计费护栏测试（不触网，使用假客户端）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentforge_platform.clients import Completion, Usage
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException
from agentforge_platform.model_gateway import (
    invoke,
    reserve,
    resolve_route,
    settle,
)
from agentforge_platform.models import ModelCall

_DEFAULT_USAGE = Usage(1000, 500)


class RecordingClient:
    def __init__(self, *, usage: Usage | None = _DEFAULT_USAGE, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self._usage = usage
        self._fail = fail

    def complete(self, *, model: str, prompt: str, thinking_mode: str | None = None) -> Completion:
        self.calls.append({"model": model, "prompt": prompt, "thinking_mode": thinking_mode})
        if self._fail:
            raise RuntimeError("provider unavailable")
        return Completion(content="ok", model=model, request_id="rid-1", usage=self._usage)


def test_user_interaction_routes_to_deepseek_flash() -> None:
    route = resolve_route("user_interaction")

    assert route.provider == "deepseek_official"
    assert route.model == "deepseek-flash"


@pytest.mark.parametrize("role", ["technical_planning", "coding", "complex_repair", "unknown"])
def test_disabled_roles_are_rejected(role: str) -> None:
    with pytest.raises(AppException) as error:
        resolve_route(role)
    assert error.value.code == "MODEL_ROLE_DISABLED"


def test_disabled_role_makes_no_external_call(platform_app) -> None:
    _, factory = platform_app
    client = RecordingClient()

    with pytest.raises(AppException), session_scope(factory) as session:
        invoke(
            session,
            call_id="c1",
            model_role="coding",
            client=client,
            prompt="x",
            project_id=1,
            max_input_tokens=1000,
            max_output_tokens=1000,
        )

    assert client.calls == []


def test_settlement_is_idempotent(platform_app, monkeypatch, tmp_path: Path) -> None:
    _, factory = platform_app
    price_file = tmp_path / "prices.json"
    price_file.write_text(
        json.dumps(
            {
                "version": "2026-10-11",
                "models": {
                    "deepseek-flash": {
                        "input_micro_per_million": 1_000_000,
                        "output_micro_per_million": 2_000_000,
                    }
                },
            }
        )
    )
    monkeypatch.setenv("AGENTFORGE_PRICE_FILE", str(price_file))

    with session_scope(factory) as session:
        reserve(
            session,
            call_id="c1",
            model_role="user_interaction",
            project_id=1,
            draft_id=5,
            max_input_tokens=1000,
            max_output_tokens=1000,
        )
        first = settle(session, "c1", usage=Usage(1000, 500), request_id="r1")
        assert first.status == "SETTLED"
        assert first.cost_micro == 2000  # 1000*1 + 500*2 微元
        second = settle(session, "c1", usage=Usage(1000, 500), request_id="r1")
        assert second.cost_micro == 2000

    with session_scope(factory) as session:
        assert session.get(ModelCall, "c1").cost_micro == 2000


def test_missing_usage_keeps_reservation_unknown(platform_app) -> None:
    _, factory = platform_app

    with session_scope(factory) as session:
        reserve(
            session,
            call_id="c1",
            model_role="user_interaction",
            project_id=1,
            draft_id=5,
            max_input_tokens=1000,
            max_output_tokens=1000,
        )
        row = settle(session, "c1", usage=None, error_class="RuntimeError")

    assert row.status == "UNKNOWN"
    assert row.cost_micro is None
    assert row.reserved_micro > 0  # 保留预留，等待对账


def test_draft_usage_is_recorded_before_run(platform_app) -> None:
    _, factory = platform_app

    with session_scope(factory) as session:
        reserve(
            session,
            call_id="d1",
            model_role="user_interaction",
            project_id=42,
            draft_id=9,
            max_input_tokens=1000,
            max_output_tokens=1000,
        )

    with session_scope(factory) as session:
        row = session.get(ModelCall, "d1")
        assert row.project_id == 42 and row.draft_id == 9 and row.run_id is None


def test_budget_exceeded_blocks_call(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    monkeypatch.setenv("AGENTFORGE_PROJECT_BUDGET_MICRO", "1")
    client = RecordingClient()

    with pytest.raises(AppException) as error, session_scope(factory) as session:
        invoke(
            session,
            call_id="c1",
            model_role="user_interaction",
            client=client,
            prompt="x",
            project_id=1,
            max_input_tokens=1000,
            max_output_tokens=1000,
        )

    assert error.value.code == "BUDGET_EXCEEDED"
    assert client.calls == []


def test_draft_call_limit_blocks_call(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    monkeypatch.setenv("AGENTFORGE_DRAFT_MAX_CALLS", "1")

    with session_scope(factory) as session:
        reserve(
            session,
            call_id="c1",
            model_role="user_interaction",
            project_id=1,
            draft_id=1,
            max_input_tokens=10,
            max_output_tokens=10,
        )

    client = RecordingClient()
    with pytest.raises(AppException) as error, session_scope(factory) as session:
        invoke(
            session,
            call_id="c2",
            model_role="user_interaction",
            client=client,
            prompt="x",
            project_id=1,
            draft_id=1,
            max_input_tokens=10,
            max_output_tokens=10,
        )

    assert error.value.code == "DRAFT_LIMIT_EXCEEDED"
    assert client.calls == []


def test_provider_failure_settles_unknown(platform_app) -> None:
    _, factory = platform_app
    client = RecordingClient(fail=True)

    with pytest.raises(RuntimeError), session_scope(factory) as session:
        invoke(
            session,
            call_id="c1",
            model_role="user_interaction",
            client=client,
            prompt="x",
            project_id=1,
            max_input_tokens=10,
            max_output_tokens=10,
        )

    with session_scope(factory) as session:
        row = session.get(ModelCall, "c1")
        assert row.status == "UNKNOWN"
        assert row.error_class == "RuntimeError"


def test_invoke_records_usage_and_model(platform_app) -> None:
    _, factory = platform_app
    client = RecordingClient()

    with session_scope(factory) as session:
        result = invoke(
            session,
            call_id="c1",
            model_role="user_interaction",
            client=client,
            prompt="hello",
            project_id=1,
            max_input_tokens=100,
            max_output_tokens=100,
        )

    assert result.content == "ok"
    assert client.calls[0]["model"] == "deepseek-flash"
    with session_scope(factory) as session:
        row = session.get(ModelCall, "c1")
        assert row.model == "deepseek-flash" and row.input_tokens == 1000
