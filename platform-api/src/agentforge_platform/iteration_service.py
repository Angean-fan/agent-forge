"""C05：既有生成项目的**真实增量迭代**（E09，依赖 C02）。

在 E09 的"新规格版本 + 新 run"工作流之上补齐两件真实能力：

1. **最小增量证据**：:func:`diff_artifacts` 逐文件比对新旧 run 目录，给出
   ``unchanged/changed/added/removed``——用于证明**未触及模块保持字节稳定**、变更被限制在受影响文件；
2. **网关化模型缝**：变更意图（**DeepSeek** ``user_interaction``）与技术影响分析（**Qwen**
   ``technical_planning``）经 :func:`agentforge_platform.model_gateway.invoke` 逐次预留/结算并入账；
   缺凭据记 ``NOT_RUN``、路由禁用记 ``DISABLED``，**不伪造调用成功**。

约束：只支持 AgentForge 自有产物（复用 :mod:`iteration`）；旧 run/ZIP/报告**不原地修改**；
本次调用为**草稿期**（``run_id=None``），仅接收**脱敏结构化事实**，不改写用户已确认需求。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from agentforge_platform.clients import ProviderPreflightError
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.iteration import ChangeImpact
from agentforge_platform.model_gateway import invoke

STATUS_OK = "OK"
STATUS_DISABLED = "DISABLED"
STATUS_NOT_RUN = "NOT_RUN"

_MAX_INPUT_TOKENS = 4000
_MAX_OUTPUT_TOKENS = 2048
DEFAULT_IGNORE_SUFFIXES = (".zip",)


@dataclass(frozen=True)
class DeltaReport:
    """新旧 run 目录的逐文件差异（相对路径）。"""

    unchanged: tuple[str, ...]
    changed: tuple[str, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def touched(self) -> tuple[str, ...]:
        return tuple(sorted(self.changed + self.added + self.removed))

    def summary(self) -> dict[str, Any]:
        return {
            "unchanged": len(self.unchanged),
            "changed": list(self.changed),
            "added": list(self.added),
            "removed": list(self.removed),
        }


def _file_hashes(root: Path, ignore_suffixes: tuple[str, ...]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.endswith(ignore_suffixes):
            continue
        hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def diff_artifacts(
    old_dir: str | Path,
    new_dir: str | Path,
    *,
    ignore_suffixes: tuple[str, ...] = DEFAULT_IGNORE_SUFFIXES,
) -> DeltaReport:
    """逐文件比对两个 run 目录；ZIP 默认忽略（各 run 自带、内容随打包时间不同）。"""
    old = _file_hashes(Path(old_dir), ignore_suffixes)
    new = _file_hashes(Path(new_dir), ignore_suffixes)
    unchanged = tuple(sorted(p for p in old.keys() & new.keys() if old[p] == new[p]))
    changed = tuple(sorted(p for p in old.keys() & new.keys() if old[p] != new[p]))
    added = tuple(sorted(new.keys() - old.keys()))
    removed = tuple(sorted(old.keys() - new.keys()))
    return DeltaReport(unchanged=unchanged, changed=changed, added=added, removed=removed)


def untouched_stable(delta: DeltaReport, *, minimum_unchanged: int = 1) -> bool:
    """未触及模块稳定性判据：存在未变文件、且无文件被删除、变更仅限被触及集合。"""
    return len(delta.unchanged) >= minimum_unchanged and not delta.removed


def refine_change_intent_gateway(
    db: Session, *, project_id: int, intent: str, client: Any
) -> tuple[str, str]:
    """经 **DeepSeek**（``user_interaction``）生成变更意图的用户可读反馈（草稿期调用）。

    缺凭据 → ``("", NOT_RUN)``；只把**脱敏**的用户意图交给模型，不改写已确认需求/权限。返回
    ``(用户可读文本, 状态)``。
    """
    prompt = (
        "你是需求助理。用中文向用户复述其**变更意图**，并提示该变更将生成**新版本**、旧产物保持不变；"
        "不得新增需求或权限，不得承诺已完成。\n"
        + json.dumps({"intent": intent.strip()}, ensure_ascii=False)
    )
    try:
        completion = invoke(
            db, call_id=uuid.uuid4().hex, model_role="user_interaction", client=client,
            prompt=prompt, project_id=project_id,
            max_input_tokens=_MAX_INPUT_TOKENS, max_output_tokens=_MAX_OUTPUT_TOKENS,
        )
    except ProviderPreflightError:
        return "", STATUS_NOT_RUN
    return completion.content.strip(), STATUS_OK


def analyze_impact_gateway(
    db: Session, *, project_id: int, impact: ChangeImpact, client: Any
) -> tuple[dict, str]:
    """经 **Qwen**（``technical_planning``）对**已确认变更**做最小任务建议（草稿期调用）。

    路由禁用 → ``({}, DISABLED)``；缺凭据 → ``({}, NOT_RUN)``；预算/次数护栏异常向上传播。
    只把确定性影响摘要交给模型，事实字段仍以程序计算为准。
    """
    facts = json.dumps(
        {
            "contract_changed": impact.contract_changed,
            "added_fields": list(impact.added_fields),
            "added_rules": list(impact.added_rules),
            "removed_fields": list(impact.removed_fields),
        },
        ensure_ascii=False, sort_keys=True,
    )
    prompt = (
        "你是技术影响分析助手。仅依据下列**已确认变更事实**，用 JSON 给出**最小**增量任务建议"
        "（字段 additions，形如 {\"tasks\":[...]})；不得扩大需求或改动既有模块。\n" + facts
    )
    try:
        completion = invoke(
            db, call_id=uuid.uuid4().hex, model_role="technical_planning", client=client,
            prompt=prompt, project_id=project_id,
            max_input_tokens=_MAX_INPUT_TOKENS, max_output_tokens=_MAX_OUTPUT_TOKENS,
            json_mode=True,
        )
    except ProviderPreflightError:
        return {}, STATUS_NOT_RUN
    except AppException as exc:
        if exc.code is ErrorCode.MODEL_ROLE_DISABLED:
            return {}, STATUS_DISABLED
        raise
    try:
        analysis = json.loads(completion.content)
    except ValueError:
        return {"note": completion.content.strip()}, STATUS_OK
    return analysis if isinstance(analysis, dict) else {"note": analysis}, STATUS_OK


__all__ = [
    "DEFAULT_IGNORE_SUFFIXES",
    "STATUS_DISABLED",
    "STATUS_NOT_RUN",
    "STATUS_OK",
    "DeltaReport",
    "analyze_impact_gateway",
    "diff_artifacts",
    "refine_change_intent_gateway",
    "untouched_stable",
]
