# F04｜按模块实例化受限 DeepAgent：独立上下文、静态委派与受限 Patch

日期：2026-10-15
状态：**PASS**（确定性、零真实模型调用；脚本化引擎；默认关闭，未接入生成图）。

## 1. 基准与干净状态

| 项 | 值 |
|---|---|
| 基准 Commit | `64a8cca`（F03） |
| 结束 Commit | 见本文件所在提交 |
| 与来源文档编号对应 | AgentForge_03 §F04 |
| 依赖前置 | C02（受限引擎/账本/恢复）、F01（所有权）、F02（记忆）、F03（公共契约）；均已就绪 |

## 2. Goal 与判定

**Goal：** 在 R16/R17 单任务 patch engine 之上引入**按需逻辑模块 Agent**（`ModuleAgentFactory`/`ModuleExecutor`）：
为每个已分配任务加载**本模块**配置、专属记忆与**已批准公共契约**，用原有 DeepAgent 处理真实业务缺口。

**结论：** 上下文严格隔离（本模块记忆 + 他模块**已批准公共契约摘要**）；写入生效权限为
`task.allowed_paths ∩ module.owned_paths`；无缺口零编码调用；不自动提交；**默认关闭** → 回退既有单执行器。

## 3. 变更文件（真实路径）

**新增（本步**纯新增**，未改任何既有文件）**
- `platform-api/src/agentforge_platform/module_agent.py`：`ModuleContext`/`ModuleRunResult`；
  `build_module_context`/`render_module_prompt`/`ModuleExecutor`/`ModuleAgentFactory`/`module_agents_enabled`。
- `platform-api/tests/test_f04_module_agent.py` — 12 例。

## 4. 输入 / 输出契约

- **输入**：`ModuleRegistry`（F01）、`ModuleMemoryStore`（F02）、`InterfaceRegistry`（F03）、
  任务 `allowed_paths`、Gap、**注入的引擎工厂** `(allowed_paths, task_id) -> (engine, backend)`（生产由 C01 编码路由提供）。
- **输出**：`ModuleContext`（只读逻辑上下文）；`ModuleRunResult{module_id, invoked, coding_calls, allowed_paths, change_set}`。

## 5. 关键设计

- **逻辑隔离，共享物理**：不同模块 = 不同上下文/权限配置；不要求不同模型/进程（由注入工厂决定）。
- **静态委派**：调度权归 Python（`ModuleAgentFactory`/`ModuleExecutor`）；模块 Agent 不得自发启动其他 Agent、
  追加工具或宣布发布完成——提示词与工具表均受限。
- **上下文最小化**：仅本模块私有记忆 + 他模块 `VERIFIED` 公共契约**摘要**（`_readable_interfaces` 过滤 DRAFT/自身）；
  `build_module_context` 拒绝记忆/模块不匹配。
- **权限交集**：`effective_write_scope(task.allowed_paths, module.owned_paths)`；空交集或越权写入 → `FORBIDDEN`。
- **不自动提交**：`run` 仅产出 `FileChangeSet`（提交与验收归协调/复验环节）。
- **默认关闭**：`AGENTFORGE_MODULE_AGENTS_ENABLED` 未开启 → 生成图不引用本模块，旧路径零漂移。

## 6. 测试命令与逐项结果

`./scripts/ci_gate.sh` → **gate passed**

| 套件 | 命令 | 结果 |
|---|---|---|
| 后端全量 | `uv run --locked pytest` | **730 passed**（0 failed） |
| 后端静态 | `ruff check .` | All checks passed |
| 前端 | `npm ci + type-check + build + test` | **41 passed** |
| Runner | `pytest / ruff` | **30 passed, 2 skipped** |
| **F04 新增** | `pytest tests/test_f04_module_agent.py` | **12 passed** |
| MVP 基线（排除 E） | `pytest $(ls tests/test_*.py \| grep -v /test_e)` | **596 passed**（584 + F04 12） |
| E01—E10 | `pytest $(ls tests/test_e*.py)` | **134 passed**（不变） |

**FAIL：0。** NOT_RUN：真实模型编码调用（F04 用脚本化引擎；真实分支由 C01 开关/凭据控制）。

## 7. WhenDone 逐项证据

| WhenDone | 结论 | 证据 |
|---|---|---|
| 双模块 Prompt/可读上下文**互不包含对方私有记忆** | **满足** | `test_context_isolation_between_modules`、`test_prompt_contains_only_own_context`（prompt 无他模块私有 owned_paths）；`test_readable_interfaces_only_verified_from_others` |
| 文件越权被工具层拒绝 | **满足** | `test_out_of_scope_write_rejected`（越出交集 → `FORBIDDEN`）、`test_empty_intersection_blocked` |
| 模块无 Gap 时其编码请求数为零 | **满足** | `test_no_gap_makes_zero_coding_calls`（`invoked=False, coding_calls=0`，引擎未调用） |
| 真实 Gap 时仅对应模块发生受限 Patch/重验 | **满足** | `test_gap_uses_intersected_allowed_paths`（仅该模块、allowed ⊆ 本模块文件） |
| 每次模型与内部工具调用均可审计 | **满足（承接 C01/R16）** | 引擎工厂注入 C01 编码路由 → `run_id/task_id/call_id` 逐次入账（`module:<id>` 任务）；本步不自动提交 |
| 开关关闭回退旧路径 PASS | **满足** | `test_disabled_by_default`、`test_not_wired_into_generation_graph` |
| 中断/重试不重复提交代码或记忆 | **满足** | `test_repeat_run_is_deterministic_and_not_auto_committed`（重试结果一致、run 不落盘）；记忆幂等见 F02 |

## 8. 安全 / 费用影响

- **费用**：本步测试零真实调用（脚本化引擎）；真实路径继承 C01 的开关/凭据与限额。
- **安全**：上下文不含他模块私有记忆/源码；写入限交集；模块 Agent 无自发委派/追加工具；不自动提交、不自宣发布完成。

## 9. 遗留风险 / 待确认 / 下一步

1. **未接入生成图**：本步产出工厂/执行器；把模块执行编排进生成图（按 Gap 派发到对应模块、提交后复验）归 **F05/F06**。
2. **真实编码调用 —— NOT_RUN**：需 C01 的 `AGENTFORGE_CODING_ENABLED` + 凭据并授权；届时 `module:<id>` 任务将真实入账。
3. **MemoryView 契约版本**：`ModuleAgentFactory` 按 `contract_revision` 过滤记忆；跨 run 的契约修订由 F06 协调。
4. **下一步 F05**：Module Coordinator（跨模块依赖请求、接口变更与职责冲突协调）——依赖 F01—F04，推荐具备 C05 增量能力。
