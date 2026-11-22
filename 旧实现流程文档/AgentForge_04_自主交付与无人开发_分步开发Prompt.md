# AgentForge｜第四阶段：自主交付与无人开发（A—F）｜分步开发 Prompt

> **文档性质：** 面向 AI 编码工具的**增量实现流程**；不是对既有仓库的代码审计报告，也不是对 `M/R/E/C/F` 历史步骤的重新验收。
>
> **编制基线：** 2026-11-13。依据《AgentForge_01_MVP_分步开发.md》《AgentForge_02_迭代增强_分步开发Prompt_方法索引版.md》《AgentForge_03_遗留闭环与模块自治_分步开发Prompt.md》与当前仓库事实（含 `docs/F01—F08`、`docs/DEMO_P0_ACCEPTANCE.md`、`docs/PHASE3_BASELINE.md`）。
>
> **目标：** 在**不重写 MVP、不推翻既有 Registry/Resolver/Method Unit/Runner** 的前提下，把平台从「人工确认规格 + 固定验收 + 单 run 装配」推进到「**人只给目标与约束，系统自主完成需求澄清、架构、实现、验证与交付**」。
>
> **新增实施编号：** `A—F` 六个工作流。它们不改写既有 `M/R/E/C/F/X` 的编号、完成标记或验收证据；与第三阶段冲突之处集中列在 §0.3，必须由开发者明确批准。
>
> **进度声明边界：** 本文件的所有 `WhenDone` 都是**待实现目标**，不是既有事实。任何一步未取得真实运行证据，只能记 `NOT_RUN/BLOCKED`，不得用文档、模拟测试或单次样例冒充「生产已接通」。

---

## 0. 使用方式：每次仅交付一个步骤

### 0.1 依赖图

```text
C00 仓库事实核对（继承第三阶段，所有步骤的共同前置）
  └─ A 需求契约与验收合成
       └─ B 架构与模块契约合成
            └─ C 层级编排与模块执行
                 ├─ D 独立验证与证据链
                 └─ E 自主性策略与升级
                      └─ F 端到端自主交付验收与阶段收口
```

**说明：**

- 依赖是**能力依赖**，不是日历依赖。A/B/D 的纯确定性部分可并行推进；C 必须在 B 的契约冻结之后；F 必须在 A—E 全部具备真实证据之后。
- 每步默认**开关关闭**。关闭时旧 `run_generation`、旧 asset-lock、旧固定验收必须零漂移。
- 未满足前置依赖时**阻塞并记录**，禁止「先写接口再补证据」或「先宣布自主再补安全边界」。

### 0.2 总控 Prompt（每个步骤开始前附上）

```text
你是 AgentForge 第四阶段的增量实施型编码助手。只执行本次指定的 A/B/C/D/E/F 一步；禁止擅自执行后续步骤。

【先检查再编码】
1. 查看 Git 状态、README、依赖锁、Profile、冻结 spec/contract、Registry/Resolver、asset-lock、生成图、worker/default_context、Runner、测试与 docs/DEMO_P0_ACCEPTANCE.md、docs/PHASE3_BASELINE.md、docs/F01...F08 记录。文档中的路径只是线索，以仓库事实为准。
2. 对照本步 Goal/Context/Constraints/WhenDone，列出「已有 / 部分 / 未有 / NOT_RUN / 外部阻塞」；若前置未满足，停在检查结论。
3. 最小增量与适配器优先；禁止重写历史 Registry/Resolver、已发布资产、冻结 spec/asset-lock、固定验收测试。新旧 schema/锁必须区分版本，旧锁可原样重放。
4. 保留「冻结规格 → Asset Finder（只读召回）→ Registry/Resolver（确定性兼容决策）→ 装配 → 明确 Gap 才受限 Patch → Runner 验证」的优先级。检索命中、模型建议、模块记忆或相似度不等于业务验收已覆盖。
5. 模型路由沿用第三阶段固定值：需求理解/提问 DeepSeek deepseek-flash；技术规划/契约提议 qwen3.7-plus；受限编码/普通修复 qwen3-coder-flash；疑难修复 qwen3.8-max。不得新增路由决策模型、无授权换模型。新增验证/审查角色只能复用既有角色或纯确定性检查，不得自造新 Provider。
6. 每次模型调用（含子 Agent、重试、工具循环）走平台网关、预留/幂等结算、共享限额，并带 run_id/task_id/call_id。没有真实 usage/价格证据标 UNKNOWN/NOT_RUN，不得记零或伪造 PASS。
7. 不可信输入包括自然语言、外部文档、OpenAPI、模型返回与生成代码。秘密不得进入 Prompt、检查点、日志、ZIP、长期记忆或验收报告。
8. 运行期 Agent 只可在 task.allowed_paths ∩ module.owned_paths 内通过受控暂存 Backend 写入；共享目录必须有唯一 owner；无任意 shell/网络/宿主 Docker 权限。
9. 独立验证者只读；实现者不得验收自己的产出；任何放行必须基于可复现证据，不得以删除/放宽断言制造成功。
10. 本阶段的意义是「让系统自己证明完成」，不是「让 Agent 数量变多」。不以 Agent 数量、调用次数或文档篇幅作为成功指标。

【本步完成后的固定输出】
- 步骤编号、基准/结束 commit、仓库干净状态；
- 已有能力与差距、目标与实际变更文件、schema/API/迁移/回滚说明；
- 测试命令与真实 PASS/FAIL/NOT_RUN；旧 Task 冷启动与旧 asset-lock 重放结果；
- 权限/路由/费用/密钥扫描证据；
- 每条 WhenDone 的判定与证据位置；
- 剩余风险、需开发者批准事项、下一步依赖。
```

