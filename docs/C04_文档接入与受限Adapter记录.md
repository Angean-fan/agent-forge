# C04｜仅有供应商文档时的待确认集成规格与受限 Adapter（E07/S3）

日期：2026-10-15
状态：**PASS（确定性/受控部分）**；真实 DeepSeek 提问调用 **NOT_RUN**（缺凭据）；真实 Runner 执行 Java 契约测试 **NOT_RUN**。

## 1. 基准与干净状态

| 项 | 值 |
|---|---|
| 基准 Commit | `d486cf5`（C03） |
| 结束 Commit | 见本文件所在提交 |
| 与来源文档编号对应 | AgentForge_03 §C04；E07 剩余项；偏差 **S3** |
| 依赖前置 | C01/C02/C03 |

## 2. Goal 与判定

**Goal：** 把 E07 的规则式提取与待确认门禁，串成
`不可信文档 → 待确认 Integration Spec → DeepSeek 提问/用户确认 → 冻结集成契约 → Qwen 受限 Adapter → Mock 契约校验`
的真实链路。

**结论：** 链路已实现（模块级服务，与 E06/E07 同级）；DeepSeek 提问经**网关账本**且缺凭据记 `NOT_RUN` 并**保留草稿**；受限 Adapter 仅在 `integration/adapter/**` 变更（越界即拒）。

## 3. 变更文件（真实路径）

**新增（本步**纯新增**，未改任何既有文件）**
- `platform-api/src/agentforge_platform/integration_onboarding.py` — 接入链路服务：
  `start_onboarding`（规则式提取→问题清单）/ `augment_questions_with_deepseek`（经 `user_interaction` 网关）/
  `confirm_onboarding`（确认后冻结）/ `adapter_generation_plan` / `apply_adapter_changes`（受限引擎 + 授权校验 + 提交）。
- `platform-api/tests/test_c04_integration_onboarding.py` — 8 例。

## 4. 输入 / 输出契约

- **输入**：不可信文档（`integration_doc.Doc`，如 `schemas/third-party-doc.rate-provider.sample.md`）、
  用户确认 `{qid: answer}`、编码引擎工厂 `(allowed_paths, task_id) -> (engine, backend)`（与 R17/R18 同形；生产由 C01 编码路由提供）。
- **输出**：`OnboardingResult{provider, draft, questions, explain_status, frozen}`；冻结 `FrozenIntegrationSpec`（`ready_for_live=False`）；
  Adapter 变更集（仅 `backend/src/main/java/com/agentforge/integration/adapter/**`）。

## 5. 兼容性 / 回滚

- **零改动兼容**：未修改 E07 的提取/冻结/授权边界；新增模块**纯增量**，无 schema/迁移/锁变化。
- **开关**：`AGENTFORGE_INTEGRATION_DOC_ENABLED`（默认关闭）；关时普通 Task 路径**零漂移**（本步不接入生成图）。
- 回滚：删除新增两文件即可。

## 6. 测试命令与逐项结果

`./scripts/ci_gate.sh` → **gate passed**

| 套件 | 命令 | 结果 |
|---|---|---|
| 后端全量 | `uv run --locked pytest` | **666 passed**（0 failed） |
| 后端静态 | `ruff check .` | All checks passed |
| 前端 | `npm ci + type-check + build + test` | **41 passed** |
| Runner | `pytest / ruff` | **30 passed, 2 skipped** |
| **C04 新增** | `pytest tests/test_c04_integration_onboarding.py` | **8 passed** |
| MVP 基线（排除 E） | `pytest $(ls tests/test_*.py \| grep -v /test_e)` | **532 passed**（524 + C04 8） |
| E01—E10 | `pytest $(ls tests/test_e*.py)` | **134 passed**（不变） |

**FAIL：0。** NOT_RUN：真实 DeepSeek 提问、真实 Runner Java 契约测试。

## 7. WhenDone 逐项证据

| WhenDone | 结论 | 证据 |
|---|---|---|
| 缺少关键字段的文档真实产生问题并暂停 | **满足** | `test_incomplete_doc_pauses_then_freezes`（`INTEGRATION_PENDING` + `pending_ids`） |
| 用户确认后签名/配置/错误约定被冻结 | **满足** | 同上；冻结保留出处、`ready_for_live=False` |
| 演示 Adapter 仅在批准的目录变更 | **满足** | `test_adapter_changes_confined_to_adapter_dir`（写 `adapter/` 成功） |
| 越界变更被拒 | **满足** | `test_adapter_out_of_bounds_change_is_rejected`（改 `port/` → `ASSET_CONFLICT`） |
| Mock 成功/失败/超时契约 | **契约层满足（复用 E02）** | `integration_contract.port_contract_cases` 覆盖 success/failure/timeout（`tests/test_e02_integration_contract.py`）；Java 执行 NOT_RUN |
| 缺凭据时调用列为 NOT_RUN 且可保留草稿 | **满足** | `test_deepseek_missing_key_is_not_run_and_keeps_draft`（`NOT_SENT`、草稿原样） |
| 工具权限可核查 | **满足** | `adapter_edit_issues` 授权前缀校验 + `FileBackend.allowed_paths` 工具层约束 |
| 模型路由/账本可核查 | **满足** | `test_deepseek_augments_with_injected_client`（`user_interaction` / `deepseek_official` 入账） |
| 注入/未知 SDK 防护 | **满足** | `test_injection_recorded_not_applied`、`test_unknown_sdk_pending_and_not_installed` |
| 普通 Task 无回归 | **满足** | `test_onboarding_disabled_by_default`；本步不改生成图 |

## 8. 路由 / 账本 / 费用 / 安全

- **路由**：DeepSeek 提问经 `model_gateway.invoke`，`user_interaction → deepseek_official/deepseek-flash`（草稿期 `run_id=None`）；适配器编码由**注入的** C01 编码路由提供（本步测试用脚本化引擎）。
- **账本**：缺凭据 → `ProviderPreflightError` → `NOT_SENT`（预留 0）；注入假客户端 → 记账并 `role=user_interaction`。
- **费用**：测试**零真实调用**；无 Token/费用。
- **安全**：文档视为数据（注入仅记录）；未知 SDK 只 pending；Adapter 越界即拒；不宣称 Live；新 Adapter 不自动发布公共资产（无入库路径）。

## 9. 遗留风险 / 待确认 / 下一步

1. **真实 DeepSeek 提问 —— NOT_RUN**：`.env` 有 `DEEPSEEK_API_KEY`，可经授权后真实复跑（经网关账本）。
2. **未接入生成图**：本步是模块级链路；把"确认后的集成规格 → 受限 Adapter 任务"作为生成图节点/任务接入（复用 C02 的 `_gap_factory` 缝与 R17 语义）留待需要时进行——本步**不接入**以保持普通 Task 零漂移。
3. **真实 Runner Java 契约测试 —— NOT_RUN**：生成的 Adapter 需由受控 Runner 运行 `mvn test`；平台侧仅做 E02 确定性契约校验。
4. **下一步 C05**：E09 生成项目的真实增量迭代（新 spec/新 run/旧 ZIP 不变），可复用本步与 C03 的资产/装配能力。
