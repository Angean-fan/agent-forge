# R13｜接口契约先行的多任务 DAG 规划

日期：2026-10-12
状态：完成（TaskPlan 契约 + 确定性 DAG 校验 + task_id 账本 + Qwen 路由；真实 Qwen 调用记 NOT_RUN）
前置：R12（`04327f4`）；迭代文档 D2 的 R13。

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：在需要 AI 规划时由 **Qwen3.7 Plus（`qwen3.7-plus`）** 基于**用户确认的冻结 spec** 提议接口/DTO/SQL/权限契约与 `TaskPlan`；
  Python 冻结、校验并决定最终任务 DAG；**规划前先执行 Asset Finder**，向模型只提供**已校验的候选/缺口摘要**，尽量避免重复规划可直接装配的常规任务。
  （本次确认：实现 DashScope 客户端+路由，真实调用 **NOT_RUN**；**仅在真实规划复杂性**时调用模型。）
- **Context**：依赖 R10—R12、M07 已确认 spec/hash 与 M14/M15；技术文档第 4 节定义 `TaskPlan.task_id`，第 8 节要求每次内部请求继承 `run_id/task_id/call_id`；
  由 R06 的 DeepSeek 用户摘要交接，**不得读取未确认原始聊天**；计划须引用 R06D 选定资产与冻结索引快照。
- **Constraints**：新增可空 `model_call.task_id` 的**向后兼容迁移**，并从 DAG 任务上下文经网关预留/结算传递；
  `task_id` 必须属于该 `run_id`，无任务上下文的 run 级调用保持空值，生成前 `project_id + draft_id` 调用的 `run_id/task_id` 均为空，旧行无需回填；
  静态任务继续确定性转换，**只有确有规划复杂性才调用 `technical_planning`**；模型只能提议，不得改 confirmed spec/权限/验收/依赖版本；
  **Python 做 DAG 无环、冲突和覆盖校验**；Qwen 的方法建议不得越过兼容筛选。
- **WhenDone**：同一冻结 spec/hash 的计划具备可追溯输出、输入摘要 hash 与 schema 验证；无环、越界 `allowed_paths`、同路径冲突被拒；
  用已登记 `RunTask` 测试任务内网关调用，账本行同时含正确 `run_id/task_id/call_id`，错误 run/task 配对被拒，迁移前旧账本行及生成前账本仍可读取；
  DAG 建立前的规划调用保留空 `task_id`；模型日志仅出现允许的 `technical_planning` 路由，TaskPlan 标注执行模式与检索拒绝原因，已覆盖任务可跳过技术模型。

## 输入 / 输出契约

- **监听**：`model_call` 新增可空 `task_id`（迁移 `0011`）；`reserve/invoke` 透传并校验：`task_id` 必须属于该 `run_id`（否则 422），`task_id` 无 `run_id` 即拒绝。
- **路由**：`technical_planning`（DashScope `qwen3.7-plus`）默认**禁用**，仅当 `AGENTFORGE_TECHNICAL_PLANNING_ENABLED=1` 时可用；否则 `MODEL_ROLE_DISABLED`。
- **`TaskPlan`**（`task_plan.py`）：`{plan_version, execution_mode(deterministic|planned), input_digest_hash, tasks:[{task_id,operation,depends_on,allowed_paths,asset_refs,acceptance_ids,mode}], retrieval_reasons, uncovered_acceptance_ids}`。
- **`validate_plan`**：task_id 唯一/依赖存在/无环、`allowed_paths` 相对安全、无同路径冲突、`asset_refs` 必须在冻结锁内、`mode` 合法。
- **规划器**（`task_planning.py`）：`needs_planning`（未覆盖验收/显式方法请求/关系>1）→ 否则**确定性转换**；
  `input_digest` 由 spec_hash+contract_hash+query_hash+index_revision+候选+未覆盖派生；模型仅返回 `tasks`，`execution_mode/digest` 由 Python 决定；
  路由禁用/调用失败/校验失败 → **回退确定性计划并记录原因**。
- **生成图**：新增 `plan_tasks` 节点（Finder 之后、装配之前），产出 `task-plan.json`（随交付包）+ `task_plan` 事件；规划调用在 DAG 建立前，`task_id` 留空。

