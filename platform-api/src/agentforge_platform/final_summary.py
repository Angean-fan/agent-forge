"""R06：面向用户的最终需求摘要（`user_interaction → deepseek-flash`）。

- **思考模式由 Python 确定性决定**：复杂汇总（见 :func:`is_complex_summary`）优先
  `enabled + high`，但**仅当 `high` 已通过真实接口探针**（R03，列入
  ``AGENTFORGE_VERIFIED_REASONING_EFFORTS``）时才下发；未验证则降级到 `low`（若已验证）
  或 `disabled`。结果**如实回报实际使用的模式**，绝不谎称已使用 `high`。
- DeepSeek 只起草/解释摘要，**不代替用户确认**：不改动 spec 任何 ``source``，不把 ``pending``
  变已成事实。
- 一次生成（最多一次结构纠正重试），经 M21 网关计费并按 ``call_id`` 入账；不保存隐藏思考。
"""

from __future__ import annotations

import re
from uuid import uuid4

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from agentforge_platform.clients import ModelClient
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.model_gateway import invoke, verified_reasoning_efforts
from agentforge_platform.models import Project, SpecVersion
from agentforge_platform.production_routing import default_user_interaction_client
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import load_spec

_MAX_OUTPUT_TOKENS = 2048
_MAX_INPUT_TOKENS = 6000
# 确定性复杂度阈值：多实体、较多业务规则/验收场景或较多页面视为“复杂汇总”。
_COMPLEX_RULE_AND_CASE_TOTAL = 6
_COMPLEX_PAGE_TOTAL = 4
_UNSAFE_OUTPUT = re.compile(
    r"https?://|javascript:|<script|sk-[A-Za-z0-9]{12,}|api[_ -]?key|密钥|令牌|环境变量"
    r"|\bcurl\b|\bshell\b|终端|执行命令",
    re.IGNORECASE,
)


class FinalSummaryPayload(BaseModel):
    """Provider 只产出一段面向用户的中文摘要，不携带任何结构或来源声明。"""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1000)


_PARSER = PydanticOutputParser(pydantic_object=FinalSummaryPayload)
_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            (
                "你是需求整理助手，面向不懂技术的用户。根据已确认的需求事实，写一段简洁的中文最终需求摘要（150–300 字），输出 JSON。"
                "只用业务语言，禁止出现框架、ORM、数据库、技术选型或工具术语。"
                "只复述给出的已确认事实，不得新增、夸大或推断任何需求；不得改动用户已确认的范围。"
                "摘要中不要出现 URL、命令、密钥或任何操作指示。"
                "仅输出符合以下格式的 JSON：\n{format_instructions}"
            ),
        ),
        ("human", "<confirmed_facts>\n{facts}\n</confirmed_facts>\n{correction}"),
    ]
)


def is_complex_summary(spec: SoftwareSpecification) -> bool:
    """确定性判定是否需要“复杂汇总”（多实体或较多业务规则/验收场景/页面）。"""
    if len(spec.entities) >= 2:
        return True
    if len(spec.business_rules) + len(spec.acceptance_cases) >= _COMPLEX_RULE_AND_CASE_TOTAL:
        return True
    return len(spec.pages) >= _COMPLEX_PAGE_TOTAL


def select_summary_thinking(spec: SoftwareSpecification) -> tuple[str, str | None]:
    """复杂度 → 思考模式；`high` 仅在经探针验证后使用，否则降级，绝不谎称。"""
    if is_complex_summary(spec):
        verified = verified_reasoning_efforts()
        if "high" in verified:
            return ("enabled", "high")
        if "low" in verified:
            return ("enabled", "low")
    return ("disabled", None)


def _facts(spec: SoftwareSpecification) -> str:
    lines = [
        f"目标：{spec.goal}",
        f"角色：{'、'.join(spec.actors) if spec.actors else '（无）'}",
        "实体：" + ("、".join(entity.name for entity in spec.entities) or "（无）"),
    ]
    if spec.business_rules:
        lines.append("业务规则：")
        lines += [f"- [{rule.id}] {rule.text}" for rule in spec.business_rules]
    if spec.acceptance_cases:
        lines.append("验收场景：")
        lines += [
            f"- [{case.id}] {case.scenario} → {case.expected}" for case in spec.acceptance_cases
        ]
    if spec.pages:
        lines.append("业务页面：" + "、".join(page.name for page in spec.pages))
    if spec.exclusions:
        lines.append("排除项：" + "；".join(spec.exclusions))
    if spec.constraints:
        lines.append("约束：" + "；".join(spec.constraints))
    return "\n".join(lines)


def _prompt(spec: SoftwareSpecification, *, correction: bool) -> str:
    return _PROMPT.format(
        format_instructions=_PARSER.get_format_instructions(),
        facts=_facts(spec),
        correction="上次响应未通过结构校验。请重新输出合法 JSON。" if correction else "",
    )


def _input_token_bound(prompt: str) -> int:
    return max(_MAX_INPUT_TOKENS, len(prompt.encode("utf-8")) + 1024)


def generate_final_summary(
    db: Session,
    project: Project,
    row: SpecVersion,
    *,
    client: ModelClient | None = None,
) -> tuple[str, str, str | None]:
    """生成最终摘要；返回 ``(summary, thinking_mode, reasoning_effort)``（实际使用值）。"""
    spec = load_spec(row)
    thinking_mode, reasoning_effort = select_summary_thinking(spec)
    provider = default_user_interaction_client(client)

    for attempt in range(2):
        prompt = _prompt(spec, correction=attempt == 1)
        try:
            completion = invoke(
                db,
                call_id=uuid4().hex,
                model_role="user_interaction",
                client=provider,
                prompt=prompt,
                project_id=project.id,
                draft_id=row.id,
                max_input_tokens=_input_token_bound(prompt),
                max_output_tokens=_MAX_OUTPUT_TOKENS,
                thinking_mode=thinking_mode,
                reasoning_effort=reasoning_effort,
                json_mode=True,
            )
        except AppException:
            raise
        except Exception as exc:
            raise AppException(
                ErrorCode.MODEL_UNAVAILABLE, "最终需求摘要生成暂不可用。", status_code=503
            ) from exc

        try:
            if completion.finish_reason != "stop":
                raise ValueError("模型输出未完成")
            payload = _PARSER.parse(completion.content)
            if _UNSAFE_OUTPUT.search(payload.summary):
                raise ValueError("模型输出含不安全的操作指示")
        except (OutputParserException, ValidationError, ValueError) as exc:
            if attempt == 1:
                raise AppException(
                    ErrorCode.EXTRACTION_INVALID,
                    "模型未生成可用的最终摘要。",
                    status_code=422,
                ) from exc
            continue

        return payload.summary, thinking_mode, reasoning_effort

    raise AssertionError("摘要生成循环未返回")


__all__ = [
    "generate_final_summary",
    "is_complex_summary",
    "select_summary_thinking",
]
