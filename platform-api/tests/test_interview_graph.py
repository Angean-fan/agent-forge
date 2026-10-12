"""R04 LangGraph 访谈中断/恢复测试（不触网，脚本化假客户端 + 真实 SQLite 检查点）。"""

from __future__ import annotations

import json

from sqlalchemy import select

from agentforge_platform.clients import Completion, Usage
from agentforge_platform.db import make_engine, make_session_factory, session_scope
from agentforge_platform.interview_graph import (
    InterviewContext,
    resume_interview,
    start_interview,
)
from agentforge_platform.models import ModelCall, Project, SpecVersion


def _q(question_id: str, affects: list[str], prompt: str = "成员如何操作该记录？") -> dict:
    return {
        "question_id": question_id,
        "type": "single_choice",
        "prompt": prompt,
        "options": ["可以", "不可以"],
        "allow_custom": False,
        "required": True,
        "affects": affects,
    }


def _payload(questions: list[dict], unresolved: list[str] | None = None) -> str:
    return json.dumps(
        {"unresolved_fields": unresolved or [], "questions": questions}, ensure_ascii=False
    )


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
            "model": model, "thinking_mode": thinking_mode, "reasoning_effort": reasoning_effort,
        })
        content = self._contents[min(self._index, len(self._contents) - 1)]
        self._index += 1
        return Completion(
            content=content, model=model, request_id="rid", usage=Usage(10, 5),
            finish_reason="stop",
        )


def _answer(question_id: str, selected: tuple[str, ...] = ("可以",)) -> dict:
    return {"question_id": question_id, "question_version": 1, "selected": list(selected)}


def _seed(factory) -> tuple[int, int]:
    with session_scope(factory) as db:
        project = Project(owner_id=1, name="p", requirement_text="校园活动报名")
        db.add(project)
        db.flush()
        draft = SpecVersion(project_id=project.id, version=1, body_json="{}", hash="x")
        db.add(draft)
        db.flush()
        return project.id, draft.id


def _ctx(url: str, client: ScriptedClient) -> InterviewContext:
    return InterviewContext(
        session_factory=make_session_factory(make_engine(url)), database_url=url, client=client
    )


def test_start_interrupts_with_questions(migrated_sqlite_url: str) -> None:
    client = ScriptedClient([_payload([_q("q1", ["actors", "entities"])])])
    ctx = _ctx(migrated_sqlite_url, client)
    project_id, draft_id = _seed(ctx.session_factory)

    result = start_interview(ctx, project_id=project_id, draft_id=draft_id)

    assert "__interrupt__" in result
    assert result["status"] == "CLARIFYING"
    assert [q["question_id"] for q in result["questions"]] == ["q1"]
    assert client.calls[0]["model"] == "deepseek-flash"
    assert client.calls[0]["thinking_mode"] == "disabled"


def test_two_rounds_update_same_draft_then_ready(migrated_sqlite_url: str) -> None:
    client = ScriptedClient([
        _payload([_q("q1", ["actors", "entities"])]),
        _payload([_q("q2", ["permissions", "business_rules"])]),
    ])
    ctx = _ctx(migrated_sqlite_url, client)
    project_id, draft_id = _seed(ctx.session_factory)

    start_interview(ctx, project_id=project_id, draft_id=draft_id)
    second = resume_interview(ctx, project_id=project_id, draft_id=draft_id, answers=[_answer("q1")])
    assert [q["question_id"] for q in second["questions"]] == ["q2"]

    final = resume_interview(ctx, project_id=project_id, draft_id=draft_id, answers=[_answer("q2")])

    assert final["status"] == "READY_TO_CONFIRM"
    assert "__interrupt__" not in final
    assert len(client.calls) == 2  # 每轮一次生成，无多余调用
    with session_scope(ctx.session_factory) as db:
        draft = db.get(SpecVersion, draft_id)
        assert draft.draft_revision == 3  # 初始 1 + 两轮
        row = db.scalars(select(ModelCall)).first()
        assert row.model_role == "user_interaction" and row.model == "deepseek-flash"


def test_restart_recovers_pending_interview(migrated_sqlite_url: str) -> None:
    client = ScriptedClient([
        _payload([_q("q1", ["actors", "entities"])]),
        _payload([_q("q2", ["permissions", "business_rules"])]),
    ])
    ctx1 = _ctx(migrated_sqlite_url, client)
    project_id, draft_id = _seed(ctx1.session_factory)
    first = start_interview(ctx1, project_id=project_id, draft_id=draft_id)
    assert "__interrupt__" in first

    # 模拟进程重启：全新引擎/上下文，同一 SQLite 文件
    ctx2 = _ctx(migrated_sqlite_url, client)
    resumed = resume_interview(
        ctx2, project_id=project_id, draft_id=draft_id, answers=[_answer("q1")]
    )

    assert [q["question_id"] for q in resumed["questions"]] == ["q2"]  # 从持久化检查点续跑


def test_duplicate_resume_does_not_recharge_or_reapply(migrated_sqlite_url: str) -> None:
    client = ScriptedClient([
        _payload([_q("q1", ["actors", "entities"])]),
        _payload([_q("q2", ["permissions", "business_rules"])]),
    ])
    ctx = _ctx(migrated_sqlite_url, client)
    project_id, draft_id = _seed(ctx.session_factory)

    start_interview(ctx, project_id=project_id, draft_id=draft_id)
    resume_interview(ctx, project_id=project_id, draft_id=draft_id, answers=[_answer("q1")])
    calls_after_first = len(client.calls)  # =2

    # 重复提交过期答案（属于第 1 轮的 q1，而当前是第 2 轮的 q2）
    duplicate = resume_interview(
        ctx, project_id=project_id, draft_id=draft_id, answers=[_answer("q1")]
    )

    assert len(client.calls) == calls_after_first  # 未重复计费
    assert [q["question_id"] for q in duplicate["questions"]] == ["q2"]  # 仍停在 q2
    assert "__interrupt__" in duplicate
    assert len(duplicate["decisions"]) == 2  # actors/entities 未被重复写入


def test_round_limit_reaches_needs_input(migrated_sqlite_url: str) -> None:
    # 每轮都返回一个「非必填」业务决定的问题：有进展但 4 个必填项始终缺失 → 4 轮后 NEEDS_INPUT。
    client = ScriptedClient([
        _payload([_q("q1", ["fields"], prompt="需要记录哪些信息？")]),
        _payload([_q("q2", ["pages"], prompt="需要哪些页面？")]),
        _payload([_q("q3", ["exclusions"], prompt="哪些功能不做？")]),
        _payload([_q("q4", ["goal"], prompt="目标是解决什么问题？")]),
        _payload([_q("q5", ["relations"], prompt="实体之间如何关联？")]),
    ])
    ctx = _ctx(migrated_sqlite_url, client)
    project_id, draft_id = _seed(ctx.session_factory)

    result = start_interview(ctx, project_id=project_id, draft_id=draft_id)
    for question_id in ("q1", "q2", "q3", "q4"):
        assert "__interrupt__" in result
        result = resume_interview(
            ctx, project_id=project_id, draft_id=draft_id, answers=[_answer(question_id)]
        )

    assert result["status"] == "NEEDS_INPUT"
    assert "__interrupt__" not in result
    assert len(client.calls) == 4  # 只问了 4 轮，未超上限继续追问