### 0.3 不可改变的既有边界

| 边界 | 继承方式 |
|---|---|
| 生成对象 | 继续以已认证 Vue 3（JS/TS）+ Spring Boot（MyBatis/MyBatis-Plus）+ MySQL/Maven Profile 为基线；不擅自引入微服务或新框架。 |
| MVP / Demo P0 | `M01—M28`、`R01—R27` 的固定验收与 9 次评估门槛不因本阶段改写。 |
| 资产真值 | 已发布 Template/Capability Pack/Method Unit/Integration Pack 的源码与 manifest 为权威；FTS5 只是可重建的候选索引。 |
| 资产发布 | 当前 run 通过 ≠ 公共资产可发布；仍需来源/许可证、独立兼容测试、人工批准、不可变版本。 |
| 任务与预算 | 复用既有 TaskPlan/DAG、run/task/模型账本、取消/暂停/恢复/Runner 验收；不创建第二套计费或任务真值。 |
| 文件与安全 | 最小读权限、受保护文件只读、暂存后原子提交、无任意 shell/网络、无宿主 Docker Engine 权限。 |
| 成功语义 | 必需项非 PASS 不得标 SUCCEEDED；`NOT_RUN` 不得算通过；代码交付状态与第三方 Live 就绪状态分开。 |
| 实现与验收分离 | 实现者不得验收自己的模块/系统；验证者不得修改被验代码。 |
| 不可逆操作 | 生产发布、真实付费、外部副作用、扩大预算、跨出沙箱，仍须人授权。 |

### 0.4 与第三阶段边界的差异（必须由开发者明确批准）

| 第三阶段边界 | 本阶段变化 | 为什么必须批准 |
|---|---|---|
| 默认「单 worker、串行任务、共享预算」，并行后置 | C 引入系统级 → 模块级编排与**受限并行波次** | 涉及 Runner 资源、SQLite 租约/锁、预算池与恢复语义 |
| 「人工确认规格/验收」 | A 改为**系统合成验收并冻结**；人在 L2/L3 下不参与开发验收 | 错误验收会导致「自信地交付错的东西」 |
| 验收断言「由平台固定」 | A 增加**从需求合成可执行验收**，但冻结后仍不可被实现者修改 | 改变完成定义的来源 |
| 模块由规则派生、接口事后发布 | B 允许模型提议架构/模块，但由 Python 校验并**接口开工前冻结** | 陌生领域规则不足；接口先行是并行的前提 |
| 「不默认持续驻留 Supervisor、无限自主委派」 | 仍**不引入**模型型常驻 Supervisor；改为**确定性编排器 + 有界自主** | 避免自我扩权、无限循环与成本失控 |
| 人工批准 F08/X 等运行时扩展 | 保留；L2/L3 自主等级本身即为需批准事项 | 风险等级高于默认 Demo |

> **明确不改变：** 不引入无审核的自动资产入库；不允许模型自报 VERIFIED；不允许生产公网执行；不做自动收费/多租户；不把「无人」理解为「无边界」。

---

## 1. 已有实施事实与未闭环事项

| 既有能力 | 当前事实 | 本阶段承接 | 是否前置 |
|---|---|---|---|
| `spec` 冻结与 hash | R06 已实现：功能/规则/验收场景随规格冻结，篡改被拒 | A 扩展为「可执行验收计划」 | **是** |
| 需求访谈 | R02—R05 已实现：缺失字段/冲突/多轮提问 | A 复用为澄清问题生成 | **是** |
| 技术规划 | R13：模型只提议任务，Python 校验 DAG | B 复用为架构/模块提议 | 是 |
| 模块登记/所有权 | F01：规则派生模块、独占写入路径 | B 扩展为模型提议 + 确定性校验 | 是 |
| 接口登记 | F03：DRAFT/VERIFIED，**实现后发布** | B 增加 `APPROVED_FOR_BUILD` 开工前冻结态 | 是 |
| 模块 Agent | F04：按模块加载上下文，**默认关闭、未接入生成图** | C 接入层级编排 | **是** |
| 模块协调器 | F05：请求路由/消费者分析，**未接入生成图** | C 接入模块间变更 | 是 |
| 跨模块集成门禁 | F06：`integration_ok` + 提交状态机，**未接入生成图** | C/D 接入终态 | **是** |
| 并行调度 | F08：开关 + 人工批准，默认关闭；真实多 worker `NOT_RUN` | C 作为可选波次，不默认启用 | 否 |
| 失败分类/修复 | R18：代码错误有限修复；基础设施不盲改 | D/E 复用为验证失败归因 | 是 |
| 语义审查 | R19：最多一轮，可选，不覆盖确定性结果 | D 作为验证者的一种证据源 | 否 |
| 预算/时限/取消/恢复 | R20/R21/R22/R23/R24 | E 的策略输入与刹车 | **是** |
| 固定验收 | Runner 的 Task/校园/借阅固定场景 | D 的确定性基线；A 的新验收必须另行生成 | 是 |
| 方法仓库/索引 | R06A—R06D：文件真值 + FTS5 | C 的复用底座；**本阶段不默认引入向量库** | 否 |

