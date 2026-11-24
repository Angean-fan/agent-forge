"""R04：LangGraph 访谈中断/恢复图。

`understand → detect_missing → ask → await_answer(interrupt) → validate → ready_or_wait`，
以 ``SqliteSaver`` 在同一平台 SQLite 上持久化检查点，``thread_id`` 绑定 ``project_id + draft_id``。

要点：
- 生成问题（DeepSeek `user_interaction`）只在 ``ask`` 节点发生；**中断节点 `await_answer` 无副作用**，
  因此恢复（``Command(resume=...)``）重放中断节点时不会重复扣费或重复写库。
- 答案经 R02 ``validate_submission`` 校验（含题目版本绑定），落回 ``decisions`` 驱动下一轮缺失/冲突。
- 最多 ``MAX_ROUNDS`` 轮，超额进入 ``NEEDS_INPUT``；不调用 Qwen。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform.clients import ModelClient
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException
from agentforge_platform.interview import (
    MAX_ROUNDS,
    Answer,
    AnswerSubmission,
    Question,
    validate_submission,
)
from agentforge_platform.interview_generation import (
    AnsweredDecision,
    detect_conflicts,
    generate_questions,
)
from agentforge_platform.models import Project, SpecVersion

# FR02「必须收敛的信息」映射到 spec 业务区块：未收敛则继续追问，收敛则 READY_TO_CONFIRM。
REQUIRED_DECISIONS: tuple[str, ...] = ("actors", "entities", "permissions", "business_rules")
BUSY_TIMEOUT_MS = 5000


class InterviewError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InterviewState(TypedDict, total=False):
    project_id: int
    draft_id: int
    round: int
    missing_fields: list[str]
    conflicts: list[str]
    decisions: list[dict]
    questions: list[dict]
    questions_round: int
    answers: list[dict]
    resolved: list[str]
    pending: list[str]
    issues: list[str]
    answered_round: bool
    status: str
    error_code: str


@dataclass(frozen=True)
class InterviewContext:
    session_factory: sessionmaker[Session]
    database_url: str
    client: ModelClient | None = None


def thread_config(project_id: int, draft_id: int) -> dict:
    return {"configurable": {"thread_id": f"interview:{project_id}:{draft_id}"}}


def _sqlite_path(database_url: str) -> str:
    """解析访谈检查点 SQLite 文件路径。

    - SQLite 平台库：直接复用平台 SQLite 文件（保持 R04 零变更）；
    - PostgreSQL 等非 SQLite 平台库：落到独立本地检查点 SQLite 文件（可由
      ``AGENTFORGE_INTERVIEW_CHECKPOINT_DB`` 指定，默认按连接目标哈希隔离存放在 ``var/`` 下），
      确保切换 PG 后动态访谈 interrupt/resume 仍完整可用。
    """
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        if not url.database:
            raise InterviewError("SPEC_NOT_FOUND", "访谈检查点需要有效的 SQLite 路径")
        return str(Path(url.database))

    override = os.environ.get("AGENTFORGE_INTERVIEW_CHECKPOINT_DB", "").strip()
    if override:
        target = Path(override)
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    # 非 SQLite（如 PostgreSQL）：按库目标与 schema 哈希生成独立检查点文件，避免不同库/测试 schema 串扰
    scope_key = f"{url.host or ''}:{url.port or ''}/{url.database or ''}?{sorted(url.query.items())}"
    digest = hashlib.sha256(scope_key.encode("utf-8")).hexdigest()[:12]
    base_dir = Path(os.environ.get("RUNNER_WORK_ROOT", "./var/runs")).resolve().parent
    target = base_dir / f"interview-checkpoints-{digest}.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    return str(target)


class _Checkpointer:
    """同一平台 SQLite 文件上的会话级 SqliteSaver；每次使用后关闭连接。"""

    def __init__(self, database_url: str) -> None:
        self._path = _sqlite_path(database_url)
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> SqliteSaver:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._conn = conn
        saver = SqliteSaver(conn)
        saver.setup()
        return saver

    def __exit__(self, *_exc) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def build_interview_graph(context: InterviewContext, saver: SqliteSaver):
    def understand(state: InterviewState) -> InterviewState:
        project_id, draft_id = state["project_id"], state["draft_id"]
        with session_scope(context.session_factory) as db:
            draft = db.get(SpecVersion, draft_id)
            if draft is None or draft.project_id != project_id:
                raise InterviewError("SPEC_NOT_FOUND", "访谈草稿不存在或与项目不符")
            if draft.confirmed_at is not None:
                raise InterviewError("SPEC_CONFLICT", "访谈针对未确认草稿")
        return {
            "round": state.get("round") or 1,
            "decisions": state.get("decisions") or [],
            "questions": [],
            "answers": [],
        }

    def detect_missing(state: InterviewState) -> InterviewState:
        decisions = [AnsweredDecision(**item) for item in state.get("decisions", [])]
        conflicts = detect_conflicts(decisions)
        answered = {d.affects for d in decisions if d.affects not in set(conflicts)}
        missing = [key for key in REQUIRED_DECISIONS if key not in answered]
        if not missing and not conflicts:
            status = "READY_TO_CONFIRM"
        elif state["round"] > MAX_ROUNDS:
            status = "NEEDS_INPUT"
        else:
            status = "CLARIFYING"
        return {"missing_fields": missing, "conflicts": conflicts, "status": status}

    def ask(state: InterviewState) -> InterviewState:
        if state.get("questions_round") == state["round"]:
            return {}  # 该轮已生成，防止节点重放时重复计费
        with session_scope(context.session_factory) as db:
            project = db.get(Project, state["project_id"])
            draft = db.get(SpecVersion, state["draft_id"])
            if project is None or draft is None:
                raise InterviewError("SPEC_NOT_FOUND", "访谈草稿不存在")
            decisions = [AnsweredDecision(**item) for item in state.get("decisions", [])]
            try:
                result = generate_questions(
                    db, project, draft,
                    missing_fields=state["missing_fields"],
                    decisions=decisions, client=context.client,
                )
            except AppException as exc:
                return {"status": "NEEDS_INPUT", "error_code": exc.code, "questions": []}
            # 同一草稿：记录本轮问题与缺失项，推进草稿修订号。
            draft.questions_json = json.dumps(
                [question.prompt for question in result.questions], ensure_ascii=False
            )
            draft.missing_fields_json = json.dumps(state["missing_fields"], ensure_ascii=False)
            draft.draft_revision += 1
            questions = [question.model_dump(mode="json") for question in result.questions]
        return {"questions": questions, "questions_round": state["round"]}

    def await_answer(state: InterviewState) -> InterviewState:
        # 无副作用：恢复时重放本节点只返回 resume 值。
        answers = interrupt({"round": state["round"], "questions": state["questions"]})
        return {"answers": answers or []}

    def validate(state: InterviewState) -> InterviewState:
        questions = [Question(**item) for item in state.get("questions", [])]
        submission = AnswerSubmission(
            answers=[Answer(**item) for item in state.get("answers", [])]
        )
        result = validate_submission(questions, submission)
        decisions = list(state.get("decisions", []))
        resolved_ids = set(result["resolved"])
        for answer in submission.answers:
            if answer.question_id not in resolved_ids:
                continue
            question = next((q for q in questions if q.question_id == answer.question_id), None)
            if question is None:
                continue
            value = ",".join(answer.selected) if answer.selected else (answer.custom or "")
            decisions.extend({"affects": affect, "value": value} for affect in question.affects)
        return {
            "decisions": decisions,
            "resolved": result["resolved"],
            "pending": result["pending"],
            "issues": result["issues"],
            "answered_round": bool(result["resolved"]),
        }

    def ready_or_wait(state: InterviewState) -> InterviewState:
        # 本轮无有效新答案（重复/过期 resume）→ 不推进轮次；随后 ask 因
        # questions_round==round 不再调用模型，仅重新抛出同一批问题，故不重复计费。
        if not state.get("answered_round"):
            return {}
        return {"round": state["round"] + 1}

    def route_after_detect(state: InterviewState) -> str:
        return "ask" if state["status"] == "CLARIFYING" else "finish"

    def route_after_ask(state: InterviewState) -> str:
        return "await_answer" if not state.get("error_code") else "finish"

    graph = StateGraph(InterviewState)
    graph.add_node("understand", understand)
    graph.add_node("detect_missing", detect_missing)
    graph.add_node("ask", ask)
    graph.add_node("await_answer", await_answer)
    graph.add_node("validate", validate)
    graph.add_node("ready_or_wait", ready_or_wait)
    graph.add_node("finish", lambda _state: {})

    graph.add_edge(START, "understand")
    graph.add_edge("understand", "detect_missing")
    graph.add_conditional_edges(
        "detect_missing", route_after_detect, {"ask": "ask", "finish": "finish"}
    )
    graph.add_conditional_edges(
        "ask", route_after_ask, {"await_answer": "await_answer", "finish": "finish"}
    )
    graph.add_edge("await_answer", "validate")
    graph.add_edge("validate", "ready_or_wait")
    graph.add_edge("ready_or_wait", "detect_missing")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=saver)


def start_interview(context: InterviewContext, *, project_id: int, draft_id: int) -> dict:
    with _Checkpointer(context.database_url) as saver:
        app = build_interview_graph(context, saver)
        return app.invoke(
            {"project_id": project_id, "draft_id": draft_id}, thread_config(project_id, draft_id)
        )


def resume_interview(
    context: InterviewContext, *, project_id: int, draft_id: int, answers: list[dict]
) -> dict:
    with _Checkpointer(context.database_url) as saver:
        app = build_interview_graph(context, saver)
        return app.invoke(Command(resume=answers), thread_config(project_id, draft_id))


def read_interview(context: InterviewContext, *, project_id: int, draft_id: int) -> dict | None:
    """读取持久化状态（不 invoke），用于“离开后回来接续”。无检查点时返回 None。"""
    with _Checkpointer(context.database_url) as saver:
        app = build_interview_graph(context, saver)
        snapshot = app.get_state(thread_config(project_id, draft_id))
        if not snapshot.values:
            return None
        return {**snapshot.values, "waiting": bool(snapshot.next)}


__all__ = [
    "REQUIRED_DECISIONS",
    "InterviewContext",
    "InterviewError",
    "InterviewState",
    "build_interview_graph",
    "read_interview",
    "resume_interview",
    "start_interview",
    "thread_config",
]
