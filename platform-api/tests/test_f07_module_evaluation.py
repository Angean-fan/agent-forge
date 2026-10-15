"""F07：模块自治对照评估与启用门槛。

覆盖（**确定性、零真实模型调用**）：

- 对照报告聚合（首轮/修复后通过率、越权拦截、跨模块写、模型调用/费用、重放一致性）；
- 模块路径**收敛写入**（无跨模块写、越权被拒）；单执行器路径**允许跨模块写**（对照点）；
- 启用门槛：**安全门禁全过且不回退正确性**才可启用；**模型不可用不作真实成本结论**；
- **不改写 R27 七点门槛**（本模块独立）；开关默认关闭、回退路径保留。
"""

from __future__ import annotations

from pathlib import Path

from agentforge_platform.module_evaluation import (
    EVIDENCE_SIMULATED,
    RunObservation,
    SecurityGates,
    compare,
    enablement_verdict,
    module_agents_enabled,
    replay_consistent,
    simulate_module_path,
    simulate_single_path,
)
from agentforge_platform.module_registry import derive_modules
from agentforge_platform.spec import SoftwareSpecification

REPO = Path(__file__).resolve().parents[2]
CAMPUS = REPO / "schemas" / "campus-activity.spec.json"


def _spec() -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(CAMPUS.read_text(encoding="utf-8"))


def _owned(module_id: str) -> list[str]:
    module = next(m for m in derive_modules(_spec(), project_id="eval").modules
                  if m.module_id == module_id)
    return module.owned_paths


def _obs(**overrides) -> RunObservation:
    payload: dict = {
        "path": "module", "run_id": "r", "first_pass": True, "repaired_pass": True,
        "asset_reuse": {"exact": 1, "parametric": 0, "structured": 0},
        "uncovered_acceptance_ids": (), "interface_reuse": 1, "overreach_blocked": 0,
        "cross_module_writes": 0, "model_calls": 2, "input_tokens": 100, "output_tokens": 50,
        "cost_micro": 200, "unknown_calls": 0, "duration_s": 1.5, "failure_reason": None,
        "fingerprint": "f",
    }
    payload.update(overrides)
    return RunObservation(**payload)


# --- 对照报告聚合 -------------------------------------------------------------

def test_comparison_summary_math() -> None:
    baseline = [_obs(path="single", first_pass=True, model_calls=1, cross_module_writes=1),
                _obs(path="single", first_pass=False, model_calls=1, cross_module_writes=0)]
    module = [_obs(model_calls=2), _obs(model_calls=2)]

    report = compare(baseline, module, simulated=True)
    summary = report.summary()

    assert summary["simulated"] is True and summary["evidence_kind"] == EVIDENCE_SIMULATED
    assert summary["single_executor"]["first_pass_rate"] == 0.5
    assert summary["module_agent"]["first_pass_rate"] == 1.0
    assert summary["module_agent"]["model_calls"] == 4
    assert summary["single_executor"]["cross_module_writes"] == 1


def test_replay_consistency() -> None:
    assert replay_consistent([_obs(fingerprint="f"), _obs(fingerprint="f")]) is True
    assert replay_consistent([_obs(fingerprint="f"), _obs(fingerprint="g")]) is False


# --- 两条路径的确定性对照（脚本化引擎，无真实模型） ---------------------------

def test_module_path_confines_writes(tmp_path: Path) -> None:
    writes = {"module:activity": [_owned("activity")[0]],
              "module:enrollment": [_owned("enrollment")[0]]}
    observation = simulate_module_path(_spec(), writes=writes,
                                       task_allowed_paths=["backend/", "frontend/"],
                                       root=tmp_path, run_id="m1")

    assert observation.model_calls == 2  # 每模块一次受限调用
    assert observation.cross_module_writes == 0 and observation.overreach_blocked == 0
    assert observation.first_pass is True


def test_module_path_blocks_overreach(tmp_path: Path) -> None:
    # activity 的 Agent 试图写 enrollment 的文件 → 被模块边界拒绝
    writes = {"module:activity": [_owned("enrollment")[0]]}
    observation = simulate_module_path(_spec(), writes=writes,
                                       task_allowed_paths=["backend/", "frontend/"],
                                       root=tmp_path, run_id="m2")

    assert observation.overreach_blocked == 1
    assert observation.failure_reason == "no_writes"  # 无合法写入 → 缺口保留


def test_single_path_allows_cross_module_writes(tmp_path: Path) -> None:
    observation = simulate_single_path(
        _spec(), writes={"single": [_owned("activity")[0], _owned("enrollment")[0]]}, run_id="s1")

    assert observation.model_calls == 1  # 单执行器一次完成
    assert observation.cross_module_writes == 1  # 但**跨模块写**（无边界）——对照点


# --- 启用门槛 -----------------------------------------------------------------

def _all_gates() -> SecurityGates:
    return SecurityGates(overreach_blocked=True, interface_change_blocked=True,
                         crash_recovery_ok=True, disabled_fallback_ok=True)


def test_enablement_requires_all_security_gates() -> None:
    baseline = [_obs(path="single")]
    module = [_obs()]
    report = compare(baseline, module, simulated=True)

    assert enablement_verdict(report, gates=_all_gates(), real_evidence=False)["enabled"] is True
    bad = SecurityGates(overreach_blocked=False, interface_change_blocked=True,
                        crash_recovery_ok=True, disabled_fallback_ok=True)
    verdict = enablement_verdict(report, gates=bad, real_evidence=False)
    assert verdict["enabled"] is False and any("门禁" in r for r in verdict["reasons"])


def test_enablement_blocks_on_correctness_regression() -> None:
    baseline = [_obs(path="single", first_pass=True), _obs(path="single", first_pass=True)]
    module = [_obs(first_pass=False), _obs(first_pass=True)]  # 50% < 100%
    report = compare(baseline, module, simulated=True)

    verdict = enablement_verdict(report, gates=_all_gates(), real_evidence=False)
    assert verdict["enabled"] is False and any("回退" in r for r in verdict["reasons"])


def test_no_real_cost_claim_without_real_evidence() -> None:
    report = compare([_obs(path="single")], [_obs()], simulated=True)
    verdict = enablement_verdict(report, gates=_all_gates(), real_evidence=False)

    assert verdict["evidence_kind"] == EVIDENCE_SIMULATED
    assert "不作真实成本" in verdict["cost_claim"]
    # 真实证据时才允许费用对比结论
    real = enablement_verdict(report, gates=_all_gates(), real_evidence=True)
    assert real["evidence_kind"] == "REAL" and "真实费用" in real["cost_claim"]


def test_disabled_by_default_leaves_fallback_path(monkeypatch) -> None:
    monkeypatch.delenv("AGENTFORGE_MODULE_AGENTS_ENABLED", raising=False)
    assert module_agents_enabled() is False  # 原单执行器保留为可撤销回退路径