**严格区分：** F01—F08 的「机制 PASS」不等于「已接入生成图」；本阶段所有接线必须在真实 run 上有证据。`NOT_RUN` 不得因增加模拟测试改为 PASS。

---

# 第一阶段｜A：需求契约与验收合成

## A｜从任意需求生成「意图契约 + 可执行验收计划」

- **Goal：** 把用户输入（可以是粗略目标，也可以附带框架/技术约束）转成一份**冻结的意图契约**：目标、非目标、角色、实体、权限、业务规则、非功能约束、技术约束、**假设台账**，以及**可执行的验收计划**。验收计划是「完成」的唯一权威定义。
- **Context：** 现有 `SoftwareSpecification` 已含 acceptance_cases，但它们是「随规格冻结的断言」，不是自动生成的可执行测试；Runner 验收仍是平台固定场景。A 需要在不改变冻结语义的前提下，把「断言」推进到「可运行的测试/检查」。
- **Constraints：**
  - 不确定项必须落入**假设台账**（`user_explicit / inferred / default`），不得静默补全；
  - 澄清问题**批量、有限**（默认 ≤5 个/项目），只问阻塞性问题；能安全默认的不问；
  - 每条验收必须标记 `executable` 或 `MANUAL_REQUIRED`；`MANUAL_REQUIRED` 在自主模式下必须转为「需用户确认」或「显式延期」，不得当作通过；
  - 验收计划冻结后，实现者**无权修改**；篡改必须被检测；
  - 模型只起草/提议，Python 负责结构校验、hash 与冻结；
  - 人可以在 L1 模式确认；L2/L3 模式下不要求人确认，但假设台账随交付物披露。
- **WhenDone：**
  1. 一个**陌生需求**能从原文生成完整意图契约，缺字段有假设而非空白；
  2. 每条验收有 ID、类型、Given/When/Then、对应需求项、可执行标志；
  3. 可执行验收能生成真实测试并被 Runner 或本地测试框架执行；
  4. 故意违反任一验收的实现会被测试捕获；
  5. 篡改冻结验收 → `SPEC_TAMPERED`；
  6. 旧 Task 路径零漂移。

### A.1 输入 / 输出契约

```yaml
intent_contract:
  contract_id: "<uuid>"
  project_id: "<project>"
  spec_hash: "<frozen-hash>"
  goals: ["..."]
  non_goals: ["..."]
  actors: [{id, name, permissions}]
  entities: [{name, fields, relations}]
  business_rules: [{id: "BR01", text, source: "user_explicit|inferred|default"}]
  non_functional: [{id, kind: "performance|security|availability|...", target, source}]
  constraints: {stack: "...", architecture: "...", budget_micro: 0, deadline: "..."}
  assumptions:
    - id: "AS01"
      text: "..."
      source: "inferred"
      confidence: "high|medium|low"
      risk: "low|medium|high"
  unresolved: [{id, question, blocking: true|false, default_if_unanswered}]

acceptance_plan:
  plan_id: "<uuid>"
  intent_hash: "<hash>"
  cases:
    - id: "AC01"
      requirement_ref: "BR01"
      kind: "http|db|ui|property|static"
      given: "..."
      when: "..."
      then: "..."
      executable: true
      test_ref: "<generated-test-path-or-id>"
      manual_reason: null
  coverage: {requirements_total: 0, covered: 0, manual_required: 0}

clarification_bundle:
  questions: [{id, question, why_blocking, options, default}]
  max_questions: 5
  answered: false
```

### A.2 实现方式

1. **扩展 spec schema（向后兼容）**：`acceptance_cases` 增加 `kind/executable/test_ref/requirement_ref`；新增 `assumptions`、`constraints`、`unresolved`。旧版本 body 按默认值读取，不改旧 hash。
2. **歧义检测**：确定性规则（缺实体/缺权限/缺验收/规则冲突）+ 模型提议；输出 `clarification_bundle`，按阻塞性与风险排序，**上限 5 个**。
3. **验收合成**：模板优先（CRUD/权限/分页/唯一约束/状态迁移），模型补业务语义；Python 校验每条验收**可追溯到需求项**，且不是同义反复。
4. **测试生成**：为 `http/db/static` 生成可执行测试；`ui` 生成最小 Playwright/接口级冒烟；无法自动化的标 `MANUAL_REQUIRED` 并进入升级策略（E）。
5. **冻结与追溯**：`intent_hash + acceptance_hash` 进入 run；实现者只读；新增 `traceability.json`：需求 → 验收 → 模块（B 补齐）。
6. **L1/L2 分支**：L1 允许人确认/编辑；L2 直接冻结并披露假设；L3 仅限沙箱且必须通过 D 的独立验证。

### A.3 建议变更文件（以仓库实际为准）

```text
platform-api/src/agentforge_platform/
├─ spec.py                     # AcceptanceCase 扩展、Assumption/Constraint/Unresolved
├─ intent_contract.py          # 新增：契约合成、校验、冻结、hash
├─ acceptance_synthesis.py     # 新增：验收合成 + 用例追溯校验
├─ acceptance_tests.py         # 新增：测试生成（http/db/static/ui）
├─ routes_spec.py              # 新增/扩展：生成意图契约、预览验收、确认
└─ migrations/versions/00xx_intent_contract.py
platform-api/tests/            # 新增：模糊需求、假设台账、篡改、可执行性、负例
```

