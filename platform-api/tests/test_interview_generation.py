"""R03 动态澄清问题生成测试（不触网，使用脚本化假客户端）。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from agentforge_platform.clients import Completion, Usage
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException
from agentforge_platform.interview_generation import (
    AnsweredDecision,
    detect_conflicts,
    generate_questions,
    select_thinking_mode,
)
from agentforge_platform.models import ModelCall, Project, SpecVersion


def _payload(questions: list[dict], unresolved: list[str] | None = None) -> str:
    body = {
        "unresolved_fields": unresolved or ["报名规则"],
        "questions": questions,
    }
    return json.dumps(body, ensure_ascii=False)


def _valid_choice(affects: list[str], question_id: str = "q1") -> dict:
    return {
        "question_id": question_id,
        "type": "single_choice",
        "prompt": "成员能否重复报名同一个活动？",
        "options": ["可以", "不可以"],
        "allow_custom": False,
        "required": True,
        "affects": affects,
    }


class ScriptedClient:
    def __init__(self, contents: list[str]) -> None:
        self._contents = list(contents)
        self._index = 0
        self.calls: list[dict] = []

    def complete(
        self, *, model, prompt, thinking_mode=None, json_mode=False,
        max_output_tokens=None, reasoning_effort=None,
    ) -> Completion:
        self.calls.append({
            "model": model, "prompt": prompt, "thinking_mode": thinking_mode,
            "reasoning_effort": reasoning_effort,
        })
        content = self._contents[min(self._index, len(self._contents) - 1)]
        self._index += 1
        return Completion(
            content=content, model=model, request_id="rid", usage=Usage(10, 5),
            finish_reason="stop",
        )


def _seed(factory) -> tuple[int, int]:
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="校园活动报名系统")
        session.add(project)
        session.flush()
        draft = SpecVersion(project_id=project.id, version=1, body_json="{}", hash="x")
        session.add(draft)
        session.flush()
        return project.id, draft.id


def _generate(factory, client, *, decisions=None, missing=None):
    project_id, draft_id = _seed(factory)
    with session_scope(factory) as session:
        return generate_questions(
            session,
            session.get(Project, project_id),
            session.get(SpecVersion, draft_id),
            missing_fields=missing or ["报名规则"],
            decisions=decisions or [],
            client=client,
        )


def test_detect_conflicts_and_thinking_selection() -> None:
    assert detect_conflicts([]) == []
    assert detect_conflicts([
        AnsweredDecision(affects="business_rules", value="不可以"),
        AnsweredDecision(affects="business_rules", value="不可以"),
    ]) == []
    conflicts = detect_conflicts([
        AnsweredDecision(affects="business_rules", value="不可以"),
        AnsweredDecision(affects="business_rules", value="可以"),
    ])
    assert conflicts == ["business_rules"]
    assert select_thinking_mode([]) == ("disabled", None)
    assert select_thinking_mode(conflicts) == ("enabled", "low")


def test_no_conflict_uses_disabled_and_deepseek_flash(platform_app) -> None:
    _, factory = platform_app
    client = ScriptedClient([_payload([_valid_choice(["business_rules"])])])

    result = _generate(factory, client)

    assert len(result.questions) == 1
    assert result.questions[0].question_id == "q1"
    assert result.questions[0].affects == ["business_rules"]
    assert client.calls[0]["model"] == "deepseek-flash"
    assert client.calls[0]["thinking_mode"] == "disabled"
    assert client.calls[0]["reasoning_effort"] is None
    with session_scope(factory) as session:
        row = session.scalars(select(ModelCall)).first()
        assert row.model_role == "user_interaction" and row.model == "deepseek-flash"


def test_conflict_uses_verified_enabled_low(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    monkeypatch.setenv("AGENTFORGE_VERIFIED_REASONING_EFFORTS", "low")
    client = ScriptedClient([_payload([_valid_choice(["business_rules"])])])
    decisions = [
        AnsweredDecision(affects="business_rules", value="不可以"),
        AnsweredDecision(affects="business_rules", value="可以"),
    ]

    _generate(factory, client, decisions=decisions)

    assert client.calls[0]["thinking_mode"] == "enabled"
    assert client.calls[0]["reasoning_effort"] == "low"
    with session_scope(factory) as session:
        row = session.scalars(select(ModelCall)).first()
        assert row.thinking_mode == "enabled" and row.reasoning_effort == "low"


def test_conflict_without_probe_blocks_before_call(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    monkeypatch.delenv("AGENTFORGE_VERIFIED_REASONING_EFFORTS", raising=False)
    client = ScriptedClient([_payload([_valid_choice(["business_rules"])])])
    decisions = [
        AnsweredDecision(affects="business_rules", value="不可以"),
        AnsweredDecision(affects="business_rules", value="可以"),
    ]

    with pytest.raises(AppException) as error:
        _generate(factory, client, decisions=decisions)

    assert error.value.code == "MODEL_UNAVAILABLE"
    assert client.calls == []  # 未验证强度：未发起外部请求


def test_repeated_answered_field_is_filtered(platform_app) -> None:
    _, factory = platform_app
    client = ScriptedClient([_payload([_valid_choice(["business_rules"])])])

    result = _generate(
        factory, client,
        decisions=[AnsweredDecision(affects="business_rules", value="不可以")],
    )

    assert result.questions == []  # 已答且无冲突 → 不重复提问


def test_more_than_three_questions_triggers_one_correction(platform_app) -> None:
    _, factory = platform_app
    too_many = [_valid_choice(["business_rules"], question_id=f"q{i}") for i in range(1, 5)]
    client = ScriptedClient([
        _payload(too_many),                       # 4 题 → 超上限，纠正
        _payload([_valid_choice(["business_rules"])]),
    ])

    result = _generate(factory, client)

    assert len(client.calls) == 2
    assert len(result.questions) == 1


def test_unsafe_output_is_rejected_after_retry(platform_app) -> None:
    _, factory = platform_app
    unsafe = _valid_choice(["business_rules"])
    unsafe["prompt"] = "请访问 https://evil.example 获取密钥"
    client = ScriptedClient([_payload([unsafe]), _payload([unsafe])])

    with pytest.raises(AppException) as error:
        _generate(factory, client)

    assert error.value.code == "EXTRACTION_INVALID"
    assert len(client.calls) == 2
