"""R09：四组合 Profile 选项与面向用户的通俗解释路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from agentforge_platform.accounts import Account
from agentforge_platform.auth import get_current_account, require_csrf, require_project_owner
from agentforge_platform.db import get_db
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.middleware import get_request_id
from agentforge_platform.models import Project
from agentforge_platform.profile_explain import explain_profile
from agentforge_platform.profiles_matrix import find_option, list_profile_options
from agentforge_platform.responses import ApiResponse

router = APIRouter(prefix="/api", tags=["profiles"])
_STRICT = ConfigDict(extra="forbid")


class ProfileOptionView(BaseModel):
    model_config = _STRICT

    profile_id: str
    profile_version: str
    template_version: str
    frontend_language: str | None
    orm: str | None
    recommended: bool
    notes: list[str]


class ProfileExplanationRequest(BaseModel):
    model_config = _STRICT

    profile_id: str


class ProfileExplanationView(BaseModel):
    model_config = _STRICT

    profile_id: str
    explanation: str
    thinking_mode: str
    reasoning_effort: str | None


@router.get("/profiles", response_model=ApiResponse[list[ProfileOptionView]])
def list_profiles(
    request: Request,
    _account: Account = Depends(get_current_account),
) -> ApiResponse[list[ProfileOptionView]]:
    """已认证的四组合选项与**确定性**特点（不调用模型）。"""
    return ApiResponse(
        request_id=get_request_id(request),
        data=[ProfileOptionView(**option) for option in list_profile_options()],
    )


@router.post(
    "/projects/{project_id}/profile-explanation",
    response_model=ApiResponse[ProfileExplanationView],
)
def explain_profile_option(
    payload: ProfileExplanationRequest,
    request: Request,
    project: Project = Depends(require_project_owner),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApiResponse[ProfileExplanationView]:
    """面向用户的选型通俗解释（`user_interaction → deepseek-flash`；仅基于已认证事实）。"""
    option = find_option(payload.profile_id)
    if option is None:
        raise AppException(ErrorCode.NOT_FOUND, "未认证的 Profile 组合。", status_code=404)
    explanation, thinking_mode, reasoning_effort = explain_profile(db, project, option)
    return ApiResponse(
        request_id=get_request_id(request),
        data=ProfileExplanationView(
            profile_id=option["profile_id"],
            explanation=explanation,
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
        ),
    )