### A.4 验收证据要求

- 一个粗略需求与一个带框架约束的需求各跑一遍；
- 假设台账非空且可解释；
- 每条可执行验收有真实测试运行结果；
- 故意违反一条验收 → 测试失败；
- 冻结后篡改 → 拒绝；
- 旧 Task 生成/验收回归 PASS。

---

# 第二阶段｜B：架构与模块契约合成

## B｜从冻结意图契约生成架构、模块责任图与「开工前冻结」的公共接口

- **Goal：** 生成并冻结 `ArchitectureContract`：模块划分、职责、独占写入路径、共享资源唯一 owner、依赖 DAG、公共接口（签名/DTO/错误码/权限/版本）、数据模型与迁移归属、非功能落点；并把每一条验收映射到模块。
- **Context：** F01 已能规则派生模块；F03 已能登记接口，但状态是 DRAFT/VERIFIED，**实现后才发布**。陌生领域需要模型参与模块划分，但必须由 Python 做确定性校验；并行与集成的前提是接口先冻结。
- **Constraints：**
  - 模型只提议，Python 校验并冻结；所有产物带 hash；
  - 校验必须拒绝：依赖环、路径重叠、共享文件无唯一 owner、接口缺失、验收无归属、孤儿模块、未覆盖需求；
  - 公共接口在开工前为 `APPROVED_FOR_BUILD`，不是 `VERIFIED`；实现并通过验证后才升 `VERIFIED`；
  - 破坏性接口变更必须做消费者影响分析（F05），并回到 B 的新版本；
  - 业务范围变化必须回 A/用户确认，不得由技术 Agent 自行扩大。
- **WhenDone：**
  1. 一个陌生需求能被拆成稳定模块图，所有验收 100% 映射到模块；
  2. 每个跨模块依赖都有预声明接口；
  3. 循环依赖/路径重叠/无主共享文件被拒；
  4. 接口 `APPROVED_FOR_BUILD` 可被 C 消费，且实现者不能自改；
  5. 架构契约变更可追溯、可回滚。

### B.1 输入 / 输出契约

```yaml
architecture_contract:
  contract_id: "<uuid>"
  intent_hash: "<hash>"
  modules:
    - module_id: "user"
      responsibility: "..."
      owned_paths: ["backend/src/main/java/.../user/**", "frontend/src/views/user/**"]
      depends_on: []
      shared_owner: false
      acceptance_ids: ["AC01", "AC02"]
  shared_resources:
    - path: "backend/src/main/java/.../common/**"
      owner: "template|integration|module:shared"
      consumers: ["user", "order"]
  interfaces:
    - capability_id: "user.find_by_id"
      owner_module: "user"
      kind: "JAVA_METHOD|HTTP|OPENAPI"
      symbol: "UserQueryService.findById"
      signature: "UserDTO findById(Long userId)"
      args_schema: {...}
      returns_schema: {...}
      errors: ["USER_NOT_FOUND"]
      auth: ["..."]
      version: 1
      status: "APPROVED_FOR_BUILD"
  data_model:
    tables: [{name, owner_module, columns, indexes, constraints}]
    migration_owner: "template|module:user"
  non_functional: [{id: "NFR01", target, module_ids}]
  traceability: [{requirement_ref, acceptance_id, module_id}]

module_spec:
  module_id: "..."
  architecture_hash: "<hash>"
  acceptance_ids: [...]
  allowed_paths: [...]
  interfaces_consumed: [...]
  interfaces_provided: [...]
  definition_of_done: [...]
```

### B.2 实现方式

1. **模块提议**：模型基于意图契约提议模块与职责；无模型时退回 F01 规则派生。
2. **确定性校验**：无环、路径不重叠、共享文件唯一 owner、接口覆盖全部跨模块依赖、验收全覆盖、无孤儿模块；任一失败 → 阻塞。
3. **接口先行**：把每个跨模块能力写成可校验契约（签名/DTO/错误/权限/版本），状态 `APPROVED_FOR_BUILD`；F03 增加该状态与迁移规则。
4. **数据模型/迁移归属**：表与 migration 命名空间唯一 owner；共享表由模板层拥有；防止并行模块改同一 migration。
5. **冻结与变更控制**：架构 hash 进 run；接口变更生成新版本并做消费者分析；架构变更回 A 检查验收是否仍覆盖。
6. **追溯矩阵**：需求 → 验收 → 模块 → 接口；缺一环即阻塞。

### B.3 建议变更文件

```text
platform-api/src/agentforge_platform/
├─ architecture.py             # 新增：架构契约 schema/校验/冻结
├─ module_registry.py          # 扩展：模型提议接入 + 共享资源 owner
├─ interface_registry.py       # 扩展：APPROVED_FOR_BUILD 状态与迁移
├─ module_coordinator.py       # 复用：破坏性变更消费者分析
├─ spec_service.py             # 追溯矩阵挂载点
└─ migrations/versions/00xx_architecture_contract.py
platform-api/tests/            # 新增：环/重叠/无主共享/接口缺失/追溯缺口
```

### B.4 验收证据要求

- 陌生需求生成模块图与接口契约；
- 构造循环依赖、路径重叠、共享文件无 owner、接口缺失 → 全部拒绝；
- 验收追溯 100%；
- 接口在实现前可被消费，实现后升级 VERIFIED；
- 架构变更可回滚且旧 run 不受影响。

