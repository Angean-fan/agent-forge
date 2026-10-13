"""R27：9 次评估的汇总、阈值判定与报告渲染（纯函数回归）。

覆盖：7/9 门槛且校园成功才达标；失败/NOT_RUN 不计成功、缺失测试计失败；成功项目平均成本与“总费用/成功次数”
（成功为 0 记不可计算）；资产覆盖四层指标；路由断言；报告含全部必需小节。
"""

from __future__ import annotations

from agentforge_platform import model_gateway
from agentforge_platform.evaluation import (
    CASES,
    PASS_THRESHOLD,
    RunEvaluation,
    render_report,
    routing_assertions,
    summarize,
)


def _run(case: str, index: int, *, ok: bool, **kw) -> RunEvaluation:
    return RunEvaluation(
        case=case, index=index, run_id=index, status="SUCCEEDED" if ok else "FAILED",
        required_pass=ok, **kw,
    )


def _matrix(pass_cases: dict[str, int]) -> list[RunEvaluation]:
    runs = []
    for case in CASES:
        for index in range(1, 4):
            runs.append(_run(case, index, ok=index <= pass_cases.get(case, 0)))
    return runs


def test_threshold_requires_seven_and_campus_success() -> None:
    # 7/9 且校园成功 → 达标
    summary = summarize(_matrix({"campus": 3, "task": 3, "lending": 1}))
    assert summary["passed"] == 7 and summary["campus_passed"] and summary["goal_met"] is True

    # 6/9 → 未达标
    summary = summarize(_matrix({"campus": 3, "task": 2, "lending": 1}))
    assert summary["passed"] == 6 and summary["goal_met"] is False

    # 7/9 但校园失败 → 未达标
    summary = summarize(_matrix({"campus": 0, "task": 3, "lending": 3}))
    assert summary["passed"] == 6  # campus 0 + task 3 + lending 3 = 6
    assert summary["campus_passed"] is False and summary["goal_met"] is False


def test_failed_and_not_run_not_counted_as_success() -> None:
    runs = _matrix({"campus": 3, "task": 3, "lending": 0})
    runs[8] = RunEvaluation(case="lending", index=3, run_id=9, status="NOT_RUN", required_pass=False,
                            note="必需测试缺失/超时 → 计为失败")
    summary = summarize(runs)
    assert summary["passed"] == 6 and summary["not_run"] == 1 and summary["goal_met"] is False


def test_costs_and_uncomputable() -> None:
    runs = _matrix({"campus": 2, "task": 0, "lending": 0})
    for run in runs:
        run.cost_micro = 100 if run.required_pass else 50
    summary = summarize(runs)
    assert summary["avg_success_cost_micro"] == 100
    assert summary["total_cost_micro"] == 9 * 50 + 2 * 50  # 成功 2 个各 100，其余 7 个各 50
    assert summary["cost_per_success_micro"] == round(summary["total_cost_micro"] / 2)

    # 成功为 0 → 不可计算
    zero = summarize(_matrix({"campus": 0, "task": 0, "lending": 0}))
    assert zero["avg_success_cost_micro"] is None and zero["cost_per_success_micro"] is None


def test_four_layer_asset_metrics() -> None:
    runs = _matrix({"campus": 3, "task": 0, "lending": 0})
    runs[0].retrieval_candidates = 5
    runs[0].compatible_selected = 2
    runs[0].reuse_path = True
    runs[1].retrieval_candidates = 3  # 命中但无兼容/复用
    summary = summarize(runs)
    layers = summary["asset_layers"]
    assert layers["retrieval_hit_runs"] == 2
    assert layers["compatible_runs"] == 1
    assert layers["reused_runs"] == 1
    assert layers["acceptance_pass_runs"] == 3  # 仅验收通过算覆盖


def test_routing_assertions() -> None:
    routes = {role: (r.provider, r.model) for role, r in model_gateway.ROUTES.items()}
    assertions = routing_assertions(routes)
    assert all(item["ok"] for item in assertions)
    bad = routing_assertions({"user_interaction": ("openai", "gpt")})
    assert bad[0]["ok"] is False


def test_report_has_required_sections() -> None:
    summary = summarize(_matrix({"campus": 3, "task": 3, "lending": 1}))
    routes = {role: (r.provider, r.model) for role, r in model_gateway.ROUTES.items()}
    report = render_report(summary, routing=routing_assertions(routes),
                           generated_at="2026-10-13T00:00:00Z", runner_note="真实 Runner")
    for section in ("发布判定", "逐次结果", "费用与延迟", "分角色模型用量",
                    "资产覆盖四层指标", "模型路由断言", "可追溯证据"):
        assert section in report
    assert "达到文档建议验收目标" in report

    not_run = summarize([RunEvaluation(case=c, index=i, run_id=None, status="NOT_RUN",
                                       required_pass=False, note="runner 不可达")
                         for c in CASES for i in range(1, 4)])
    report2 = render_report(not_run, routing=routing_assertions(routes),
                            generated_at="x", runner_note="NOT_RUN")
    assert "未达到文档建议验收目标" in report2 and f"**0 / {sum(not_run['by_case'][c]['total'] for c in CASES)}**" in report2


def test_threshold_constant_is_seven_of_nine() -> None:
    assert PASS_THRESHOLD == 7
