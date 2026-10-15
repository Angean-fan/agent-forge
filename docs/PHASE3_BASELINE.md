# PHASE3_BASELINE｜AgentForge 第三阶段（C00）仓库事实核对与回归基线

> **步骤：C00**（《AgentForge_03_遗留闭环与模块自治_分步开发Prompt.md》第一阶段｜C）
> **性质：** 只做真实仓库核对与基线固定，**不修改任何生成逻辑、不新增功能**；凡未经证据支持的状态一律标注如实。
> **日期：** 2026-10-15。

---

## 1. 基准与干净状态

| 项 | 值 |
|---|---|
| 基准 Commit（C00 之前） | `bcffbd5`（docs: 追加 E01—E10 实现状态与偏差说明） |
| 基线 Commit（本次纳入第三阶段文档） | `0ba7086`（docs(plan): 纳入第三阶段《遗留闭环与模块自治》分步开发 Prompt） |
| C00 结束 Commit | 见本文件所在提交（仅新增 `docs/PHASE3_BASELINE.md`） |
| 分支 | `master` |
| 工作树状态 | 仅 `out/` 未跟踪（`out/club-management-source/`：早期生成样例产物，属运行产物，**不入库**，见 §9 风险） |
| 门禁脚本 | `./scripts/ci_gate.sh`（平台自身；不冒充 Provider/Runner 验收） |

**基线动作：** 将 `AgentForge_03_…Prompt.md` 作为独立基线 commit（`0ba7086`）纳入，随后跑一次**免费**门禁确认第二阶段结束态为绿，再进入 C00 核对。未改动任何 M/R/E 实现。

---

## 2. 对应来源文档编号

- **本步来源：** AgentForge_03 §C00。
- **核验依据来源：** AgentForge_02 文末《E01—E10 实现状态与偏差说明》(2026-10-13) 的 S1—S4 结构性偏差表与分步偏差表；`docs/MVP_ACCEPTANCE.md`、`docs/DEMO_P0_ACCEPTANCE.md`、`docs/E_MILESTONE_ACCEPTANCE.md`、`docs/E01…E10_*.md`、`docs/M01…R27_*.md`。
- **技术依据：** 《Demo技术文档_修订版_v0.3.docx》§1/§2/§4/§6/§7/§8/§9；《Demo需求分析文档.docx》。

---

## 3. 当前状态汇总

**C00 状态：PASS（核对完成）**——仓库、回归、旧锁、差异表均可定位；未发现需要伪造或缺失的关键证据。

### 3.1 已存在能力（已证实）

| 能力 | 证据位置 | 状态 |
|---|---|---|
| MVP M01—M28（含 M08A/M15A） | `docs/M01…M28_*.md`、`platform-api/src/agentforge_platform/*` | 声明完成，本步仅核对 |
| 平台后端回归 | `uv run --locked pytest` = **639 passed** | PASS |
| 平台前端 | `npm ci + type-check + build + test` = **41 passed (7 files)** | PASS |
| 独立 Runner | `pytest/ruff` = **30 passed, 2 skipped**；ruff All checks passed | PASS |
| R 系列（R01—R27、R06A—R06D） | `docs/R01…R27_*.md` | 声明完成 |
| E 系列（E01—E10，确定性部分） | `docs/E01…E10_*.md`、`platform-api/tests/test_e0*.py` = **134 passed** | PASS（运行时子集 NOT_RUN） |
| 冻结 Profile | `profiles/`：`certified.json`、`demo-{js,ts}-{mp,mb}-0.1`、`demo-ts-mp-0.2/0.3/0.4` | 存在、只读 |
| 模板 | `templates/`：`demo-0.1`、`demo-0.2`、`demo-0.2-js`、`demo-0.2-js-mb`、`demo-0.2-mb` | 存在、版本化 |
| 能力包 | `capability-packs/`：`task-crud-backend`(0.1.0—0.4.0)、`-backend-mb`(0.1.0—0.3.0)、`-frontend`(0.1.0—0.2.0)、`-frontend-js`(0.1.0—0.2.0) | 存在、版本化 |
| Integration Pack（只读） | `integration-packs/weather-lookup/` 0.1.0/0.2.0/0.3.0 | 存在（**不进入**生成默认注册表） |
| Method Unit 仓库 | `methods/{field-validator,date-normalizer,status-guard}/1.0.0` | 3 个方法 |
| 方法索引（派生） | `methods/.index/method-index.sqlite` | schema 0.1 |
| DB 表 | `project, spec_version, platform_session, run, task, event, artifact, worker, budget_adjustment, model_call`（`models.py`） | 已迁移至 0014 |

