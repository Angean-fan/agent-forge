"""F07：模块自治对照评估、可观测性与启用门槛。

以**固定业务规格、同一 Profile、同一受保护验收**为边界，对照「原单执行器」与「模块 Agent」两条路径，
如实报告首轮/修复后通过率、资产复用分层、未覆盖验收、接口复用、**越权拦截**、模型调用/Token/费用/UNKNOWN、
耗时、失败原因与**重放一致性**。

重要边界：

- **不改写 R27 的九次独立生成评估与 7/9 门槛**（那是 Demo P0 的判据，此处独立）；
- **模型不可用时不作"真实成本节省"结论**：证据类型显式标注 ``REAL`` / ``SIMULATED``；
- **开销为负收益也如实记录**；新功能以开关灰度启用，原单执行器保留为**可撤销回退路径**。

本模块**纯确定性、零真实模型调用**（脚本化引擎驱动 F01/F04 的真实组件）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from agentforge_platform.errors import AppException
from agentforge_platform.interface_registry import InterfaceRegistry
from agentforge_platform.module_agent import ModuleAgentFactory, module_agents_enabled
from agentforge_platform.module_memory import ModuleMemoryStore
from agentforge_platform.module_registry import derive_modules
from agentforge_platform.spec import SoftwareSpecification

EVIDENCE_REAL = "REAL"
EVIDENCE_SIMULATED = "SIMULATED"


@dataclass(frozen=True)
class RunObservation:
    """一条路径的**一次**运行观测（脱敏，供对照与重放）。"""

    path: str
    run_id: str
    first_pass: bool
    repaired_pass: bool
    asset_reuse: dict[str, int]
    uncovered_acceptance_ids: tuple[str, ...]
    interface_reuse: int
    overreach_blocked: int
    cross_module_writes: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    cost_micro: int
    unknown_calls: int
    duration_s: float
    failure_reason: str | None
    fingerprint: str


def _fingerprint(paths: list[str]) -> str:
    blob = "\n".join(sorted(paths))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def replay_consistent(observations: list[RunObservation]) -> bool:
    """同一路径多次运行的**重放一致性**：变更指纹完全一致。"""
    fingerprints = {o.fingerprint for o in observations}
    return len(fingerprints) <= 1


@dataclass(frozen=True)
class ComparisonReport:
    baseline: tuple[RunObservation, ...]
    module: tuple[RunObservation, ...]
    simulated: bool

    def _rate(self, path_observations: tuple[RunObservation, ...], field: str) -> float:
        if not path_observations:
            return 0.0
        return sum(1 for o in path_observations if getattr(o, field)) / len(path_observations)

    def _sum(self, path_observations: tuple[RunObservation, ...], field: str) -> int:
        return sum(int(getattr(o, field)) for o in path_observations)

    @property
    def evidence_kind(self) -> str:
        return EVIDENCE_SIMULATED if self.simulated else EVIDENCE_REAL

    def summary(self) -> dict:
        base, mod = self.baseline, self.module

        def block(obs: tuple[RunObservation, ...]) -> dict:
            return {
                "runs": len(obs),
                "first_pass_rate": self._rate(obs, "first_pass"),
                "repaired_pass_rate": self._rate(obs, "repaired_pass"),
                "uncovered_acceptance_ids": sorted({i for o in obs for i in o.uncovered_acceptance_ids}),
                "interface_reuse": self._sum(obs, "interface_reuse"),
                "overreach_blocked": self._sum(obs, "overreach_blocked"),
                "cross_module_writes": self._sum(obs, "cross_module_writes"),
                "model_calls": self._sum(obs, "model_calls"),
                "input_tokens": self._sum(obs, "input_tokens"),
                "output_tokens": self._sum(obs, "output_tokens"),
                "cost_micro": self._sum(obs, "cost_micro"),
                "unknown_calls": self._sum(obs, "unknown_calls"),
                "duration_s": round(sum(o.duration_s for o in obs), 3),
                "failure_reasons": sorted({o.failure_reason for o in obs if o.failure_reason}),
                "replay_consistent": replay_consistent(list(obs)),
            }

        return {
            "evidence_kind": self.evidence_kind,
            "simulated": self.simulated,
            "single_executor": block(base),
            "module_agent": block(mod),
        }


def compare(
    baseline: list[RunObservation], module: list[RunObservation], *, simulated: bool
) -> ComparisonReport:
    return ComparisonReport(baseline=tuple(baseline), module=tuple(module), simulated=simulated)


@dataclass(frozen=True)
class SecurityGates:
    """硬性安全/正确性门禁（由各自的负例测试提供证据）。"""

    overreach_blocked: bool
    interface_change_blocked: bool
    crash_recovery_ok: bool
    disabled_fallback_ok: bool

    @property
    def all_pass(self) -> bool:
        return (
            self.overreach_blocked and self.interface_change_blocked
            and self.crash_recovery_ok and self.disabled_fallback_ok
        )

    def to_dict(self) -> dict:
        return {
            "overreach_blocked": self.overreach_blocked,
            "interface_change_blocked": self.interface_change_blocked,
            "crash_recovery_ok": self.crash_recovery_ok,
            "disabled_fallback_ok": self.disabled_fallback_ok,
        }


def enablement_verdict(
    report: ComparisonReport, *, gates: SecurityGates, real_evidence: bool
) -> dict:
    """是否可把 ``module_agents_enabled`` 在授权环境启用：须安全门禁全过且**不回退正确性**。"""
    reasons: list[str] = []
    if not gates.all_pass:
        reasons.append("硬性安全/正确性门禁未全过：" + "、".join(
            k for k, v in gates.to_dict().items() if not v))
    summary = report.summary()
    base_first = summary["single_executor"]["first_pass_rate"]
    mod_first = summary["module_agent"]["first_pass_rate"]
    if mod_first < base_first:
        reasons.append(f"模块路径首轮通过率回退：{mod_first} < {base_first}")
    if not summary["module_agent"]["replay_consistent"]:
        reasons.append("模块路径重放不一致")

    cost_note = (
        "模型可用：可报告真实费用对比。"
        if real_evidence else "模型不可用/未授权：不作真实成本节省结论（标记 SIMULATED/NOT_RUN）。"
    )
    return {
        "enabled": not reasons,
        "reasons": reasons,
        "evidence_kind": EVIDENCE_REAL if real_evidence else EVIDENCE_SIMULATED,
        "cost_claim": cost_note,
        "report": summary,
    }


# --------------------------------------------------------------------------- #
# 确定性对照运行（脚本化引擎驱动 F01/F04 真实组件；**模拟**、无真实模型/网络）
# --------------------------------------------------------------------------- #
def _scripted_factory(writes: dict[str, list[str]]):
    def factory(_allowed, task_id):
        class _Engine:
            def run(self, _prompt):
                return {"change_set": {"changes": [
                    {"path": p, "action": "create"} for p in writes.get(task_id, [])
                ]}}

        return _Engine(), None

    return factory


def simulate_module_path(
    spec: SoftwareSpecification,
    *,
    writes: dict[str, list[str]],
    task_allowed_paths: list[str],
    root: str | Path,
    run_id: str,
    duration_s: float = 0.0,
) -> RunObservation:
    """模块路径：每模块独立受限执行；越权写入被拒（计 `overreach_blocked`）。"""
    modules = derive_modules(spec, project_id="eval")
    factory = ModuleAgentFactory(
        module_registry=modules,
        memory_store=ModuleMemoryStore(Path(root) / "mem"),
        interface_registry=InterfaceRegistry(project_id="eval", based_on_spec_hash=modules.based_on_spec_hash),
        engine_factory=_scripted_factory(writes),
        contract_revision=0,
    )
    calls = overreach = reuse = 0
    written: list[str] = []
    for module in modules.modules:
        executor = factory.executor_for(module.module_id, gap={"module": module.module_id})
        try:
            result = executor.run(task_allowed_paths=task_allowed_paths)
        except AppException:
            overreach += 1  # 越权写入被工具层拒绝
            continue
        calls += result.coding_calls
        written += [c["path"] for c in (result.change_set or {}).get("changes", [])]
    # 模块路径：每个 Agent 仅写自身 owned_paths（越权已被工具层拒绝）→ 跨模块写恒为 0。
    return RunObservation(
        path="module", run_id=run_id, first_pass=bool(written), repaired_pass=True,
        asset_reuse={"exact": 0, "parametric": 0, "structured": 0},
        uncovered_acceptance_ids=() if written else ("AC01",),
        interface_reuse=reuse, overreach_blocked=overreach, cross_module_writes=0,
        model_calls=calls, input_tokens=0, output_tokens=0, cost_micro=0, unknown_calls=0,
        duration_s=duration_s, failure_reason=None if written else "no_writes",
        fingerprint=_fingerprint(written),
    )


def simulate_single_path(
    spec: SoftwareSpecification,
    *,
    writes: dict[str, list[str]],
    run_id: str,
    duration_s: float = 0.0,
) -> RunObservation:
    """单执行器路径：一次广泛授权，无模块边界（可跨模块写，计 `cross_module_writes`）。"""
    engine, _ = _scripted_factory(writes)("", "single")
    result = engine.run("single-executor")
    written = [c["path"] for c in result["change_set"]["changes"]]
    modules = derive_modules(spec, project_id="eval")
    owners = {p: m.module_id for m in modules.modules for p in m.owned_paths}
    cross = len({owners.get(p) for p in written} - {None})
    return RunObservation(
        path="single", run_id=run_id, first_pass=True, repaired_pass=True,
        asset_reuse={"exact": 0, "parametric": 0, "structured": 0},
        uncovered_acceptance_ids=(), interface_reuse=0, overreach_blocked=0,
        cross_module_writes=max(cross - 1, 0),
        model_calls=1 if written else 0, input_tokens=0, output_tokens=0, cost_micro=0,
        unknown_calls=0, duration_s=duration_s, failure_reason=None,
        fingerprint=_fingerprint(written),
    )


__all__ = [
    "EVIDENCE_REAL",
    "EVIDENCE_SIMULATED",
    "ComparisonReport",
    "RunObservation",
    "SecurityGates",
    "compare",
    "enablement_verdict",
    "module_agents_enabled",
    "replay_consistent",
    "simulate_module_path",
    "simulate_single_path",
]
