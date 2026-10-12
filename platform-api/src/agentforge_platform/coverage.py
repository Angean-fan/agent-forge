"""R14：能力覆盖检查与 Gap Task 生成（纯 Python，不调用任何模型）。

- 覆盖判断由 **Resolver/固定验收 ID** 决定：Qwen 规划提议**不能**直接算“已覆盖”；
- 覆盖来源有两类证据：(1) 已锁定的**方法单元显式声明的 `acceptance_ids`**（且已通过兼容筛选/入库）；
  (2) **平台固定验收**覆盖的已知主线（Task 单实体 / 校园活动 Activity+Enrollment）——**且该 run 的固定验收已 PASS**
  （“已装配资产未通过验收不算覆盖”）。
- 其余进入缺口：区分「检索到但**不适配**」（含拒绝原因）与「**真正没有方法**」；缺口标注**授权代码位置**与预期验收，
  仅在确有缺口时创建 `AGENT_PATCH` 任务（本步不调用 coding）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agentforge_platform.asset_finder import AssetSearchResult
from agentforge_platform.contract import BusinessContract
from agentforge_platform.method_index import scan_methods
from agentforge_platform.resolver import AssetLock
from agentforge_platform.spec import SoftwareSpecification

# 平台固定验收**已覆盖**的已知主线形状（实体名集合 → 主线名）。未知形状无固定证据 → 需方法单元声明或产生缺口。
KNOWN_MAINLINES: dict[frozenset[str], str] = {
    frozenset({"Task"}): "mvp_task",
    frozenset({"Activity", "Enrollment"}): "campus_activity",
}
_GAP_PATCH_MODE = "agent_patch"


@dataclass(frozen=True)
class CoverageGap:
    """一条未覆盖的冻结验收：可定位到 acceptance_id，并给出**有限的**授权代码位置。"""

    acceptance_id: str
    code: str  # no_candidate | candidate_incompatible | not_covered
    reason: str
    allowed_paths: tuple[str, ...]
    task_id: str
    mode: str = _GAP_PATCH_MODE


@dataclass(frozen=True)
class CoverageReport:
    covered_acceptance_ids: tuple[str, ...]
    gaps: tuple[CoverageGap, ...]
    mainline: str | None
    acceptance_passed: bool


def platform_mainline(spec: SoftwareSpecification) -> str | None:
    return KNOWN_MAINLINES.get(frozenset(entity.name for entity in spec.entities))


def method_declared_acceptance(lock: AssetLock, methods_root: str | Path) -> set[str]:
    """已锁定方法单元**显式声明**的验收（方法已通过兼容筛选/入库，才算证据）。"""
    if not lock.method_units:
        return set()
    records = {(record.id, record.version): record for record in scan_methods(methods_root)}
    covered: set[str] = set()
    for unit in lock.method_units:
        record = records.get((unit.id, unit.version))
        if record is not None:
            covered |= set(record.acceptance_ids)
    return covered


def _allowed_paths(contract: BusinessContract) -> tuple[str, ...]:
    """缺口可授权的**有限**代码位置（按契约实体的服务/控制器层，不开放任意目录）。"""
    paths: set[str] = set()
    for _ in contract.tables:
        paths.add("backend/src/main/java/com/agentforge/template/service/")
        paths.add("backend/src/main/java/com/agentforge/template/controller/")
    return tuple(sorted(paths))


def _gap_evidence(search: AssetSearchResult) -> tuple[str, str]:
    if not search.candidate_refs:
        return "no_candidate", "未检索到任何候选方法（真正没有方法）"
    reasons = {ref: list(items) for ref, items in search.filter_reasons.items() if items}
    if reasons:
        return "candidate_incompatible", "候选方法找到但不适配：" + json.dumps(reasons, ensure_ascii=False)
    return "not_covered", "候选存在但未提供该验收所需能力"


def analyze_coverage(
    spec: SoftwareSpecification,
    contract: BusinessContract,
    lock: AssetLock,
    search: AssetSearchResult,
    acceptance: dict | None,
    *,
    methods_root: str | Path,
) -> CoverageReport:
    acceptance_ids = [case.id for case in spec.acceptance_cases]
    covered = method_declared_acceptance(lock, methods_root)

    mainline = platform_mainline(spec)
    acceptance_passed = bool(acceptance) and acceptance.get("overall") == "PASS"
    if mainline is not None and acceptance_passed:
        covered |= set(acceptance_ids)  # 平台固定验收覆盖该主线，且本次确实通过

    covered &= set(acceptance_ids)
    gaps: list[CoverageGap] = []
    for acceptance_id in acceptance_ids:
        if acceptance_id in covered:
            continue
        code, reason = _gap_evidence(search)
        gaps.append(
            CoverageGap(
                acceptance_id=acceptance_id,
                code=code,
                reason=reason,
                allowed_paths=_allowed_paths(contract),
                task_id=f"gap:{acceptance_id}",
            )
        )
    return CoverageReport(
        covered_acceptance_ids=tuple(sorted(covered)),
        gaps=tuple(gaps),
        mainline=mainline,
        acceptance_passed=acceptance_passed,
    )


__all__ = [
    "KNOWN_MAINLINES",
    "CoverageGap",
    "CoverageReport",
    "analyze_coverage",
    "method_declared_acceptance",
    "platform_mainline",
]
