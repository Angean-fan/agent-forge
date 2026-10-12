"""R02：访谈题目与答案 schema（本步不调用模型）。

定义结构化澄清问题与回答协议，供 R03（DeepSeek 生成问题）、R04（LangGraph 中断/恢复）
与 R05（前端问答卡片）共用。要点：

- 题目含稳定 ``question_id`` 与 ``version``、选项、自定义开关、必填标记，以及
  ``affects``（本问题改变哪项业务决定，取值对齐 spec 的业务区块，避免框架术语）。
- 答案**必须绑定题目版本**：旧版题目的答案不得写入新版本题目（:func:`validate_answer`）。
- 支持单选、多选、文本、自定义补充项与"不确定"；"不确定"不视为已明确。

本模块只做 schema 与确定性校验，不做持久化、不调用任何模型；轮次上限见
``MAX_ROUNDS``/``MAX_QUESTIONS_PER_ROUND``。
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_QUESTIONS_PER_ROUND = 3
MAX_ROUNDS = 4
MAX_PROMPT_LENGTH = 200


class QuestionType(StrEnum):
    """题型：单选、多选、文本。"""

    SINGLE_CHOICE = "single_choice"
    MULTI_CHOICE = "multi_choice"
    TEXT = "text"


# affects：本问题改变哪项业务决定；对齐 spec 的业务区块（非框架/技术术语）。
AFFECT_KEYS = frozenset({
    "goal", "actors", "entities", "fields", "relations", "pages",
    "permissions", "business_rules", "acceptance_cases", "constraints", "exclusions",
})

_STRICT = ConfigDict(extra="forbid")
_SAFE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
# 面向普通用户的题面不得出现框架/ORM/技术选型术语。
_TECH_TERMS = re.compile(
    r"(MyBatis[- ]?Plus|MyBatis|Hibernate|JPA|ORM|Spring|Vue|React|Angular|JWT|Redis|主键|外键|框架)",
    re.IGNORECASE,
)


class Question(BaseModel):
    """一条面向用户的澄清问题。"""

    model_config = _STRICT

    question_id: str
    version: int = 1
    type: QuestionType
    prompt: str
    options: list[str] = Field(default_factory=list)
    allow_custom: bool = False
    required: bool = False
    affects: list[str]

    @field_validator("question_id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError(f"不安全的 question_id：{value!r}")
        return value

    @field_validator("version")
    @classmethod
    def _positive_version(cls, value: int) -> int:
        if value < 1:
            raise ValueError("题目版本必须 ≥ 1")
        return value

    @field_validator("prompt")
    @classmethod
    def _readable_prompt(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("题面不能为空")
        if len(text) > MAX_PROMPT_LENGTH:
            raise ValueError(f"题面最长 {MAX_PROMPT_LENGTH} 字")
        if _TECH_TERMS.search(text):
            raise ValueError("题面应使用业务语言，不能出现框架/ORM 等技术术语")
        return text

    @field_validator("affects")
    @classmethod
    def _known_affects(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if not cleaned:
            raise ValueError("affects 不能为空：必须声明本问题影响的业务决定")
        unknown = [item for item in cleaned if item not in AFFECT_KEYS]
        if unknown:
            raise ValueError(f"未知的 affects 业务决定：{unknown}")
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def _options_match_type(self) -> Question:
        if self.type is QuestionType.TEXT:
            if self.options:
                raise ValueError("文本题的 options 必须为空")
        else:
            if not self.options:
                raise ValueError("选择题必须提供 options")
            if any(not option.strip() for option in self.options):
                raise ValueError("options 不能包含空字符串")
            if len(set(self.options)) != len(self.options):
                raise ValueError("options 不能重复")
        return self


class QuestionBatch(BaseModel):
    """一轮访谈（FR02：最多 4 轮，每轮最多 3 题）。"""

    model_config = _STRICT

    round: int = Field(ge=1, le=MAX_ROUNDS)
    questions: list[Question] = Field(min_length=1, max_length=MAX_QUESTIONS_PER_ROUND)

    @model_validator(mode="after")
    def _unique_ids(self) -> QuestionBatch:
        ids = [question.question_id for question in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("同一轮内 question_id 不能重复")
        return self


class Answer(BaseModel):
    """一条回答，绑定其题目版本。

    ``selected`` 为从 options 中选择的项；``custom`` 为自定义补充项（文本题也用此字段）；
    ``unsure`` 表示"不确定"。
    """

    model_config = _STRICT

    question_id: str
    question_version: int
    selected: list[str] = Field(default_factory=list)
    custom: str | None = None
    unsure: bool = False

    @field_validator("custom")
    @classmethod
    def _non_empty_custom(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("自定义答案不能为空字符串")
        return text


class AnswerSubmission(BaseModel):
    """一次提交的全部回答。"""

    model_config = _STRICT

    answers: list[Answer] = Field(default_factory=list)


def validate_answer(question: Question, answer: Answer) -> list[str]:
    """校验一条答案相对其题目是否合法；返回问题列表（空 = 合法）。"""
    issues: list[str] = []
    if answer.question_id != question.question_id:
        return [f"答案 question_id 与题目不一致：{answer.question_id} != {question.question_id}"]
    if answer.question_version != question.version:
        return [
            (
                f"答案绑定题目版本 {answer.question_version}，当前为 {question.version}；"
                "过期题目的答案不予接受"
            )
        ]

    if answer.unsure:
        if answer.selected or answer.custom is not None:
            issues.append("标记为不确定时不能同时给出选择或自定义答案")
        return issues

    unknown = [item for item in answer.selected if item not in question.options]
    if unknown:
        issues.append(f"选项不在题目范围内：{unknown}")
    if answer.custom is not None and not question.allow_custom:
        issues.append("该题不允许自定义答案")

    if question.type is QuestionType.TEXT:
        if answer.selected:
            issues.append("文本题不能选择选项")
        if question.required and answer.custom is None:
            issues.append("必填题缺少答案")
    elif question.type is QuestionType.SINGLE_CHOICE:
        if len(answer.selected) > 1:
            issues.append("单选题最多选择一个选项")
        if answer.selected and answer.custom is not None:
            issues.append("单选题不能同时选择选项和自定义答案")
        if question.required and not answer.selected and answer.custom is None:
            issues.append("必填题缺少答案")
    else:  # MULTI_CHOICE
        if question.required and not answer.selected and answer.custom is None:
            issues.append("必填题缺少答案")
    return issues


def validate_submission(questions: list[Question], submission: AnswerSubmission) -> dict:
    """对照当前题目校验整份提交。

    返回 ``{"issues": [...], "resolved": [...], "pending": [...]}``：

    - 未知题目 / 版本不匹配 / 非法答案 → 计入 ``issues``，不进入 ``resolved``；
    - 合法且非"不确定"的回答 → 进入 ``resolved``；
    - 必填题若未被 resolved（未回答或标记"不确定"）→ 计入 ``pending``。
    """
    by_id = {question.question_id: question for question in questions}
    issues: list[str] = []
    resolved: set[str] = set()
    for answer in submission.answers:
        question = by_id.get(answer.question_id)
        if question is None:
            issues.append(f"未知题目：{answer.question_id}")
            continue
        answer_issues = validate_answer(question, answer)
        if answer_issues:
            issues.extend(f"{answer.question_id}: {item}" for item in answer_issues)
        elif not answer.unsure:
            resolved.add(answer.question_id)
    pending = [
        question.question_id
        for question in questions
        if question.required and question.question_id not in resolved
    ]
    return {"issues": issues, "resolved": sorted(resolved), "pending": pending}


__all__ = [
    "AFFECT_KEYS",
    "MAX_QUESTIONS_PER_ROUND",
    "MAX_ROUNDS",
    "Answer",
    "AnswerSubmission",
    "Question",
    "QuestionBatch",
    "QuestionType",
    "validate_answer",
    "validate_submission",
]