### 3.2 部分 / 偏差（承自第二阶段，未在本步修复）

| 编号 | 当前实现事实（本步代码核实） | 处置步骤 |
|---|---|---|
| **S1** Integration Pack 装配/打包 | `generation_graph.py`/`assembler.py` **无** Integration Pack 引用；`packaging.py:244` 仍写占位 `EXTERNAL_SERVICES.md`；`DEFAULT_ROOTS` 仅含 `templates`+`capability-packs`（`asset_registry.py:28`），集成包在 `DEFAULT_INTEGRATION_ROOT` 但**不进入**默认 | **C03** |
| **S2** Qwen 生产路由注入 | `worker.py:30-39` 构造 `GenerationContext` **未注入** `planning_client / gap_engine_factory / repair_engine_factory / review_client`；`generation_graph.py:758` `default_context` 同样未注入 → **生产 run 即便置 flag 也不调用 Qwen** | **C01**（+C02） |
| **S3** DeepSeek 可选调用 | E03/E07/E09 的 DeepSeek "缝" 已实现，默认无凭据不调用 | **C01/C04/C05** |
| **S4** E 范围跨厂商路由断言 | R27 评估已有路由断言；E 里程碑无独立"无意外跨厂商调用"断言（E 范围调用为 0） | **C06** |
| E03/E04 | 指南未进 ZIP；`spec.external_requirements` 未回填；`external_readiness` 读时派生未落库 | **C03** |
| E07 | 规则式提取已有；确认后集成规格未驱动 Adapter 生成 | **C04** |
| E09 | 新 spec+新 run 部分支持；模型缝未调用 | **C05** |
| E10 | 资产审核闭环部分；跨 Profile 契约为文本校验、未编译 | **C06** |

### 3.3 未有（F 阶段基础，现为零实现）

代码核实：`platform-api/src/agentforge_platform/` 中**不存在** `module_id` / `ModuleRegistry` / `ModuleMemory` / `ModuleAgent` / `InterfaceRegistry` / `module_agents_*`。出现的 `owned_paths` 全部是**资产 manifest** 字段（模板/能力包/方法的目标安装范围），**不是模块所有权**。故 F01—F04 的模块责任/记忆/接口/按需 Agent 均为**全新**，依赖 C02 真实闭环 + F01 起步。

### 3.4 NOT_RUN / 外部阻塞（保持，不伪造）

| 项 | 原因 | 关联步骤 |
|---|---|---|
| E05 真实 Live 第三方验证 | 需供应商沙箱、Runner 网络放行（现 `container_network=none`）、受控秘密注入、副作用人工批准 | X02 |
| E08 真实隔离在线预览 | 需预览基础设施、TLS/访问控制、回收执行主体、演示数据 | X03 |
| C01 真实 Qwen 工具调用探针 | 需开发者确认地域/接口/凭据/预算/价格版本 | C01（探针部分） |
| F08 并行调度 / X01 YAML | 依赖前序 + 单独批准 | F08/X01 |

### 3.5 新增 feature flag 基线

**本步不新增任何开关**（C00 约束：仅在确有需要时添加，不杜撰已存在字段）。经 grep 核实，仓库当前**尚无** `module_agents_enabled` / `integration_pack_generation_enabled` / `module_parallel_enabled`。其引入分别归 C03 / F-阶段。当前所有既有行为在**无新开关**下与第二阶段结束态一致。

---

## 4. 测试命令与逐项结果

| 套件 | 命令 | 结果 |
|---|---|---|
| 门禁（单一口径） | `./scripts/ci_gate.sh` | **gate passed** |
| 平台后端（全量） | `cd platform-api && uv run --locked pytest` | **639 passed**（0 failed） |
| 平台后端静态 | `ruff check .` | All checks passed |
| 平台前端 | `npm ci && type-check && build && test` | **41 passed (7 files)** |
| 独立 Runner | `uv run --locked pytest && ruff check .` | **30 passed, 2 skipped**；ruff clean |
| **MVP/基线冷启动**（排除 E） | `pytest $(ls tests/test_*.py \| grep -v /test_e)` | **505 passed** |
| **E01—E10 新增** | `pytest $(ls tests/test_e*.py)` | **134 passed** |
| **旧锁重放（聚焦）** | `pytest test_r06d… test_r09… test_e01… test_e10… -k "lock or replay or stable or cold"` | **14 passed** |