## 变更文件

```
platform-api/
├── migrations/versions/0011_model_call_task_id.py   # model_call.task_id（可空）
├── src/agentforge_platform/
│   ├── models.py            # + ModelCall.task_id
│   ├── model_gateway.py     # reserve/invoke 透传 task_id + _require_task_context + _role_enabled
│   ├── clients.py           # + DashScopeClient（OpenAI 兼容）
│   ├── task_plan.py         # 新增：TaskPlan/TaskSpec + validate_plan + is_safe_task_path
│   ├── task_planning.py     # 新增：needs_planning/deterministic_plan/model_plan/plan_tasks/input_digest
│   └── generation_graph.py  # + plan_tasks 节点、task-plan.json
├── tests/test_migrations.py # head=0011、task_id 列与回退步
└── tests/test_r13_task_planning.py  # 新增：7 例
```

## 验证（实测）

`./scripts/ci_gate.sh` → `==> gate passed`

| 目标 | 命令 | 结果 |
|---|---|---|
| 平台后端 | `uv run --locked pytest` | **372 passed**（R12 后 365，本步 +7） |
| 平台后端静态 | `ruff check .` | All checks passed |
| 平台前端 | `npm ci + type-check + build + test` | 36 passed |
| 独立 Runner | `pytest / ruff` | 28 passed, 2 skipped；All checks passed |
| **真实 Qwen 规划调用** | — | **NOT_RUN**（无 `DASHSCOPE_API_KEY`，按约定不猜供应商配置） |

生成 `task-plan.json`（`execution_mode=deterministic`、`input_digest_hash`、`retrieval_reasons.technical_planning=["MODEL_ROLE_DISABLED"]`、未覆盖验收 4 项）。

## WhenDone 逐项判定

| 判据 | 结果 | 依据 |
|---|---|---|
| 计划可追溯输出 + 输入摘要 hash + schema 验证 | ✅ PASS | `input_digest`；`TaskPlan` schema；`test_needs_planning_and_deterministic_digest` |
| 无环 / 越界 allowed_paths / 同路径冲突被拒 | ✅ PASS | `test_validate_plan_rejects_cycle_path_escape_conflict_and_unknown_asset` |
| 任务内网关调用账本含正确 run_id/task_id/call_id；错误配对被拒 | ✅ PASS | `test_gateway_records_task_id_and_rejects_bad_pairing` |
| 迁移前旧账本行与生成前账本仍可读（task_id NULL） | ✅ PASS | `task_id` 可空迁移（0011 往返）+ 生成前调用 `run_id/task_id` 均为空 |
| DAG 建立前规划调用保留空 task_id | ✅ PASS | `model_plan` 传 `task_id=None` |
| 仅允许 `technical_planning` 路由；已覆盖任务可跳过技术模型 | ✅ PASS | 路由 env 门控；`needs_planning=False` → 确定性（`plan_tasks` 短路） |
| TaskPlan 标注执行模式与检索拒绝原因 | ✅ PASS | `execution_mode` + `retrieval_reasons`（含 `MODEL_ROLE_DISABLED`/`PLAN_INVALID`） |
| 真实 Qwen 规划 | ⚠️ **NOT_RUN** | 无 DashScope key（如实记录） |

## 风险 / 下一步

- **真实 Qwen 未跑**：DashScope 客户端/路由就绪但真实调用 **NOT_RUN**；启用前需 `DASHSCOPE_API_KEY` + 工具调用探针（R16 同类要求）。
- **DAG 执行仍确定性**：`plan_tasks` 记录计划与审计，但装配/验证执行链未按模型 DAG 动态重排（D3 的受限 Agent 补缺在 R14+）。
- **需要规划的场景**：由于生成的工程选中的方法单元通常为空 → 未覆盖验收非空 → 多数 spec 会“需要规划”并走回退；待 R14+ 引入真实缺口任务后语义更完整。
- **下一步 R14**：实现能力覆盖检查与 Gap Task 生成（在 D3 之下，只有真实未覆盖规则才产生结构化 Gap）。
