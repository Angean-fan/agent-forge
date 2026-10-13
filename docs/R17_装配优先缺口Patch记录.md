# R17｜实现“装配优先，缺口局部 Patch”

日期：2026-10-13
状态：完成（缺口决策 + 受限 Patch 提交 + 装配优先复用；生成图接入并在变更后复验）
前置：R13/R14/R15/R16（`514addc`/`530fc4d`/`c36c69a`/`b25f08c`）；迭代文档 D3 的 R17；技术文档第 3 节 `gap_check → patch → commit → verify`、第 6 节 TaskContext。

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：把 R14 的真实缺口交给 R16 受限引擎，变更校验通过后按 R15 **原子提交**；若存在**经验证、可安全装配**的方法单元，则**优先复用装配**，不因 Planner 提议新写就直接调用 Agent。
- **Context**：依赖 R13—R16；`gap_check → patch → commit → verify`；TaskContext 含冻结规格片段、契约、依赖与授权文件。
- **Constraints**：Agent 只消费 confirmed spec / 冻结 contract / `allowed_paths`（**不接触 DeepSeek 对话历史**）；不可扩大需求、改模板原件、升级依赖或重写模块；**不提供固定测试写权限**；未通过 schema/文件校验**不提交**；`NO_MATCH`/契约权限不符/扩展点不可用才允许 Patch；**模型输出不自动登记为新 Method Unit**；每次 Patch 与**检索轨迹互相关联**。
- **WhenDone**：① 受控缺口由 Agent 小范围补全、变更集可审查且测试 PASS；② 标准 Task 无缺口时**不启动 Agent**；③ 补充经审核兼容方法资产后重跑，在**无 Coding Agent** 下复用该资产并通过。

## 输入 / 输出契约

- **`choose_gap_action(gap, search, methods_root) -> GapDecision`**（`gap_patch.py`）：
  - `reuse`：`search.selected_refs` 中存在**兼容且声明该验收**的方法单元（装配优先）；
  - `refuse`：缺口 `allowed_paths` 为空（无可安全工作扩展点）→ 保持缺口；
  - `patch`：其余（有扩展点、无兼容方法）。
- **`build_task_context(gap, spec, contract, lock, *, run_dir) -> dict`**：冻结上下文（规格片段/契约/授权文件/已锁资产/禁止项），**不含对话历史**；`render_task_prompt` 确定性渲染。
- **`validate_change_set(change_set, *, allowed_paths)`**：提交前确定性校验（路径安全、在 allowed 内、动作/数量合法）；非法即抛错**不提交**。
- **`apply_gap_patch(engine, file_backend, gap, context, *, search) -> dict`**：`engine.run` → 校验 → `FileBackend.commit()`（R15 原子提交）；返回 `change_set`（可审查）与 `correlation`（query_hash/候选/拒绝原因）。
- **`resolve_gaps(gaps, ..., engine_factory)`**：逐缺口执行 reuse/refuse/patch；`engine_factory(allowed_paths, task_id)` 按缺口绑定账本上下文。
- **生成图**：新增 `resolve_gaps` 节点（`check_coverage` 之后、`package_or_fail` 之前）；**默认关闭**（`gap_engine_factory=None`）时无缺口即零副作用；有变更后**重新验证 + 重算覆盖**，交付包只反映最后一次验证。

## 变更文件

```
platform-api/src/agentforge_platform/
├── gap_patch.py            # 新增：缺口决策 + TaskContext + 变更集校验 + reuse 增量装配 + resolve_gaps
└── generation_graph.py     # + resolve_gaps 节点、GenerationContext.gap_engine_factory、文档串
platform-api/tests/test_r17_gap_patch.py   # 新增：10 例
```

## 设计

