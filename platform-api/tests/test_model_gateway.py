"""M21 模型网关计费护栏测试（不触网，使用假客户端）。"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
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

    def complete(
        self, *, model: str, prompt: str, thinking_mode: str | None = None,
        max_output_tokens: int | None = None, reasoning_effort: str | None = None,
    ) -> Completion:
        self.calls.append({
            "model": model, "prompt": prompt, "thinking_mode": thinking_mode,
            "max_output_tokens": max_output_tokens, "reasoning_effort": reasoning_effort,
        })
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


def test_missing_key_releases_budget_reservation(platform_app, monkeypatch) -> None:
    from agentforge_platform.clients import DeepSeekClient, ProviderPreflightError

    _, factory = platform_app
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ProviderPreflightError), session_scope(factory) as session:
        invoke(
            session, call_id="no-key", model_role="user_interaction", client=DeepSeekClient(),
            prompt="JSON", project_id=1, max_input_tokens=100, max_output_tokens=100,
        )
    with session_scope(factory) as session:
        row = session.get(ModelCall, "no-key")
        assert row.status == "NOT_SENT"
        assert row.reserved_micro == 0


def test_price_is_pinned_at_reservation(platform_app, monkeypatch, tmp_path: Path) -> None:
    _, factory = platform_app
    price_file = tmp_path / "prices.json"

    def write_price(rate: int) -> None:
        price_file.write_text(json.dumps({"version": str(rate), "models": {
            "deepseek-flash": {"input_micro_per_million": rate,
                               "output_micro_per_million": rate}
        }}))

    write_price(1_000_000)
    monkeypatch.setenv("AGENTFORGE_PRICE_FILE", str(price_file))

    class RepriceClient(RecordingClient):
        def complete(self, **kwargs):
            write_price(2_000_000)
            return super().complete(**kwargs)

    with session_scope(factory) as session:
        invoke(
            session, call_id="pin", model_role="user_interaction", client=RepriceClient(),
            prompt="x", project_id=1, max_input_tokens=1000, max_output_tokens=1000,
        )
    with session_scope(factory) as session:
        row = session.get(ModelCall, "pin")
        assert row.price_version == "1000000"
        assert row.cost_micro == 1500


def test_concurrent_reservations_obey_budget(platform_app, monkeypatch) -> None:
    import agentforge_platform.model_gateway as gateway

    _, factory = platform_app
    monkeypatch.setenv("AGENTFORGE_PROJECT_BUDGET_MICRO", "1000000")
    started = threading.Event()
    release = threading.Event()
    calls = []
    simultaneous_reads = threading.Barrier(2)
    original_totals = gateway._totals

    def race_totals(db, project_id):
        try:
            simultaneous_reads.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return original_totals(db, project_id)

    monkeypatch.setattr(gateway, "_totals", race_totals)

    class HeldClient(RecordingClient):
        def complete(self, **kwargs):
            calls.append(kwargs)
            started.set()
            release.wait(timeout=5)
            return super().complete(**kwargs)

    def call(call_id: str):
        try:
            with session_scope(factory) as session:
                invoke(
                    session, call_id=call_id, model_role="user_interaction",
                    client=HeldClient(), prompt="x", project_id=1,
                    max_input_tokens=1000, max_output_tokens=1000,
                )
            return "ok"
        except AppException as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(call, "concurrent-1")
        assert started.wait(timeout=5)
        second = pool.submit(call, "concurrent-2")
        assert second.result(timeout=5) == "BUDGET_EXCEEDED"
        release.set()
        assert first.result(timeout=5) == "ok"
    assert len(calls) == 1


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


def test_user_draft_limit_spans_projects(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    monkeypatch.setenv("AGENTFORGE_USER_DRAFT_MAX_CALLS", "1")
    from agentforge_platform.models import Project

    with session_scope(factory) as session:
        first = Project(owner_id=1, name="a", requirement_text="需求")
        second = Project(owner_id=1, name="b", requirement_text="需求")
        session.add_all([first, second])
        session.flush()
        first_id, second_id = first.id, second.id
        reserve(
            session, call_id="user-1", model_role="user_interaction", project_id=first_id,
            max_input_tokens=10, max_output_tokens=10,
        )

    client = RecordingClient()
    with pytest.raises(AppException) as error, session_scope(factory) as session:
        invoke(
            session, call_id="user-2", model_role="user_interaction", client=client,
            prompt="x", project_id=second_id, max_input_tokens=10, max_output_tokens=10,
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
    assert client.calls[0]["max_output_tokens"] == 100
    with session_scope(factory) as session:
        row = session.get(ModelCall, "c1")
        assert row.model == "deepseek-flash" and row.input_tokens == 1000


def test_invoke_persists_reservation_before_network_and_settlement_after_outer_rollback(
    platform_app,
) -> None:
    _, factory = platform_app

    class ObservingClient(RecordingClient):
        def complete(
            self, *, model, prompt, thinking_mode=None, max_output_tokens=None,
            reasoning_effort=None,
        ):
            with session_scope(factory) as observer:
                row = observer.get(ModelCall, "durable-call")
                assert row is not None
                assert row.status == "RESERVED"
            return super().complete(
                model=model, prompt=prompt, thinking_mode=thinking_mode,
                max_output_tokens=max_output_tokens, reasoning_effort=reasoning_effort,
            )

    with pytest.raises(RuntimeError, match="later draft failure"), session_scope(factory) as session:
        invoke(
            session,
            call_id="durable-call",
            model_role="user_interaction",
            client=ObservingClient(),
            prompt="hello",
            project_id=1,
            max_input_tokens=100,
            max_output_tokens=100,
        )
        raise RuntimeError("later draft failure")

    with session_scope(factory) as session:
        row = session.get(ModelCall, "durable-call")
        assert row is not None
        assert row.status == "UNKNOWN"  # 无价格文件，但 usage 仍须持久化
        assert row.input_tokens == 1000


def test_unverified_reasoning_effort_is_blocked(platform_app) -> None:
    _, factory = platform_app
    client = RecordingClient()

    with pytest.raises(AppException) as error, session_scope(factory) as session:
        invoke(
            session, call_id="c-eff", model_role="user_interaction", client=client,
            prompt="x", project_id=1, max_input_tokens=100, max_output_tokens=100,
            thinking_mode="enabled", reasoning_effort="low",
        )

    assert error.value.code == "MODEL_UNAVAILABLE"
    assert client.calls == []  # 未验证强度：未发起外部请求
    with session_scope(factory) as session:
        assert session.get(ModelCall, "c-eff") is None


def test_reasoning_effort_requires_enabled_thinking(platform_app) -> None:
    _, factory = platform_app
    client = RecordingClient()

    with pytest.raises(AppException) as error, session_scope(factory) as session:
        invoke(
            session, call_id="c-bad", model_role="user_interaction", client=client,
            prompt="x", project_id=1, max_input_tokens=100, max_output_tokens=100,
            thinking_mode="disabled", reasoning_effort="low",
        )

    assert error.value.code == "MODEL_UNAVAILABLE"
    assert client.calls == []


def test_verified_reasoning_effort_is_sent_and_recorded(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    monkeypatch.setenv("AGENTFORGE_VERIFIED_REASONING_EFFORTS", "low")
    client = RecordingClient()

    with session_scope(factory) as session:
        invoke(
            session, call_id="c-ok", model_role="user_interaction", client=client,
            prompt="x", project_id=1, max_input_tokens=100, max_output_tokens=100,
            thinking_mode="enabled", reasoning_effort="low",
        )

    assert client.calls[0]["thinking_mode"] == "enabled"
    assert client.calls[0]["reasoning_effort"] == "low"
    with session_scope(factory) as session:
        row = session.get(ModelCall, "c-ok")
        assert row.thinking_mode == "enabled" and row.reasoning_effort == "low"


def test_default_call_records_null_reasoning_effort(platform_app) -> None:
    _, factory = platform_app
    client = RecordingClient()

    with session_scope(factory) as session:
        invoke(
            session, call_id="c-null", model_role="user_interaction", client=client,
            prompt="x", project_id=1, max_input_tokens=100, max_output_tokens=100,
            thinking_mode="disabled",
        )

    with session_scope(factory) as session:
        assert session.get(ModelCall, "c-null").reasoning_effort is None
