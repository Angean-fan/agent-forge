"""E09：既有生成项目的规格增量迭代（技术文档 §6；需求第 9 节"既有项目迭代"）。

- **仅支持 AgentForge 自有产物**：源运行目录必须含 `spec.json` + `asset-lock.json`，否则拒绝（不承诺任意仓库改造）；
- **影响分析（确定性）**：对比新旧规格，判定**契约是否改变**（`contract_hash` + 实体/字段/规则差异）；
- **最小变更 + 新版本**：契约改变必须**显式重新检索并重新冻结新锁**，**不得**原地改旧 run/ZIP/锁；
- **优先复用**：升级优先重用**锁定的旧方法资产**与兼容新版本；
- 变更意图与用户可读反馈由 **DeepSeek** 处理、技术影响分析由 **Qwen** 处理（此处为**可注入缝**，默认确定性、不调用模型）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from agentforge_platform.contract import compile_from_spec
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.resolver import AssetLock
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import content_hash

ARTIFACT_SPEC = "spec.json"
ARTIFACT_LOCK = "asset-lock.json"


@dataclass(frozen=True)
class ChangeImpact:
    """新旧规格的确定性影响分析。"""

    contract_changed: bool
    added_entities: tuple[str, ...] = ()
    removed_entities: tuple[str, ...] = ()
    added_fields: tuple[str, ...] = ()  # "entity.field"
    removed_fields: tuple[str, ...] = ()
    added_rules: tuple[str, ...] = ()
    removed_rules: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        parts = []
        if self.added_entities:
            parts.append("新增实体：" + "、".join(self.added_entities))
        if self.added_fields:
            parts.append("新增字段：" + "、".join(self.added_fields))
        if self.removed_fields:
            parts.append("移除字段：" + "、".join(self.removed_fields))
        if self.added_rules:
            parts.append("新增规则：" + "、".join(self.added_rules))
        if self.removed_rules:
            parts.append("移除规则：" + "、".join(self.removed_rules))
        if self.contract_changed:
            parts.append("契约已改变，必须重新检索并重新冻结新锁")
        return "；".join(parts) or "无实质变更"


@dataclass(frozen=True)
class IterationPlan:
    """一次迭代的最小变更计划（只读；不写旧 run）。"""

    old_spec_hash: str
    new_spec_hash: str
    impact: ChangeImpact
    relock_required: bool
    reused_method_units: tuple[str, ...] = ()
    dropped_method_units: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


def artifact_eligibility(run_dir: str | Path) -> list[str]:
    """仅 AgentForge 自有产物可迭代：须含可解析的 spec.json + asset-lock.json。"""
    root = Path(run_dir)
    issues: list[str] = []
    spec_path, lock_path = root / ARTIFACT_SPEC, root / ARTIFACT_LOCK
    if not spec_path.is_file():
        issues.append(f"缺少 {ARTIFACT_SPEC}：不是 AgentForge 产物")
    else:
        try:
            json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            issues.append(f"{ARTIFACT_SPEC} 不可解析")
    if not lock_path.is_file():
        issues.append(f"缺少 {ARTIFACT_LOCK}：不是 AgentForge 产物")
    else:
        try:
            json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            issues.append(f"{ARTIFACT_LOCK} 不可解析")
    return issues


def load_artifact(run_dir: str | Path) -> tuple[SoftwareSpecification, AssetLock]:
    """读取源产物的冻结规格与资产锁；非 AgentForge 产物抛 ``ITERATION_INELIGIBLE``。"""
    root = Path(run_dir)
    issues = artifact_eligibility(root)
    if issues:
        raise AppException(
            ErrorCode.ITERATION_INELIGIBLE,
            "不支持迭代：" + "；".join(issues) + "（起步仅支持 AgentForge 自有产物）",
            status_code=422,
        )
    spec = SoftwareSpecification.model_validate_json((root / ARTIFACT_SPEC).read_text(encoding="utf-8"))
    lock = AssetLock.model_validate_json((root / ARTIFACT_LOCK).read_text(encoding="utf-8"))
    return spec, lock


def _contract_hash(spec: SoftwareSpecification) -> str | None:
    try:
        return compile_from_spec(spec).contract_hash
    except Exception:  # noqa: BLE001 - 规格暂不可编译时退回结构签名
        return None


def _structural_signature(spec: SoftwareSpecification) -> tuple:
    return (
        tuple(sorted((entity.name, tuple(sorted((f.name, f.type, f.required) for f in entity.fields)))
                     for entity in spec.entities)),
        tuple(sorted((perm.role, tuple(sorted(perm.actions))) for perm in spec.permissions)),
        tuple(sorted((rule.id, rule.text) for rule in spec.business_rules)),
    )


def classify_change(old: SoftwareSpecification, new: SoftwareSpecification) -> ChangeImpact:
    """确定性影响分析：契约是否改变 + 实体/字段/规则差异。"""
    old_hash, new_hash = _contract_hash(old), _contract_hash(new)
    if old_hash is not None and new_hash is not None:
        contract_changed = old_hash != new_hash
    else:
        contract_changed = _structural_signature(old) != _structural_signature(new)

    old_entities = {entity.name: {f.name for f in entity.fields} for entity in old.entities}
    new_entities = {entity.name: {f.name for f in entity.fields} for entity in new.entities}
    added_entities = tuple(sorted(set(new_entities) - set(old_entities)))
    removed_entities = tuple(sorted(set(old_entities) - set(new_entities)))
    added_fields, removed_fields = [], []
    for name in sorted(set(old_entities) & set(new_entities)):
        added_fields.extend(f"{name}.{f}" for f in sorted(new_entities[name] - old_entities[name]))
        removed_fields.extend(f"{name}.{f}" for f in sorted(old_entities[name] - new_entities[name]))

    old_rules = {rule.id: rule.text for rule in old.business_rules}
    new_rules = {rule.id: rule.text for rule in new.business_rules}
    added_rules = tuple(sorted(set(new_rules) - set(old_rules)))
    removed_rules = tuple(sorted(set(old_rules) - set(new_rules)))

    return ChangeImpact(
        contract_changed=contract_changed,
        added_entities=added_entities, removed_entities=removed_entities,
        added_fields=tuple(added_fields), removed_fields=tuple(removed_fields),
        added_rules=added_rules, removed_rules=removed_rules,
    )


def _requested_methods(spec: SoftwareSpecification) -> set[str]:
    return set(getattr(spec, "method_requests", []) or [])


def plan_iteration(
    old_spec: SoftwareSpecification,
    old_lock: AssetLock,
    new_spec: SoftwareSpecification,
) -> IterationPlan:
    """最小变更计划：契约改变 → 必须重新检索/冻结新锁；否则复用旧锁；方法资产按新规格复用。"""
    impact = classify_change(old_spec, new_spec)
    requested = _requested_methods(new_spec)
    reused, dropped = [], []
    for unit in old_lock.method_units:
        if not requested or unit.id in requested or f"{unit.id}@{unit.version}" in requested:
            reused.append(f"{unit.id}@{unit.version}")
        else:
            dropped.append(f"{unit.id}@{unit.version}")

    notes = ["旧 run / ZIP / 锁不予原地修改；本次生成新版本。"]
    if impact.contract_changed:
        notes.append("契约改变：显式重新检索并重新冻结新 asset-lock（不直接改旧 ZIP 的方法引用）。")
    else:
        notes.append("契约未变：可复用旧锁（含版本与 checksum）。")
    if reused:
        notes.append("优先复用锁定的旧方法资产：" + "、".join(reused))

    return IterationPlan(
        old_spec_hash=content_hash(old_spec),
        new_spec_hash=content_hash(new_spec),
        impact=impact,
        relock_required=impact.contract_changed,
        reused_method_units=tuple(reused),
        dropped_method_units=tuple(dropped),
        notes=tuple(notes),
    )


def refine_change_intent(intent: str, *, explain: Callable[[str], str] | None = None) -> str:
    """变更意图的用户可读反馈（DeepSeek 缝）：默认确定性；``explain`` 只接收脱敏事实。"""
    text = f"已记录的变更意图：{intent.strip() or '（空）'}"
    if explain is not None:
        text += "\n\n" + explain(json.dumps({"intent": intent.strip()}, ensure_ascii=False))
    return text


def analyze_impact(impact: ChangeImpact, *, planner: Callable[[str], str] | None = None) -> dict:
    """技术影响分析（Qwen 缝）：只对**已确认**变更给出最小任务建议；默认确定性。"""
    analysis = {
        "contract_changed": impact.contract_changed,
        "relock_required": impact.contract_changed,
        "added_fields": list(impact.added_fields),
        "added_rules": list(impact.added_rules),
        "summary": impact.summary,
    }
    if planner is not None:
        analysis["planner_note"] = planner(json.dumps(analysis, ensure_ascii=False, sort_keys=True))
    return analysis


def reused_units_summary(units: Iterable[str]) -> str:
    return "、".join(units)


__all__ = [
    "ARTIFACT_LOCK",
    "ARTIFACT_SPEC",
    "ChangeImpact",
    "IterationPlan",
    "analyze_impact",
    "artifact_eligibility",
    "classify_change",
    "load_artifact",
    "plan_iteration",
    "refine_change_intent",
    "reused_units_summary",
]