- **装配优先发生在 coverage 之后（缺口已知时）**：`assemble` 会整体重建 run 目录，故「回到 Resolver 执行该资产」实现为**按装配器的方法单元 COPY 语义增量装入**（`assemble_reuse_method`：写入方法 `target_dir`、拒绝覆盖既有文件与符号链接、路径安全）——不整体重建、不触发 Agent。
- **仅真实缺口进入 Patch**：`NO_MATCH`/契约权限不符/扩展点不可用 → 受控 Patch；无兼容方法且无扩展点 → `refuse`，**不伪装覆盖**。
- **变更必须复验**：任何 reuse/patch 变更后重跑 `verify`+`accept` 并重算 `analyze_coverage`；复用方法并入覆盖率重算的锁视图（`covered` 计入其声明验收）。
- **上下文最小且冻结**：TaskContext 由平台从**已确认 spec/冻结 contract/锁/授权目录**构造，**不传对话历史**；提示词确定性可复现。
- **不自动登记资产**：Patch 结果只落变更集，**不写入方法仓库**；`correlation` 与检索轨迹（R06C）互相关联留痕。

## 验证（实测）

`./scripts/ci_gate.sh` → `==> gate passed`

| 目标 | 命令 | 结果 |
|---|---|---|
| 平台后端 | `uv run --locked pytest` | **410 passed**（R16 后 400，本步 +10） |
| 平台后端静态 | `ruff check .` | All checks passed |
| 平台前端 | `npm ci + type-check + build + test` | 36 passed |
| 独立 Runner | `pytest / ruff` | 28 passed, 2 skipped；All checks passed |

说明：R17 编排用例用**脚本化模型**驱动 **R16 已实测的受限引擎**（R16 探针已在北京地域验证真实工具调用/暂存/入账），避免重复计费调用。

## WhenDone 逐项判定

| 判据 | 结果 | 依据 |
|---|---|---|
| ① 受控缺口由 Agent 小范围补全、变更集可审查 | ✅ PASS | `test_a_controlled_gap_is_patched_by_agent`（`resolve_gaps`→patch→提交 `PatchedByAgent.java`，`coverage.resolutions[0].action=="patch"`）；`test_resolve_gaps_patch_commits_change_set`（`create` 变更集 + `correlation.query_hash`） |
| ② 标准 Task 无缺口时不启动 Agent | ✅ PASS | `test_b_standard_task_no_gap_never_starts_agent`（注入会抛错的工厂 → 未被调用；`gaps==[]`、coding 账本 0、SUCCEEDED） |
| ③ 补方法后重跑无 Agent 复用并通过 | ✅ PASS | `test_c_adding_reviewed_method_reuses_without_agent`（复用 `AC01` 计入 covered、`gaps==[]`、`action=="reuse"`、工厂未被调用） |
| 装配优先优先于 Patch | ✅ PASS | `test_choose_action_prefers_reuse_over_patch`、`test_resolve_gaps_reuse_assembles_without_agent` |
| 无扩展点即拒绝、不伪装覆盖 | ✅ PASS | `test_choose_action_refuses_without_extension_point` |
| 上下文不含对话历史、确定性 | ✅ PASS | `test_task_context_has_frozen_fields_and_no_chat` |
| 提交前确定性校验 | ✅ PASS | `test_validate_change_set_rejects_unsafe` |

## 风险 / 下一步

- **复用装配为增量 COPY，非整体重锁**：`assemble` 会清空重建，故复用实现为缺口已知后的增量装入（不修改冻结 spec、不整体重建）。若未来需要「预先并入锁再由单次 assemble 产出」，须在 resolve 阶段引入兼容方法（本步未改动 `resolve_lock` 的 R06D 语义）。
- **复用候选受能力召回限制**：现有种子方法 `provides` 与平台能力 ID 不相交，故实测中 reuse 需**补充一份声明该能力+验收的兼容方法**（正是 WhenDone ③ 场景）；测试用**临时方法树**验证，不改已发布 `methods/`。
- **变更后验证成本**：一旦发生 reuse/patch 即多一次 `verify`+`accept`；无缺口标准主線零额外调用。
- **下一步 R18**：失败分类与有限自动修复（`classify_failure → coding → 可选 complex_repair → verify`；每任务 2 次、全 run 6 次上限）。
