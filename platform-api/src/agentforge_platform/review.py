"""R19：完整复验与**最多一轮**技术语义审查（`full_verify → review → package`）。

- **完整复验**：打包前必须执行全部必需检查；`assert_full_verification` 拒绝缺项（不得以部分验证冒充通过）。
- **可选语义审查**：由 **Qwen3.7 Plus**（`technical_planning` 角色）对照**冻结业务规则**做**最多一轮**审查；
  只接收冻结 spec、契约、代码与**固定验收结果**，**不重新解释 DeepSeek 对话**。
- **不覆盖确定性结果**：审查只**产出问题**并**转换为可验证的修复任务**（登记 PENDING），
  不因模型自评绕过失败测试、不改冻结规格或固定测试；若据此改动代码，必须重新完整验收（由修复支路保证）。
- 需要面向用户的解释时，由 DeepSeek 在**脱敏事实**之上转述（本模块不直接对用户输出模型文本）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from agentforge_platform import model_gateway
from agentforge_platform.contract import BusinessContract
from agentforge_platform.model_gateway import invoke
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.repair import repair_allowed_paths
from agentforge_platform.spec import SoftwareSpecification

REVIEW_ROLE = "technical_planning"  # Qwen3.7 Plus
_MAX_INPUT_TOKENS = 8000
_MAX_OUTPUT_TOKENS = 2048
_MAX_CODE_FILES = 20
_MAX_CODE_CHARS = 4000
_STRICT = ConfigDict(extra="forbid")


class ReviewFinding(BaseModel):
    model_config = _STRICT

    rule_id: str | None = None
    summary: str
    evidence: str = ""
    severity: Literal["blocker", "major", "minor"] = "minor"
    related_acceptance_ids: list[str] = []
    suggested_operation: str = "review_fix"


class _ReviewOutput(BaseModel):
    model_config = _STRICT

    findings: list[ReviewFinding]


@dataclass(frozen=True)
class ReviewResult:
    invoked: bool
    findings: list[ReviewFinding] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)
    error: str | None = None
    model_role: str = REVIEW_ROLE


def missing_required_checks(verification: dict) -> list[str]:
    """完整复验所需但缺失的检查名（缺项即**不可能**判定通过；供报告留痕）。"""
    present = {check.get("name") for check in verification.get("checks", [])}
    return [name for name in VERIFY_REQUIRED if name not in present]


def assert_full_verification(verification: dict) -> None:
    """完整复验门禁（供直接调用/测试）：必需检查缺项即拒绝。"""
    missing = missing_required_checks(verification)
    if missing:
        raise AssertionError(f"复验不完整，缺少必需检查：{missing}")


def review_enabled() -> bool:
    return model_gateway.role_enabled(REVIEW_ROLE)


# --------------------------------------------------------------------------- #
# 审查上下文（冻结、脱敏、无对话历史）
# --------------------------------------------------------------------------- #
def _authorized_files(run_dir: Path, allowed_paths: list[str]) -> list[tuple[str, str]]:
    snippets: list[tuple[str, str]] = []
    for allowed in allowed_paths:
        base = run_dir / allowed
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            snippets.append((path.relative_to(run_dir).as_posix(), text[:_MAX_CODE_CHARS]))
            if len(snippets) >= _MAX_CODE_FILES:
                return snippets
    return snippets


def build_review_context(
    spec: SoftwareSpecification,
    contract: BusinessContract,
    verification: dict,
    acceptance: dict,
    *,
    run_dir: str | Path,
    allowed_paths: list[str] | None = None,
) -> dict:
    """冻结上下文：规格/契约/固定验收结果/授权代码片段；**不含**对话历史。"""
    root = Path(run_dir)
    paths = allowed_paths if allowed_paths is not None else repair_allowed_paths(contract)
    return {
        "spec": {
            "goal": spec.goal,
            "entities": [entity.model_dump(mode="json") for entity in spec.entities],
            "permissions": [p.model_dump(mode="json") for p in spec.permissions],
            "business_rules": [r.model_dump(mode="json") for r in spec.business_rules],
            "acceptance_cases": [c.model_dump(mode="json") for c in spec.acceptance_cases],
        },
        "contract": {
            "routes": [route.model_dump(mode="json") for route in contract.routes],
            "tables": [table.model_dump(mode="json") for table in contract.tables],
        },
        "deterministic_results": {
            "verification_overall": verification.get("overall"),
            "acceptance_overall": acceptance.get("overall"),
            "checks": verification.get("checks", []),
            "acceptance_cases": acceptance.get("cases", []),
        },
        "authorized_code": [
            {"path": path, "content": content}
            for path, content in _authorized_files(root, paths)
        ],
        "instructions": (
            "仅依据冻结规格/契约/代码与固定验收结果，找出**与技术语义相关**的问题（如未覆盖业务规则、"
            "越权、状态机缺失）；不得重新解释用户对话、不得建议修改冻结规格或固定测试。输出 JSON findings。"
        ),
    }


def render_review_prompt(context: dict) -> str:
    return (
        "你是技术语义审查助手。对照冻结业务规则审查生成工程，只报问题、不改代码。"
        "输出 JSON：{\"findings\":[{\"rule_id\",\"summary\",\"evidence\",\"severity\","
        "\"related_acceptance_ids\",\"suggested_operation\"}]}；severity 取 blocker/major/minor。\n"
        + json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True)
    )


def findings_to_tasks(findings: list[ReviewFinding], contract: BusinessContract) -> list[dict]:
    """把问题转换为**可验证的具体修复任务**（登记用；不自动执行、不改冻结规格/固定测试）。"""
    allowed = repair_allowed_paths(contract)
    return [
        {
            "task_id": f"review:{index}",
            "operation": finding.suggested_operation,
            "allowed_paths": allowed,
            "acceptance_ids": list(finding.related_acceptance_ids),
            "severity": finding.severity,
            "summary": finding.summary,
        }
        for index, finding in enumerate(findings)
    ]


def run_semantic_review(
    db: Session,
    *,
    project_id: int,
    run_id: int,
    client,  # ModelClient
    context: dict,
    max_rounds: int = 1,
) -> ReviewResult:
    """最多一轮语义审查：经 `technical_planning`（Qwen3.7 Plus）结构化调用；失败不伪装成功。"""
    if max_rounds < 1:
        return ReviewResult(invoked=False)
    prompt = render_review_prompt(context)
    completion = invoke(
        db, call_id=uuid4().hex, model_role=REVIEW_ROLE, client=client, prompt=prompt,
        project_id=project_id, run_id=run_id, task_id=None,
        max_input_tokens=max(_MAX_INPUT_TOKENS, len(prompt.encode("utf-8")) + 1024),
        max_output_tokens=_MAX_OUTPUT_TOKENS, thinking_mode="disabled", json_mode=True,
    )
    if completion.finish_reason != "stop":
        return ReviewResult(invoked=True, error="REVIEW_INCOMPLETE")
    try:
        payload = _ReviewOutput.model_validate_json(completion.content)
    except ValidationError:
        return ReviewResult(invoked=True, error="REVIEW_INVALID")
    return ReviewResult(invoked=True, findings=payload.findings)


__all__ = [
    "REVIEW_ROLE",
    "ReviewFinding",
    "ReviewResult",
    "assert_full_verification",
    "build_review_context",
    "findings_to_tasks",
    "missing_required_checks",
    "render_review_prompt",
    "review_enabled",
    "run_semantic_review",
]