---

# 第三阶段｜C：层级编排与模块执行

## C｜系统级编排器驱动多个模块 run，按 DAG 串行/受限并行执行并集成

- **Goal：** 引入两层运行模型：`SystemRun`（项目级）→ `ModuleRun`（模块级）。系统编排器按依赖 DAG 派发模块、隔离暂存、汇总集成；模块 Agent 在 `owned_paths` 内实现并发布接口。
- **Context：** 现有 `run_generation` 是单 run、单产物目录；F04/F05/F06 机制已存在但**未接入**；F08 并行默认关闭。C 是「把零件接成流水线」的一步。
- **Constraints：**
  - 初期**单 worker 串行**；并行波次必须满足：F08 双开关 + 人工批准 + 无依赖 + 无共享资源冲突；
  - 模块只能写 `allowed_paths ∩ owned_paths`；模块暂存独立；合并按路径+内容 hash 检测冲突；
  - 模块不能读取其他模块私有源码/记忆；只能消费 `APPROVED_FOR_BUILD/VERIFIED` 接口；
  - 接口发布必须在模块验证通过后；实现者不能自发布；
  - 集成门禁（F06）任一冲突 → 整个 SystemRun 不通过；
  - 取消/预算/崩溃/重放语义沿用 R22/R24/R23；不重复扣费、不重复发布；
  - 本阶段**不默认引入向量数据库**；方法检索沿用 FTS5，若后续测量证明不足，单独立项。
- **WhenDone：**
  1. 一个 2 模块、有真实依赖的陌生项目可从冻结契约跑到集成验收；
  2. 模块 A 无法写 B 的路径；越权被拒；
  3. 单模块失败不拖垮同波其他模块；失败可归因；
  4. 独立模块可并行，冲突模块自动串行；关闭并行时行为与串行一致；
  5. SystemRun 可恢复、可取消、预算可控；
  6. 集成门禁能阻止「模块各自绿、整体红」。

### C.1 输入 / 输出契约

```yaml
system_run:
  system_run_id: "<id>"
  intent_hash: "<hash>"
  architecture_hash: "<hash>"
  status: "QUEUED|PLANNING|EXECUTING|INTEGRATING|VERIFYING|SUCCEEDED|FAILED|PAUSED|CANCELLED"
  module_runs: [{module_id, run_id, wave, status}]
  budget_pool: {reserved_micro, settled_micro, limit_micro}
  integration: {status, conflicts, evidence_ref}

module_run:
  module_id: "..."
  system_run_id: "..."
  architecture_hash: "<hash>"
  status: "PENDING|RUNNING|VERIFIED|FAILED|BLOCKED"
  staging_dir: "..."
  allowed_paths: [...]
  interfaces_consumed: [...]
  interfaces_provided: [...]
  verification_ref: "..."
```

### C.2 实现方式

1. **SystemRun 状态机**：新建系统级 run/task 映射，复用既有 run/task/event 表或新增兼容表；每个模块一个子 run。
2. **模块执行器**：把 F04 `ModuleAgentFactory/Executor` 接入编排；加载模块 spec + 已批准接口 + 私有记忆 + 受控源码；产出 `FileChangeSet`，不自行提交。
3. **波次调度**：按 DAG 分层；同层无依赖且无共享资源冲突的模块可并行（F08 门禁）；否则串行。每模块独立暂存。
4. **接口发布**：模块验证通过后，F03 从 `APPROVED_FOR_BUILD` 升 `VERIFIED`；验证证据绑定源码/契约 hash。
5. **集成门禁**：F06 `integration_ok` + 提交状态机；合并冲突、契约不一致、共享资源冲突 → 整体失败/回滚。
6. **恢复/取消/预算**：复用 R23 检查点、R22 取消、R24 预算暂停；系统级和模块级都要幂等。
7. **默认关闭**：`AGENTFORGE_SYSTEM_ORCHESTRATION_ENABLED` 未开启时回退旧单 run 路径。

### C.3 建议变更文件

```text
platform-api/src/agentforge_platform/
├─ system_orchestrator.py      # 新增：SystemRun 状态机、波次、预算池、恢复
├─ module_execution.py         # 新增：F04 接入、暂存/合并、接口发布
├─ generation_graph.py         # 扩展：系统级入口/开关/旧路径回退
├─ runs.py / models.py         # 兼容新表或复用既有 run/task
├─ module_parallel.py          # 复用 F08，接线
└─ migrations/versions/00xx_system_run.py
platform-api/tests/            # 双模块、越权、失败隔离、并行/串行、恢复、集成门禁
```

### C.4 验收证据要求

- 双模块陌生项目端到端（含真实 Runner）；
- 越权写入被拒；
- 单模块失败不影响同波；
- 并行开/关结果一致；冲突自动串行；
- 取消/预算/崩溃恢复不重复扣费或发布；
- 集成门禁负例：模块各自绿、契约冲突 → 整体 FAIL。

---

# 第四阶段｜D：独立验证与证据链

## D｜实现者与验证者分离，所有放行基于可复现证据