**FAIL：0。** NOT_RUN：E05 真实 Live、E08 真实预览实例、C01 真实 Qwen 探针（均因外部条件，见 §3.4）。

---

## 5. MVP Task 冷启动与旧 asset-lock 重放

- **MVP 冷启动：** 505 项基线用例全绿（含 MVP Task 冷启动、装配、验收、四组合锁样例、R01—R27）。
- **旧 asset-lock 重放（`verify_lock_hash`，全部 OK）：**

| 样例 | 版本 | hash（前 16） |
|---|---|---|
| `asset-lock.sample.json` | 基线 | `b9d88a6a75433717…` |
| `asset-lock.r27.sample.json` | 0.1 | `29b4d9d68996f8d0…` |
| `asset-lock.js.sample.json` | — | `c2ec5c66d7ef638e…` |
| `asset-lock.js-r27.sample.json` | — | `0bf0759285f85516…` |
| `asset-lock.mb.sample.json` | — | `03e3ad5df6e7770a…` |
| `asset-lock.js-mb.sample.json` | — | `09522412fb260779…` |
| `asset-lock.integration.sample.json` | 0.3 | `6fa58c5ed4693a7c…` |

- **方法索引快照：** `INDEX_SCHEMA_VERSION=0.1`，`scan_methods` count=**3**，`index_revision=` `16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341`（与 R27 评估所记一致）。
- 旧锁字节**未变**，`methods/` 与 `schemas/*.sample.json` 未被本步触碰。

---

## 6. Runner / 路由 / 账本 / 成本

- **Runner 实际执行：** 本步**不触发** Runner（不做生成/构建/HTTP 验收）；E08/E05 运行时仍 NOT_RUN。
- **路由实际 provider/model/region：** 本步**零模型调用**；`worker.py` 生产路径未注入任何 Qwen 客户端（S2），DeepSeek 缝默认不调用（S3）。无 region 证据可归档（探针待 C01）。
- **run_id / task_id / call_id 账本与 UNKNOWN：** 本步无新 run、无模型调用、无 Token、无费用、无 UNKNOWN。

---

## 7. 模块责任 / 记忆 / 接口（F 阶段）

本步**不适用**：F01—F04 尚无实现（§3.3）。基线仅固定"当前为零"，供 F 阶段对照。

---

## 8. WhenDone 逐条判定

| WhenDone 要求 | 结论 | 证据 |
|---|---|---|
| 输出 `docs/PHASE3_BASELINE.md`，逐项列出 PASS/部分/NOT_RUN/阻塞 及其依据、commit、命令、证据 | **满足** | 本文件 §3—§5；基线 commit `0ba7086` |
| 原 Task MVP 冷启动与旧 asset-lock 重放结果**可定位** | **满足** | §5：505 冷启动全绿；7 锁样例重放 OK |
| 所有新开关默认关闭；关闭时原生成/打包行为与固定检查无回归 | **满足（N/A）** | 本步未新增开关（§3.5）；门禁绿，既有测试零改动 |
| 缺失证据不得伪造 | **满足** | 无伪造；E05/E08/C01 探针如实 NOT_RUN（§3.4） |

---

## 9. 遗留风险与下一步依赖

1. **`out/` 未跟踪目录**（`out/club-management-source/`）：早期生成样例产物，未被 `.gitignore` 覆盖。本步**不改动**（不加入库、不改 ignore），留给开发者决定是否清理或纳入忽略规则。
2. **门禁为免费路径**：`ci_gate.sh` 不调用任何模型、不启动容器；不等同于真实 Provider/Runner 验收。
3. **C01 阻塞项（需开发者确认，未自行猜测）：** 百炼地域/接口、`qwen3-coder-flash` 工具调用可用性、真实预算、价格版本、API 凭据。就绪前 C01 的生产注入可做（flag 保护），但**真实探针**须标 NOT_RUN。
4. **S1 的"已满足"边界：** E01/E02 的满足是**资产/测试层**结论，**不是**端到端生成链路已满足；C03 前不得假定集成包已在生成路径生效。
5. **C02 前置：** 依赖 C01；无 Gap 时仍走确定性 COPY/RENDER。

**下一步依赖：** C00 完成 → 可进 **C01**（Qwen 生产路由注入 + 工具探针；探针部分待授权）。F01/F02/F03 的纯确定性部分需待 **C02** 通过后推进。
