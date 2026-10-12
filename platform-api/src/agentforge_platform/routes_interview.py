"""R05：访谈问答 API。

`GET  /api/projects/{id}/interview`：读取当前访谈状态（问题、轮次、待输入/就绪/失败）。
`POST /api/projects/{id}/interview`：无 answers 时开始访谈（已开始则原样返回）；带 answers 时续答。

- 前端只展示 R03 经 schema 校验的题面（业务语言，非 Qwen 原始输出）；
- 过期/非法答案由 R04 validate 判定，返回 issues 且不推进；
- 保存与续接依赖 R04 的持久化检查点，用户可离开后回来。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agentforge_platform.auth import require_csrf, require_project_owner
from agentforge_platform.db import get_db, make_session_factory
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.interview import Answer
from agentforge_platform.interview_graph import (
    InterviewContext,
    read_interview,
    resume_interview,
    start_interview,
)
from agentforge_platform.middleware import get_request_id
from agentforge_platform.models import Project, SpecVersion
from agentforge_platform.responses import ApiResponse
from agentforge_platform.spec_service import latest_spec

router = APIRouter(prefix="/api/projects", tags=["interview"])


class InterviewQuestionView(BaseModel):
    question_id: str
    version: int
    type: str
    prompt: str
    options: list[str] = []
    allow_custom: bool = False
    required: bool = False
    affects: list[str] = []


class InterviewView(BaseModel):
    project_id: int
    draft_id: int
    round: int
    status: str  # NEW / CLARIFYING / READY_TO_CONFIRM / NEEDS_INPUT
    waiting: bool
    questions: list[InterviewQuestionView] = []
    pending: list[str] = []
    issues: list[str] = []
    error_code: str | None = None


class InterviewSubmitRequest(BaseModel):
    answers: list[Answer] = Field(default_factory=list)


def _require_draft(db: Session, project: Project) -> SpecVersion:
    draft = latest_spec(db, project.id)
    if draft is None or draft.confirmed_at is not None:
        raise AppException(
            ErrorCode.SPEC_NOT_FOUND, "没有可访谈的未确认草稿。", status_code=404
        )
    return draft


def _context(db: Session) -> InterviewContext:
    """复用当前请求所用数据库（含测试覆盖），并在其上开检查点会话。"""
    bind = db.get_bind()
    return InterviewContext(
        session_factory=make_session_factory(bind),
        database_url=str(bind.url),
        client=None,  # 生产走 DeepSeek；测试 monkeypatch DeepSeekClient.complete
    )


def _view(project_id: int, draft_id: int, state: dict) -> InterviewView:
    return InterviewView(
        project_id=project_id,
        draft_id=draft_id,
        round=int(state.get("round") or 0),
        status=state.get("status") or "NEW",
        waiting=bool(state.get("waiting")),
        questions=[InterviewQuestionView(**item) for item in state.get("questions", [])],
        pending=list(state.get("pending") or []),
        issues=list(state.get("issues") or []),
        error_code=state.get("error_code"),
    )


def _empty_view(project_id: int, draft_id: int) -> InterviewView:
    return InterviewView(
        project_id=project_id, draft_id=draft_id, round=0, status="NEW", waiting=False
    )


@router.get("/{project_id}/interview", response_model=ApiResponse[InterviewView])
def get_interview(
    request: Request,
    project: Project = Depends(require_project_owner),
    db: Session = Depends(get_db),
) -> ApiResponse[InterviewView]:
    draft = _require_draft(db, project)
    state = read_interview(_context(db), project_id=project.id, draft_id=draft.id)
    data = _empty_view(project.id, draft.id) if state is None else _view(
        project.id, draft.id, state
    )
    return ApiResponse(request_id=get_request_id(request), data=data)


@router.post("/{project_id}/interview", response_model=ApiResponse[InterviewView])
def submit_interview(
    payload: InterviewSubmitRequest,
    request: Request,
    project: Project = Depends(require_project_owner),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApiResponse[InterviewView]:
    draft = _require_draft(db, project)
    context = _context(db)
    existing = read_interview(context, project_id=project.id, draft_id=draft.id)

    if payload.answers:
        if existing is None or not existing.get("waiting"):
            raise AppException(
                ErrorCode.BAD_REQUEST, "访谈当前不在等待答案。", status_code=409
            )
        result = resume_interview(
            context,
            project_id=project.id,
            draft_id=draft.id,
            answers=[answer.model_dump() for answer in payload.answers],
        )
        state = {**result, "waiting": "__interrupt__" in result}
    elif existing is not None:
        state = existing  # 已开始：原样返回当前问题，不重复生成/计费
    else:
        result = start_interview(context, project_id=project.id, draft_id=draft.id)
        state = {**result, "waiting": "__interrupt__" in result}

    return ApiResponse(
        request_id=get_request_id(request), data=_view(project.id, draft.id, state)
    )