- **Goal：** 建立独立验证层：确定性检查（构建、测试、契约一致性、迁移、安全扫描）+ 有限语义审查 + 证据链。实现者不能验收自己的产出；验证者不能改代码。
- **Context：** Runner 已有固定验收；R19 有可选语义审查；F06 有集成门禁。D 要把它们组织成**分层验证矩阵**，并把 A 生成的验收计划真正执行起来。
- **Constraints：**
  - 确定性优先：编译/测试/契约/迁移/安全不通过，不得进入语义审查；
  - 语义审查最多一轮、只产出可测试问题；不得覆盖确定性结果；
  - 验证者只读；发现问题 → 生成修复任务 → 回到实现者；
  - 证据绑定 `spec_hash + acceptance_hash + architecture_hash + source_hash + contract_revision`；任一变化使旧证据失效；
  - `NOT_RUN` 不得当 PASS；缺环境要如实标注；
  - 没有「模型自报 VERIFIED」。
- **WhenDone：**
  1. 故意错误的实现被捕获；
  2. 缺失需求/验收未覆盖被捕获；
  3. 契约漂移（签名/DTO/错误/权限）被捕获；
  4. 证据链可审计、可重放、变更即失效；
  5. 验证者无法写入源码；实现者无法自批；
  6. 三层验证（单元/模块/集成）证据齐全。

### D.1 输入 / 输出契约

```yaml
verification_report:
  report_id: "<id>"
  scope: "module|system"
  target_ref: "<module_id|system_run_id>"
  bindings:
    spec_hash: "..."
    acceptance_hash: "..."
    architecture_hash: "..."
    source_hash: "..."
    contract_revision: 1
  checks:
    - name: "build|unit|contract|migration|security|acceptance|e2e|semantic"
      status: "PASS|FAIL|NOT_RUN"
      evidence_ref: "..."
      detail: "..."
  overall: "PASS|FAIL|NOT_RUN"
  stale: false

traceability:
  requirements: [{ref, acceptance_ids, module_ids, verification_refs}]
  uncovered: []
```

### D.2 实现方式

1. **确定性验证器**：复用 Runner 与本地测试；新增契约一致性检查（签名/DTO/错误/权限）、迁移检查、密钥/许可证扫描。
2. **验收执行器**：执行 A 的验收计划；每条用例产出独立证据；`MANUAL_REQUIRED` 进入升级策略（E）。
3. **独立验证者**：独立上下文、只读权限；基于冻结契约和验收做对抗性检查；产出问题而非修改代码。
4. **证据绑定与失效**：证据写入 run 目录并计算 hash；源码/契约/验收变化 → `stale=true`，必须重验。
5. **失败归因**：区分代码/契约/环境/验收缺陷；分别路由到实现者、B 的契约变更、基础设施或 A 的验收修订。
6. **门禁**：模块级、集成级、系统级三层；任一必需项非 PASS → 不放行。

### D.3 建议变更文件

```text
platform-api/src/agentforge_platform/
├─ verifier.py                 # 新增：确定性验证矩阵 + 证据绑定
├─ verifier_agent.py           # 新增：只读语义审查（复用既有 review 路由/客户端）
├─ acceptance_tests.py         # 复用/扩展：执行 A 的验收计划
├─ coverage.py / integration   # 扩展：追溯与缺口
└─ packaging.py                # 扩展：证据进入交付包
platform-api/tests/            # 新增：错误实现、契约漂移、证据失效、越权写入、NOT_RUN
```

### D.4 验收证据要求

- 错误实现/缺需求/契约漂移三类负例均被捕获；
- 验证者写入源码被拒；
- 源码变更后旧证据失效；
- 三层验证报告齐全；
- `NOT_RUN` 如实保留。

---

# 第五阶段｜E：自主性策略与升级

## E｜定义 L0—L3 自主等级与确定性的「继续/提问/暂停/升级/终止」策略

- **Goal：** 让系统在无人开发的情况下知道**何时继续、何时问、何时停、何时升级**。策略由确定性引擎执行，不允许模型自行扩权。
- **Context：** R18/R21/R22/R24 已有失败升级、限额、取消、预算暂停；但缺少面向自主运行的统一策略层，也没有「问题预算」和「不可逆操作」边界。
- **Constraints：**
  - 默认 L1；L2 需批准；L3 仅限沙箱、可测试、无外部副作用；
  - 澄清问题默认 ≤5 个/项目；超出则按假设台账 + 风险等级决定继续或暂停；
  - 预算/时限/连续失败/高风险假设/不可逆操作 → 暂停或升级；
  - 模型不得自行提高自主等级、扩大预算、开放网络/外部副作用；
  - 所有策略决策写入审计事件；重复失败不得无限重试；
  - 人类只处理不可逆/法律/预算/外部副作用/高不确定性问题。
- **WhenDone：**
  1. L2 下一次完整 run 无需开发人员介入；
  2. 模糊需求触发问题包而非静默假设；
  3. 预算/风险/失败阈值触发暂停/升级；
  4. 不可逆操作无法绕过授权；
  5. 策略矩阵有测试；无无限循环/无自我扩权；
  6. 审计事件可重建决策链。

### E.1 自主等级

| 等级 | 人的参与 | 适用范围 | 默认 |
|---|---|---|---|
| L0 | 逐步确认 | 探索/高风险需求 | 否 |
| L1 | 确认规格/验收 | 内部工具 | **是** |
| L2 | 只给目标+预算，异常升级 | 可测试项目 | 否，需批准 |
| L3 | 只收结果 | 沙箱、无外部副作用 | 否，需批准 |

### E.2 策略矩阵（示例）

