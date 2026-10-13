"""R27：正式 9 次独立生成评估的**汇总与报告**（纯函数，不含 IO/网络）。

- 分层统计：质量（首过/修复后过/失败/NOT_RUN）、费用（成功项目平均成本、总费用/成功次数）、延迟、资产覆盖四层
  （检索命中 → 兼容通过 → 实际复用 → 验收通过）；**关键词命中不算可复用覆盖**。
- 发布判定：9 次中 ≥7 次通过全部必需检查 **且** 校园活动成功，才标“达到文档建议验收目标”；否则如实标注。
- 失败/NOT_RUN 不伪造：缺失或超时的必需测试计为失败。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

PASS_THRESHOLD = 7
TOTAL_RUNS = 9
CASES = ("campus", "task", "lending")
RUNS_PER_CASE = 3
CAMPUS = "campus"


@dataclass
class RunEvaluation:
    """单次运行的评估记录（由驱动脚本填充）。"""

    case: str
    index: int
    run_id: int | None
    status: str  # SUCCEEDED | FAILED | NOT_RUN | PAUSED_BUDGET | CANCELLED
    required_pass: bool  # 全部必需检查通过且验收通过
    duration_seconds: float | None = None
    error_code: str | None = None
    passed_first_try: bool | None = None  # 首次通过 vs 修复后通过
    repairs: int = 0
    agent_calls: int = 0  # coding / complex_repair 实际调用量
    reuse_path: bool = False  # 复用路径（含 reuse 决策或方法锁定）
    patch_path: bool = False
    index_revision: str | None = None
    locked_method_units: list[str] = field(default_factory=list)
    rejected_reasons: dict[str, list[str]] = field(default_factory=dict)
    uncovered_acceptance_ids: list[str] = field(default_factory=list)
    retrieval_candidates: int = 0
    compatible_selected: int = 0
    pack_retrieval_candidates: int = 0
    pack_compatible_selected: int = 0
    cost_micro: int = 0
    unknown_calls: int = 0
    usage_by_role: dict[str, dict] = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    verification_overall: str | None = None
    acceptance_overall: str | None = None
    note: str | None = None  # NOT_RUN 等的原因（如实记录）
    artifact_dir: str | None = None  # 相对共享根的唯一目录，或测试中的绝对目录
    acceptance_cases: list[dict] = field(default_factory=list)
    locked_capability_packs: list[str] = field(default_factory=list)
    model_calls: list[dict] = field(default_factory=list)
    first_failure: dict | None = None


def _is_pass(run: RunEvaluation) -> bool:
    return run.status == "SUCCEEDED" and run.required_pass


def summarize(runs: list[RunEvaluation]) -> dict:
    """汇总指标 + 发布判定；全程只读、确定性。"""
    passed = [r for r in runs if _is_pass(r)]
    failed = [r for r in runs if r.status == "FAILED" or (r.status == "SUCCEEDED" and not r.required_pass)]
    not_run = [r for r in runs if r.status in ("NOT_RUN", "PAUSED_BUDGET", "CANCELLED")]
    first_try = sum(_is_pass(r) and r.repairs == 0 for r in runs)
    repaired = sum(_is_pass(r) and r.repairs > 0 for r in runs)
    failure_distribution: dict[str, int] = {}
    for run in runs:
        if _is_pass(run):
            continue
        code = run.error_code or (
            "REQUIRED_EVIDENCE_MISSING" if run.status == "SUCCEEDED" else run.status
        )
        failure_distribution[code] = failure_distribution.get(code, 0) + 1
    campus_runs = [r for r in runs if r.case == CAMPUS]
    campus_ok = any(_is_pass(r) for r in campus_runs)

    total_cost = sum(r.cost_micro for r in runs)
    success_cost = sum(r.cost_micro for r in passed)
    avg_success_cost = round(success_cost / len(passed)) if passed else None
    cost_per_success = round(total_cost / len(passed)) if passed else None

    durations = [r.duration_seconds for r in runs if r.duration_seconds is not None]
    by_case = {
        case: {
            "total": sum(1 for r in runs if r.case == case),
            "passed": sum(1 for r in runs if r.case == case and _is_pass(r)),
        }
        for case in CASES
    }

    # 四层资产指标：检索命中 → 兼容通过 → 实际复用 → 验收通过（关键词命中不计）
    layers = {
        "retrieval_hit_runs": sum(r.retrieval_candidates > 0 or r.pack_retrieval_candidates > 0
                                  for r in runs),
        "compatible_runs": sum(r.compatible_selected > 0 or r.pack_compatible_selected > 0
                               for r in runs),
        "reused_runs": sum(r.reuse_path or bool(r.locked_capability_packs) for r in runs),
        "acceptance_pass_runs": len(passed),
    }
    method_layers = {
        "retrieval_hit_runs": sum(r.retrieval_candidates > 0 for r in runs),
        "compatible_runs": sum(r.compatible_selected > 0 for r in runs),
        "reused_runs": sum(bool(r.locked_method_units) for r in runs),
        "acceptance_pass_runs": sum(_is_pass(r) and bool(r.locked_method_units) for r in runs),
    }
    # Capability Pack 的候选/兼容/复用证据来自确定性 Resolver 的资产锁。
    # 锁中没有的包不能按关键词命中计数。
    pack_layers = {
        "retrieval_hit_runs": sum(r.pack_retrieval_candidates > 0 for r in runs),
        "compatible_runs": sum(r.pack_compatible_selected > 0 for r in runs),
        "reused_runs": sum(bool(r.locked_capability_packs) for r in runs),
        "acceptance_pass_runs": sum(_is_pass(r) and bool(r.locked_capability_packs) for r in runs),
    }

    usage_by_role: dict[str, dict] = {}
    for run in runs:
        for role, usage in run.usage_by_role.items():
            bucket = usage_by_role.setdefault(role, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                                     "cost_micro": 0, "unknown_calls": 0})
            bucket["calls"] += usage.get("calls", 0)
            bucket["input_tokens"] += usage.get("input_tokens", 0)
            bucket["output_tokens"] += usage.get("output_tokens", 0)
            bucket["cost_micro"] += usage.get("cost_micro", 0)
            bucket["unknown_calls"] += usage.get("unknown_calls", 0)

    complete_matrix = (
        len(runs) == TOTAL_RUNS
        and all(sum(1 for r in runs if r.case == case) == RUNS_PER_CASE for case in CASES)
        and all({r.index for r in runs if r.case == case} == set(range(1, RUNS_PER_CASE + 1))
                for case in CASES)
    )
    goal_met = complete_matrix and len(passed) >= PASS_THRESHOLD and campus_ok
    return {
        "total": len(runs),
        "passed": len(passed),
        "failed": len(failed),
        "not_run": len(not_run),
        "first_try_passed": first_try,
        "repaired_passed": repaired,
        "failure_distribution": dict(sorted(failure_distribution.items())),
        "pass_threshold": PASS_THRESHOLD,
        "campus_passed": campus_ok,
        "goal_met": goal_met,
        "complete_matrix": complete_matrix,
        "by_case": by_case,
        "avg_success_cost_micro": avg_success_cost,
        "total_cost_micro": total_cost,
        "cost_per_success_micro": cost_per_success,
        "avg_duration_seconds": round(sum(durations) / len(durations), 1) if durations else None,
        "asset_layers": layers,
        "method_unit_layers": method_layers,
        "capability_pack_layers": pack_layers,
        "usage_by_role": usage_by_role,
        "runs": runs,
    }


def routing_assertions(routes: dict[str, tuple[str, str]]) -> list[dict]:
    """模型路由断言：``role -> (provider, model)`` 与期望表逐项核对。"""
    expected = {
        "user_interaction": ("deepseek_official", "deepseek-flash"),
        "technical_planning": ("dashscope", "qwen3.7-plus"),
        "coding": ("dashscope", "qwen3-coder-flash"),
        "complex_repair": ("dashscope", "qwen3.8-max"),
    }
    return [
        {"role": role, "expected": list(pair), "actual": list(routes.get(role, ("?", "?"))),
         "ok": routes.get(role) == pair}
        for role, pair in expected.items()
    ]


def _fmt_micro(value: int | None) -> str:
    return "不可计算" if value is None else f"{value} 微元（≈{value / 1_000_000:.4f} 元）"


def _run_row(run: RunEvaluation) -> str:
    verdict = "PASS" if _is_pass(run) else ("FAIL" if run.status in ("FAILED", "SUCCEEDED") else run.status)
    loc = f"run #{run.run_id}" if run.run_id is not None else "—"
    dur = f"{run.duration_seconds:.1f}s" if run.duration_seconds is not None else "—"
    return (f"| {run.case} | {run.index} | {loc} | {verdict} | {dur} | "
            f"{run.error_code or '—'} | {run.repairs} | {run.agent_calls} | {run.note or '—'} |")


def render_report(summary: dict, *, routing: list[dict], generated_at: str,
                  runner_note: str) -> str:
    """渲染 ``docs/DEMO_P0_ACCEPTANCE.md``（逐项 PASS/FAIL/NOT_RUN；不达标如实标注）。"""
    lines = [
        "# Demo P0 验收报告（R27｜正式 9 次独立生成评估）",
        "",
        f"生成时间：{generated_at}",
        f"运行环境说明：{runner_note}",
        "",
        "## 发布判定",
        "",
        f"- 通过（全部必需检查 PASS）次数：**{summary['passed']} / {summary['total']}**（门槛 ≥ {summary['pass_threshold']}）",
        f"- 校园活动成功：**{'是' if summary['campus_passed'] else '否'}**",
        f"- 结论：**{'达到文档建议验收目标' if summary['goal_met'] else '未达到文档建议验收目标（如实标注）'}**",
        (f"- 首次通过：{summary['first_try_passed']}；修复后通过：{summary['repaired_passed']}；"
         f"未通过：{summary['failed']}；NOT_RUN/暂停/取消：{summary['not_run']}"),
        "",
        "## 失败分布",
        "",
        "| 失败码或状态 | 次数 |",
        "|---|---:|",
        *([f"| {code} | {count} |" for code, count in summary["failure_distribution"].items()]
          or ["| 无 | 0 |"]),
        "",
        "## 逐次结果",
        "",
        "| 用例 | 第几次 | run | 判定 | 耗时 | 失败码 | 修复次数 | Agent 调用 | 说明 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [_run_row(run) for run in summary["runs"]]
    layers = summary["asset_layers"]
    lines += [
        "",
        "## 费用与延迟",
        "",
        f"- 成功项目平均成本：{_fmt_micro(summary['avg_success_cost_micro'])}",
        f"- 所有尝试总费用 / 成功次数：{_fmt_micro(summary['cost_per_success_micro'])}（总费用 {_fmt_micro(summary['total_cost_micro'])}）",
        f"- 平均耗时：{'—' if summary['avg_duration_seconds'] is None else str(summary['avg_duration_seconds']) + 's'}",
        "",
        "## 分角色模型用量（DeepSeek 交互 / Qwen 规划·编码·升级）",
        "",
        "| 角色 | 调用 | 输入 tokens | 输出 tokens | 成本 | 未知 |",
        "|---|---|---|---|---|---|",
    ]
    for route in routing:
        role = route["role"]
        usage = summary["usage_by_role"].get(role, {})
        lines.append(f"| {role} | {usage.get('calls', 0)} | {usage.get('input_tokens', 0)} | "
                     f"{usage.get('output_tokens', 0)} | {usage.get('cost_micro', 0)} | "
                     f"{usage.get('unknown_calls', 0)} |")
    lines += [
        "",
        "## 资产覆盖四层指标（关键词命中不算可复用覆盖）",
        "",
        f"- 检索命中 run 数：{layers['retrieval_hit_runs']}",
        f"- 兼容通过 run 数：{layers['compatible_runs']}",
        f"- 实际复用 run 数：{layers['reused_runs']}",
        f"- 验收通过 run 数：{layers['acceptance_pass_runs']}",
        "",
        "| 资产类型 | 检索命中 run | 兼容通过 run | 实际复用 run | 验收通过 run |",
        "|---|---:|---:|---:|---:|",
        ("| Method Unit | " + " | ".join(str(summary["method_unit_layers"][key]) for key in
         ("retrieval_hit_runs", "compatible_runs", "reused_runs", "acceptance_pass_runs")) + " |"),
        ("| Capability Pack | " + " | ".join(str(summary["capability_pack_layers"][key]) for key in
         ("retrieval_hit_runs", "compatible_runs", "reused_runs", "acceptance_pass_runs")) + " |"),
        "",
        "> 检索与兼容层取自 asset-search 候选审计；实际复用取自资产锁；验收通过需同时满足复用和全部必需检查。",
        "",
        "## 各用例汇总",
        "",
        "| 用例 | 次数 | 通过 |",
        "|---|---|---|",
    ]
    for case, stats in summary["by_case"].items():
        lines.append(f"| {case} | {stats['total']} | {stats['passed']} |")
    lines += [
        "",
        "## 模型路由断言",
        "",
        "| 角色 | 期望(provider, model) | 实际(provider, model) | 结果 |",
        "|---|---|---|---|",
    ]
    for item in routing:
        lines.append(f"| {item['role']} | {tuple(item['expected'])} | {tuple(item['actual'])} "
                     f"| {'OK' if item['ok'] else 'MISMATCH'} |")
    lines += [
        "",
        "## 可追溯证据（每次 run）",
        "",
        "| 用例 | 第几次 | 运行目录 | 索引快照 | 已锁方法 | 被拒资产数 | 复用:Patch |",
        "|---|---|---|---|---|---|---|",
    ]
    for run in summary["runs"]:
        path = ("复用" if run.reuse_path else "") + ("+Patch" if run.patch_path else "") or "确定性装配"
        lines.append(f"| {run.case} | {run.index} | `{run.artifact_dir or '—'}` | "
                     f"{run.index_revision or '—'} | "
                     f"{len(run.locked_method_units)} | {len(run.rejected_reasons)} | {path} |")
    lines += ["", "## 每次 run 的逐项证据", ""]
    for run in summary["runs"]:
        lines += [
            f"### {run.case} 第 {run.index} 次",
            "",
            f"- 运行目录：`{run.artifact_dir or '—'}`；run ID：{run.run_id if run.run_id is not None else '—'}",
            (f"- 状态：{run.status}；全部必需项通过：{'是' if _is_pass(run) else '否'}；"
            f"首次通过：{'未知' if run.passed_first_try is None else ('是' if run.passed_first_try else '否')}；"
            f"修复次数：{run.repairs}；Agent 调用：{run.agent_calls}"),
            (f"- 构建：{run.verification_overall or 'NOT_RUN'}；HTTP 验收：{run.acceptance_overall or 'NOT_RUN'}；"
            f"耗时：{run.duration_seconds if run.duration_seconds is not None else '—'}s；"
            f"已结算成本：{_fmt_micro(run.cost_micro)}；未知调用：{run.unknown_calls}"),
            (f"- 索引快照：`{run.index_revision or '—'}`；"
            f"已锁 Method Unit：{', '.join(run.locked_method_units) or '无'}；"
            f"已锁 Capability Pack：{', '.join(run.locked_capability_packs) or '无'}"),
            f"- 检索候选未声明覆盖的验收 ID：{', '.join(run.uncovered_acceptance_ids) or '无'}",
            (f"- 候选 Method Unit：{run.retrieval_candidates}；兼容通过：{run.compatible_selected}；"
             f"候选 Capability Pack：{run.pack_retrieval_candidates}；兼容通过：{run.pack_compatible_selected}；"
             f"Method Unit 复用路径：{'是' if run.reuse_path else '否'}；"
             f"Patch：{'是' if run.patch_path else '否'}"),
            f"- 首次失败快照：`{json.dumps(run.first_failure, ensure_ascii=False) if run.first_failure else '无'}`",
            f"- 说明：{run.note or '—'}",
            "",
            "| 必需构建检查 | 结果 | 失败码 |",
            "|---|---|---|",
        ]
        lines += [f"| {item.get('name', '—')} | {item.get('status', 'NOT_RUN')} | "
                  f"{item.get('error_code') or '—'} |" for item in run.checks]
        if not run.checks:
            lines.append("| — | NOT_RUN | — |")
        lines += ["", "| 固定业务验收 | 结果 | 失败码 |", "|---|---|---|"]
        lines += [f"| {item.get('name', '—')} | {item.get('status', 'NOT_RUN')} | "
                  f"{item.get('error_code') or '—'} |" for item in run.acceptance_cases]
        if not run.acceptance_cases:
            lines.append("| — | NOT_RUN | — |")
        lines += ["", "| 模型角色 | Provider / 模型 | 思考模式 / 强度 | 输入 / 输出 tokens | 成本（微元） | 状态 |",
                  "|---|---|---|---|---|---|"]
        lines += [
            f"| {call.get('role', '—')} | {call.get('provider', '—')} / {call.get('model', '—')} | "
            f"{call.get('thinking_mode') or '—'} / {call.get('reasoning_effort') or '—'} | "
            f"{call.get('input_tokens') or 0} / {call.get('output_tokens') or 0} | "
            f"{call.get('cost_micro') if call.get('cost_micro') is not None else 'UNKNOWN'} | "
            f"{call.get('status', '—')} |" for call in run.model_calls
        ]
        if not run.model_calls:
            lines.append("| — | — | — | 0 / 0 | 0 | 无调用 |")
        lines += ["", "| 被拒资产 | 原因 |", "|---|---|"]
        lines += [f"| {ref} | {', '.join(reasons)} |" for ref, reasons in run.rejected_reasons.items()]
        if not run.rejected_reasons:
            lines.append("| — | 无 |")
        lines.append("")
    lines += [
        "",
        "> 说明：本报告只反映**真实运行**结果；缺失或超时的必需测试计为失败；未达标如实标注，不伪造 PASS。",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "CAMPUS",
    "CASES",
    "PASS_THRESHOLD",
    "RUNS_PER_CASE",
    "TOTAL_RUNS",
    "RunEvaluation",
    "render_report",
    "routing_assertions",
    "summarize",
]
