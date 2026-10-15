# MODULE_AGENT_ACCEPTANCE｜模块自治对照评估与启用门槛（F07）

日期：2026-10-15
结论：**确定性门禁全部通过；启用判定为"可在授权环境灰度启用"，但证据类型为 SIMULATED（真实模型/Runner NOT_RUN），
因而不作真实成本/收益结论。** 默认仍**关闭**（`module_agents_enabled=false`），原单执行器保留为可撤销回退路径。

> **不改写 R27**：本文档独立于《DEMO_P0_ACCEPTANCE.md》的九次独立生成评估与 **≥7/9** 门槛；两者判据不同，互不覆盖。

## 1. 方法

- **固定边界**：同一冻结规格（`schemas/campus-activity.spec.json`，双模块 Activity/Enrollment）、同一 Profile
  （`demo-ts-mp`）、同一受保护验收、同一预算边界。
- **两条路径**：
  - **单执行器（baseline）**：一次广泛授权、无模块边界；
  - **模块 Agent**：每模块独立受限执行（F01 所有权 ∩ 任务授权），越权被工具层拒绝。
- **约束**：本机模型/受控 Runner 不可用 → 以**脚本化引擎**驱动 F01/F04 真实组件（**模拟**），
  模型调用/Token/费用未真实发生（记 0/不作结论）。

## 2. 对照组（模拟，各 2 次重复）

| 指标 | 单执行器 | 模块 Agent | 说明 |
|---|---:|---:|---|
| 首轮通过率 | 1.0 | 1.0 | 等价 |
| 修复后通过率 | 1.0 | 1.0 | 等价 |
| **跨模块写** | **2** | **0** | 模块边界收敛（对照点） |
| **越权拦截** | 0 | 0（本场景无越权尝试） | 越权负例见 §4 |
| 模型调用 | 2 | **4** | 模块路径每模块一次（**开销更高**，如实记录） |
| 未覆盖验收 ID | 无 | 无 | — |
| 接口复用 | 0 | 0 | 本场景无跨模块接口消费 |
| 重放一致性 | 一致 | 一致 | 指纹一致 |

**如实结论**：在**同等正确性**下，模块路径**消除了跨模块写**（隔离收益），代价是**模型调用数增加**（≈×模块数）。
真实 Token/费用/耗时因模型不可用**未测**，**不声称**成本节省或回归。

## 3. 观测字段（`simulate_*` / `RunObservation`）

`first_pass / repaired_pass / asset_reuse{exact,parametric,structured} / uncovered_acceptance_ids /
interface_reuse / overreach_blocked / cross_module_writes / model_calls / input_tokens / output_tokens /
cost_micro / unknown_calls / duration_s / failure_reason / fingerprint(重放)`。

## 4. 硬性安全/正确性门禁（证据关联的负例测试）

| 门禁 | 证据 | 结果 |
|---|---|---|
| **越权写入被拒** | `test_f04_module_agent.py::test_out_of_scope_write_rejected`、`::test_empty_intersection_blocked`；`test_f07_module_evaluation.py::test_module_path_blocks_overreach` | PASS |
| **破坏性接口变更被拒/可迁移** | `test_f03_interface_registry.py::test_breaking_upgrade_is_blocked_then_allowed_with_migration`、`::test_non_owner_cannot_overwrite_or_deprecate` | PASS |
| **跨模块一致性** | `test_f06_module_integration.py::test_interface_type_conflict_fails_run`（整体 FAIL 而非"局部绿"） | PASS |
| **崩溃/重放可恢复** | `test_f06_*::test_reconcile_detects_code_committed_but_interface_missing`、`::test_rollback_deprecates_recorded_interfaces`、`test_f04_*::test_repeat_run_is_deterministic_and_not_auto_committed` | PASS |
| **禁用回退** | `test_f04_*::test_disabled_by_default`、`::test_not_wired_into_generation_graph`、`test_f07_*::test_disabled_by_default_leaves_fallback_path` | PASS |

**专用安全性**：`test_f05_module_coordinator.py::test_no_model_coupling`（协调器非路由决策模型）、
`test_f06_*::test_verification_binding_stale_on_source_or_contract_change`（源码/契约变化使旧验证失效）。

## 5. 启用判定（`enablement_verdict`）

- **判定逻辑**：安全门禁**全过** ∧ 模块路径**不回退**正确性 ∧ 重放一致 → `enabled=True`。
- **本场景判定**：`enabled=True`（模拟证据）；`evidence_kind=SIMULATED`；
  `cost_claim="模型不可用/未授权：不作真实成本节省结论（标记 SIMULATED/NOT_RUN）"`。
- **启用动作**：仅在**授权测试环境**置 `AGENTFORGE_MODULE_AGENTS_ENABLED=1`；**默认关闭**，回退路径（单执行器）保留。

## 6. 复现命令

```bash
cd platform-api
uv run --locked pytest tests/test_f07_module_evaluation.py -v
# 对照摘要（模拟）
uv run --locked python - <<'PY'
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.module_registry import derive_modules
from agentforge_platform.module_evaluation import compare, simulate_single_path, simulate_module_path
# 见 tests/test_f07_module_evaluation.py 的脚手架
PY
```

## 7. NOT_RUN（需开发者条件）

- **真实模型对照**：需 `AGENTFORGE_CODING_ENABLED` + 凭据并授权；届时可产出真实 Token/费用/耗时并重跑本判定。
- **真实 Runner 完整 HTTP 闭环**：本机 Runner 不可达（承接 C02/C03 记录）。
- **重复次数**：本对照为 2 次确定性重放；**真实**统计显著的重复次数须先经开发者按预算确认（不虚报显著性）。

## 8. 遗留风险

1. 本对照为**模拟**，不能替代真实成本/收益结论；启用前建议在授权环境补跑真实对照。
2. 模块路径**模型调用数随模块数增加**；是否值得取决于隔离收益与真实成本，需真实数据判断。
3. F 阶段**未接入生成图**（F04/F06 门禁尚未接 `run_generation` 终态）；接入属后续工作。