| 触发 | 动作 | 说明 |
|---|---|---|
| 歧义超过阈值 | 生成问题包（≤5） | 阻塞性问题优先；可安全默认的写入假设台账 |
| 预算达到 80% | 警告 + 降低并行 | 不自动扩预算 |
| 预算耗尽 | PAUSED_BUDGET | 复用 R24 |
| 同一模块连续失败 ≥2/全 run ≥6 | 升级/终止 | 复用 R18 上限，不无限重试 |
| 高风险假设（安全/权限/数据） | 暂停，转人工 | 不允许默认 |
| 破坏性接口变更 | 消费者分析 + 批准/回滚 | 复用 F05 |
| 不可逆操作（发布/付费/外部写） | 人工授权 | 策略引擎不能自我批准 |
| 证据 `NOT_RUN` | 不得放行 | 保持诚实 |

### E.3 实现方式

1. `autonomy.py`：等级、策略、触发器、动作、审计事件；纯确定性。
2. 接入 A（问题包/假设）、C（暂停/恢复/并行降级）、D（失败归因/重验次数）、R21/R22/R24。
3. 问题包与回答持久化；同键幂等，不重复计费。
4. 不可逆操作清单与授权令牌；任何绕过尝试 → 拒绝并记录。
5. 可观测性：每决策带原因、阈值、证据引用。

### E.4 建议变更文件

```text
platform-api/src/agentforge_platform/
├─ autonomy.py                 # 新增：等级/策略/审计
├─ intent_contract.py          # 接入问题包/假设
├─ system_orchestrator.py      # 接入暂停/恢复/降级
├─ model_gateway.py / runs.py  # 接入预算/取消事件
└─ migrations/versions/00xx_autonomy_event.py
platform-api/tests/            # 政策矩阵、问题上限、预算/风险暂停、防扩权
```

### E.5 验收证据要求

- L2 无人值守 run 完成；
- 模糊需求触发问题包；
- 预算/风险/失败阈值触发；
- 不可逆操作被拦截；
- 无自我扩权/无限循环；
- 审计链完整。

---

# 第六阶段｜F：端到端自主交付验收与阶段收口

## F｜用一个陌生项目验证「人只提需求」的完整闭环，再决定是否扩大规模

- **Goal：** 选一个**陌生领域、可测试、1—2 万行**的项目，从原始需求跑到可交付件；开发过程零人工介入（澄清问题与不可逆授权除外），并用指标决定是否进入更大规模。
- **Context：** A—E 各自的机制通过不等于端到端自主成立。F 是「整机试飞」，必须用真实运行证据，不得用模拟或单模块样例代替。
- **Constraints：**
  - 不直接挑战 10 万行；先 1—2 万行，达标后再单独立项扩展；
  - 沙箱、无生产副作用、无真实付费/外部写；
  - 指标与阈值事先约定，不得事后修改口径；
  - 失败/`NOT_RUN` 如实记录；不得人工改源码后计成功；
  - 不达标就停，回到 A/D/E 定位，不靠加 Agent 硬推。
- **WhenDone：**
  1. 一个陌生需求端到端完成：意图契约 → 架构/模块契约 → 模块实现 → 独立验证 → 集成 → 交付包；
  2. 开发过程人工介入次数为 0（澄清回答/不可逆授权不计）；
  3. 验收计划覆盖率 100%（可执行或显式延期）；
  4. 所有必需检查 PASS，或未通过项有明确归因与下一步；
  5. 指标达标（见 F.2）；
  6. 形成 `docs/PHASE4_ACCEPTANCE.md` 与 go/no-go 结论。

### F.1 试飞步骤

1. 选定项目与预算；冻结验收阈值。
2. 以 L2 自主等级运行；记录每次提问、暂停、升级、重试。
3. 结束后做**独立复盘**：哪些环节仍需人、哪些假设错误、哪些验收不可执行。
4. 用同一需求重跑一次，观察稳定性（不得只报最好一次）。
5. 输出 go/no-go。

### F.2 建议指标与阈值（可经开发者批准调整）

| 指标 | 建议阈值 | 说明 |
|---|---|---|
| 无人干预完成率 | ≥ 90% | 澄清/授权不算开发介入 |
| 验收覆盖率 | 100% | 可执行或显式延期 |
| 模块首次验收通过率 | ≥ 80% | 不含人工改码 |
| 集成一次通过 | ≥ 1 次完整闭环 | 记录失败归因 |
| 需求追溯完整率 | 100% | 需求→验收→模块→证据 |
| 成本/千行 | 记录并对比基线 | 不设硬阈值，先测量 |
| 工期/千行 | 记录并对比基线 | 不设硬阈值，先测量 |
| 逃逸缺陷（验收后） | 0 个 P0 | 安全/数据/权限 |
| `NOT_RUN` 诚实度 | 100% | 不得伪 PASS |

### F.3 阶段收口

- 更新 `docs/PHASE4_ACCEPTANCE.md`：证据、指标、失败样本、限制。
- 更新后续路线：是否进入十万行/并行/向量检索/外部集成，单独立项。
- 修订第三阶段「明确排除」清单：只修订已被证据支持的条目，其余保留。
- 若不达标：写明缺口属于 A/B/C/D/E 哪一层，禁止用「再换一个大模型」掩盖。

---

## 2. 里程碑与开发执行门禁

