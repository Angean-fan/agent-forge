"""R09：面向用户的技术选型**通俗解释**（`user_interaction → deepseek-flash`）。

- 只依据 :func:`profiles_matrix.list_profile_options` 的**已认证结构化事实**生成解释，不读原始聊天，
  不改写事实、权限、费用或检查状态；**不调用 Qwen**。
- 只在固定模板无法清楚表达时才额外调用（本函数即该唯一入口）；生成前记账在**项目**上下文（草稿阶段）。
- 不保存隐藏思考；返回**实际使用**的思考模式。
"""

from __future__ import annotations

import re
from uuid import uuid4

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from agentforge_platform.clients import DeepSeekClient, ModelClient
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.model_gateway import invoke
from agentforge_platform.models import Project

_MAX_OUTPUT_TOKENS = 1024
_MAX_INPUT_TOKENS = 2000
_UNSAFE_OUTPUT = re.compile(
    r"https?://|javascript:|<script|sk-[A-Za-z0-9]{12,}|api[_ -]?key|密钥|令牌|环境变量"
    r"|\bcurl\b|\bshell\b|终端|执行命令",
    re.IGNORECASE,
)


class ProfileExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    explanation: str = Field(min_length=1, max_length=1000)


_PARSER = PydanticOutputParser(pydantic_object=ProfileExplanation)
_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            (
                "你是技术选型说明助手，面向不懂技术的用户。仅根据给定的**已认证事实**，用简洁通俗的中文解释"
                "这套技术组合的特点、局限与推荐理由（约 100–200 字），输出 JSON。"
                "只复述事实，不得新增或夸大；不得给出 URL、命令、密钥或任何操作指示；不得替用户做决定。"
                "仅输出符合以下格式的 JSON：\n{format_instructions}"
            ),
        ),
        ("human", "<verified_facts>\n{facts}\n</verified_facts>\n{correction}"),
    ]
)


def _facts(option: dict) -> str:
    return "\n".join(
        [
            f"组合：{option['profile_id']}@{option['profile_version']}（模板 {option['template_version']}）",
            f"前端语言：{option.get('frontend_language')}",
            f"后端 ORM：{option.get('orm')}",
            "是否默认推荐：是" if option.get("recommended") else "是否默认推荐：否",
            *(f"- {note}" for note in option.get("notes", [])),
        ]
    )


def _prompt(option: dict, *, correction: bool) -> str:
    return _PROMPT.format(
        format_instructions=_PARSER.get_format_instructions(),
        facts=_facts(option),
        correction="上次响应未通过结构校验。请重新输出合法 JSON。" if correction else "",
    )


def _input_token_bound(prompt: str) -> int:
    return max(_MAX_INPUT_TOKENS, len(prompt.encode("utf-8")) + 512)


def explain_profile(
    db: Session, project: Project, option: dict, *, client: ModelClient | None = None
) -> tuple[str, str, str | None]:
    """生成选型通俗解释；返回 ``(explanation, thinking_mode, reasoning_effort)``。"""
    provider = client or DeepSeekClient()
    thinking_mode: str | None = "disabled"
    for attempt in range(2):
        prompt = _prompt(option, correction=attempt == 1)
        try:
            completion = invoke(
                db,
                call_id=uuid4().hex,
                model_role="user_interaction",
                client=provider,
                prompt=prompt,
                project_id=project.id,
                max_input_tokens=_input_token_bound(prompt),
                max_output_tokens=_MAX_OUTPUT_TOKENS,
                thinking_mode=thinking_mode,
                json_mode=True,
            )
        except AppException:
            raise
        except Exception as exc:
            raise AppException(
                ErrorCode.MODEL_UNAVAILABLE, "技术选型解释暂不可用。", status_code=503
            ) from exc

        try:
            if completion.finish_reason != "stop":
                raise ValueError("模型输出未完成")
            payload = _PARSER.parse(completion.content)
            if _UNSAFE_OUTPUT.search(payload.explanation):
                raise ValueError("模型输出含不安全的操作指示")
        except (OutputParserException, ValidationError, ValueError) as exc:
            if attempt == 1:
                raise AppException(
                    ErrorCode.EXTRACTION_INVALID, "模型未生成可用的解释。", status_code=422
                ) from exc
            continue
        return payload.explanation, thinking_mode, None

    raise AssertionError("解释循环未返回")


__all__ = ["explain_profile"]