| 里程碑 | 必备步骤 | 判定依据 | 不等于 |
|---|---|---|---|
| G0｜基线 | C00 | 仓库/回归/旧锁证据 | 不代表已有自主能力 |
| G1｜意图与验收 | A | 可执行验收计划、假设台账、篡改检测 | 不代表架构/实现可用 |
| G2｜架构与契约 | B | 模块图、接口先行冻结、追溯 100% | 不代表模块已实现 |
| G3｜层级编排 | C | 双模块端到端、越权拒绝、集成门禁 | 不代表无人验证 |
| G4｜独立验证 | D | 三层验证、证据失效、负例捕获 | 不代表自主策略成立 |
| G5｜自主策略 | E | L2 无人值守、问题包、预算/风险暂停 | 不代表整机成功 |
| G6｜整机试飞 | F | 陌生项目、指标、go/no-go | 不代表十万行已可 |

**门禁规则：**

- 任一步骤未取得真实证据 → 下一步阻塞；
- 所有新开关默认关闭；关闭时旧路径零漂移；
- 每步结束必须有回归：旧 Task 冷启动 + 旧 asset-lock 重放。

---

## 3. 每一步统一验收记录模板

```markdown
### 实施记录：[A / B / C / D / E / F]
- 基准 Commit / 结束 Commit：
- 当前状态：PASS / PARTIAL / FAIL / NOT_RUN / BLOCKED
- Goal 是否达到（证据）：
- 前置依赖与门禁核对：
- 新增/修改文件（真实路径）：
- Schema/契约/锁/迁移版本兼容性：
- 需求 → 验收 → 模块追溯：
- 模块责任与可写路径 / 越权拒绝：
- 测试命令与逐项 PASS/FAIL/NOT_RUN：
- Runner 实际执行 / Mock 与 Live 区分：
- 路由实际 provider/model（无秘密值）：
- run_id / task_id / call_id 账本与 UNKNOWN：
- 自主等级、提问/暂停/升级事件：
- 证据绑定与 stale 检测：
- 旧 Task 冷启动 / 旧 asset-lock 重放：
- WhenDone 逐条结论：
- 遗留风险、需批准事项、下一步：
```

---

## 4. 需要开发者明确批准的事项

1. **第四阶段立项本身**，以及对第三阶段「明确排除」清单的修订范围。
2. **自主等级 L2/L3 的启用**、对应项目类型与沙箱边界。
3. **预算上限、时限、重试上限、问题包上限**与费用口径。
4. **模型/Provider 变更**（包括新增验证角色所需的模型）与地域/工具探针结果。
5. **并行波次**所需的 worker/Runner 资源、SQLite 租约/锁或数据库扩容。
6. **不可逆操作清单**与授权方式（生产发布、真实付费、外部写、真实 Live 第三方）。
7. **试飞项目选择与验收阈值**；阈值一经确定不得事后修改。
8. **任何放宽安全边界的改动**（网络、宿主 Docker、秘密访问、公共资产自动入库）。

---

## 5. 执行后的架构结果与不做的内容

实施到 F 后，预期形成：

```text
用户目标 + 约束（可粗略 / 可指定框架）
   └─ A 意图契约 + 可执行验收计划（冻结）
        └─ B 架构契约 + 模块责任图 + 接口先行冻结
             └─ C 系统编排器（SystemRun → ModuleRun，串行/受限并行）
                  ├─ 模块 Agent（仅 owned_paths）
                  ├─ 接口发布（验证后 VERIFIED）
                  └─ 集成门禁（F06）
                       └─ D 独立验证 + 证据链
                            └─ E 自主策略（L0—L3，继续/提问/暂停/升级）
                                 └─ F 整机试飞 + go/no-go
```

**明确不做：**

- 不重写 MVP，不推翻既有 Registry/Resolver/Method Unit/Runner；
- 不默认引入向量数据库/新模型/路由决策模型；
- 不引入模型型常驻 Supervisor 或无限自主委派；
- 不做生产公网执行、自动收费、多租户、自动资产入库；
- 不让实现者验收自己的产出；
- 不以 Agent 数量/调用次数/文档篇幅衡量成功；
- 不伪造 PASS，不把 `NOT_RUN` 改成通过。

---

## 6. 来源文档对照（供编码工具核查）

- `AgentForge_01_MVP_分步开发.md`：M01—M28 与 MVP 边界；固定验收与不可伪造成功的原则。
- `AgentForge_02_迭代增强_分步开发Prompt_方法索引版.md`：R06 冻结验收、R13 DAG、R14—R19 受限 Agent、R20—R24 预算/恢复、E01—E10 扩展与偏差说明。
- `AgentForge_03_遗留闭环与模块自治_分步开发Prompt.md`：C/F/X 阶段、模块 Agent 原则、F01—F08 机制与「未接入生成图」事实、明确排除清单。
- `docs/PHASE3_BASELINE.md`、`docs/F01—F08`、`docs/DEMO_P0_ACCEPTANCE.md`：当前仓库事实与 `NOT_RUN` 的权威依据。
- `Demo技术文档_修订版_v0.3`、`Demo需求分析文档`：既有技术边界、验收要求与后续扩展方向。

> **最终准则：** 本文档是「如何从当前项目继续走向自主交付」的增量指令，不是对既有能力的重新认证。任何一步开始前发现仓库事实与本文不一致，应暂停、记录差异、按实际代码提出最小修订；不得按想象创建重复组件，也不得宣布「已自主」。
