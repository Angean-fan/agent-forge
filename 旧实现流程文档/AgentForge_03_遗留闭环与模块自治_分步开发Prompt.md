# AgentForge｜第三阶段：遗留闭环与模块自治多 Agent 架构｜分步开发 Prompt

> **文档性质：** 面向 AI 编码工具的**增量实现流程**；不是既有仓库的代码审计报告，也不是对历史步骤的重新验收。
>
> **编制基线：** 2026-10-13。依据《AgentForge_01_MVP_分步开发(1).md》《AgentForge_02_迭代增强_分步开发Prompt_方法索引版(1).md》文末 **2026-10-13 实现状态与偏差说明**、《Demo技术文档_修订版_v0.3(2).docx》与《Demo需求分析文档(2).docx》。旧版技术文档 v0.1/v0.2 仅用于追溯，不覆盖 v0.3。
>
> **进度声明的边界：** 开发者声明 `M01—M28`（含 `M08A/M15A`）已完成；最新第二阶段实施记录列出部分 R/E 能力已有实现，同时列出 S1—S4 结构性偏差和 E 步骤的部分通过/`NOT_RUN`。**以实际 Git 仓库、测试和运行记录为最终事实来源**。本文件绝不自动把历史的“具备实现/模拟测试通过”改写成“生产入口已经接通/端到端已通过”。
>
> **目标：** 不重做 MVP、不推翻现有 Registry/Resolver/Method Unit/Runner 的前提下，先消除关键接线与交付偏差，再增加**每个业务模块拥有明确 Agent 责任、独立长期记忆、受控读写范围和标准化公共接口**的模块自治机制。允许多个**逻辑**模块 Agent，初期仍使用**单 worker、串行任务、共享预算**；并行与任意自主委派均后置。
>
> **新增实施编号：** `C00—C06` 为现存差异闭环，`F01—F08` 为新模块自治功能，`X01—X03` 为条件性/可选运行时扩展。这些编号不改写既有 `M/R/E` 的任务号、完成标记或验收证据。

---

## 0. 使用方式：每次仅交付一个步骤

执行顺序建议：

```text
C00 仓库事实核对（所有步骤的共同前置）
  └─ C01 模型地域/工具探针与生产路由接线
       └─ C02 规划→缺口→受限 Patch→Runner 的真实闭环
            ├─ C03 Integration Pack 装配/打包与 E03/E04 状态闭环
            │    └─ C04 E07 文档→确认→Adapter 受控生成闭环
            ├─ C05 E09 既有项目真实增量迭代闭环
            └─ C06 E10 可编译资产认证 + S4 模型路由断言

C00 + C02
  └─ F01 模块责任/所有权模型
       ├─ F02 模块持久记忆
       └─ F03 模块公共接口与跨模块能力发现
            └─ F04 按模块实例化受限 Agent（要求 F01/F02/F03、C02）
                 └─ F05 跨模块变更协调（推荐具备 C05 的增量能力）
                      └─ F06 模块集成验证、恢复与回滚
                           └─ F07 单 Agent/模块 Agent 对照评估
                                └─ F08 并行调度 [可选、默认关闭]

X01 YAML OpenAPI 支持 [可选，依赖 E06 与 C03]
X02 E05 真实第三方 Live 验证 [人工确认后、可选，依赖 C03/C04]
X03 E08 真实在线预览 [基础设施确认后、可选，依赖稳定 Runner]
```

**注意：** 以上是**依赖图**，不是要求把 C03—C06、X01—X03 全部做完后才能开始 F01。F01/F02/F03 的纯确定性部分可以在 C02 验证通过后推进；凡未满足依赖的步骤应阻塞而不是猜测。`X` 分支不影响核心模块自治验收；C04/C05/C06 可因运行环境、范围或人工审核条件被合理延后，必须如实记录影响。

### 总控 Prompt（每个步骤开始前附上）

```text
你是 AgentForge 的增量实施型编码助手。只执行本次指定的 Cxx/Fxx/Xxx 一步；禁止擅自执行后续步骤。

【先检查再编码】
1. 查看 Git 状态、真实目录、README、依赖锁、Profile、冻结 spec/contract、Registry、Resolver、asset-lock、生成图、worker/default_context、Runner、测试与 docs/MVP_ACCEPTANCE.md；结合 docs/DEMO_P0_ACCEPTANCE.md、docs/E_MILESTONE_ACCEPTANCE.md、docs/E01...E10 实施记录核查当前事实。文档中提到的路径只为线索，不保证仓库存在。
2. 对照本步 Goal/Context/Constraints/WhenDone 列出“已有 / 部分 / 未有 / NOT_RUN / 外部阻塞”，明确本次计划改动文件、输入输出契约、测试。若前提未满足，停在检查结论，禁止补做已完成 MVP。
3. 优先最小增量与适配器；禁止重写历史 Registry/Resolver、已发布资产、冻结 spec/asset-lock、固定验收测试。新旧 schema/锁必须区分版本，旧锁用旧字段集合计算哈希并可原样重放。
4. 保留“确权规格 → Asset Finder（只读召回）→ Registry/Resolver（确定性兼容决策）→ COPY/RENDER/STRUCTURED_EDIT → 有明确 Gap 才 AGENT_PATCH → 受限生成”的优先级。索引命中、模型建议、模块记忆或方法名相似不等于已覆盖业务验收。
5. 模型路由固定：用户语言理解/提问/可读解释为 DeepSeek 官方 deepseek-flash；技术规划/契约提议为百炼 qwen3.7-plus；受限编码/普通修复为 qwen3-coder-flash；满足既有升级条件的疑难修复为 qwen3.8-max。不得新增路由决策模型、无授权更换模型，未通过地域/Function Calling 探针则阻塞。
6. 每次模型调用（含子 Agent、重试、工具循环）走平台网关、预留/幂等结算、共享限额并标注 run_id/task_id/call_id；没有真实 usage/价格证据标 UNKNOWN/NOT_RUN，不得记零或伪造 PASS。
7. 不可信输入包括自然语言、外部文档、OpenAPI、模型返回和生成代码。秘密不得进入 Prompt、检查点、日志、ZIP 或长期记忆；Agent 无任意 shell/网络、宿主 Docker Engine 权限或已发布公共资产写权限。
8. 生成任务只可在 task.allowed_paths 与 module.owned_paths 的**交集**内通过受控暂存 Backend 写入；共享目录必须有显式唯一责任人。编码工具对全仓库的编辑权限不代表运行时 Agent 具有同等权限。
9. 审查或测试不得通过删除/放宽固定断言来制造成功。记录 PASS、FAIL、NOT_RUN、阻塞原因与日志证据；有源代码变更则执行实际可运行的单元/契约/集成测试和原 Task MVP 冷启动回归。
10. 所有模块代理首先复用确定性资产；模块职责不等于永驻进程，不等于额外买一套模型。初期按原 DAG 串行执行，默认禁用自主通用 task 委派、并行提交以及自动发布公共 Method Unit。

【本步完成后的固定输出】
- 本次步骤编号、仓库 commit/干净状态、已存在能力与差距；
- 目标与实际变更文件、schema/API 兼容策略、迁移/回滚说明；
- 测试命令、真实 PASS/FAIL/NOT_RUN、旧 Task 冷启动及旧 asset-lock 重放结果；
- Agent 权限/调用路由/费用与密钥扫描结果（不泄漏敏感值）；
- 每条 WhenDone 的判定与证据位置；
- 剩余风险、尚需开发者确认的事项、下一步依赖。
```

### 不可改变的既有边界

| 边界 | 继承方式 |
|---|---|
| 生成对象 | 继续以已认证 Vue 3（JS/TS）+ Spring Boot（MyBatis/MyBatis-Plus）+ MySQL/Maven Profile 为基线，不擅自引入微服务或新框架。 |
| MVP / Demo P0 | `M01—M28` 不重做；原 `R01—R27` 的固定验收与 9 次评估门槛不因本次改造而变化。 |
| 资产真值 | 已发布 Template/Capability Pack/Method Unit/Integration Pack 的源码与 manifest 为权威；FTS5 只是可重建的候选索引。 |
| 资产发布 | 当前 run 通过不等于公共复用资产可发布；仍须 E10 的来源/许可证、独立兼容测试、人工审核、不可变版本。 |
| 任务与预算 | 现有 TaskPlan/DAG、run/task/模型调用账本、取消/暂停/检查点/Runner 验收优先复用，不创建第二套独立计费与任务真值。 |
| 文件与安全 | 运行期最小读权限、受保护文件只读、写入暂存区、校验后原子提交；无任意 shell/网络，禁止破坏沙箱。 |
| 成功语义 | 必需项非 PASS 不得标 SUCCEEDED；代码交付状态与第三方 LIVE readiness 分开。 |
| 子 Agent | 初始模块自治是“按模块装载独立配置/记忆的逻辑 Agent”，不默认启用 Deep Agents 内置通用自主委派或任务并行。 |

---

## 1. 已有实施事实与未闭环事项（来自 2026-10-13 记录）

| 来源编号 | 文档记录的现状 | 本文处理步骤 | 是否阻塞模块自治 |
|---|---|---|---|
| MVP M01—M28 | 开发者声明全部实现；保留证据，不重做 | C00 只核对 | C00 必须核对 |
| R01—R27、R06A—R06D | 第二阶段描述增量能力及测试；不能仅凭任务文字认为生产全部接线完成 | C00 读取实际验收报告；后续按差异修复 | 仅相关能力作为前置 |
| S2：Qwen 路由生产接线 | R13/R16/R18/R19 能力可测，但 `worker/default_context` 未注入生产客户端/引擎；真实工具地域探针待完成 | C01、C02 | **是**，F04 前必须闭环 |
| S1：Integration Pack | E01/E02 样例/测试存在，生成图装配、Port 注入、打包指南未贯通 | C03 | 否，除涉及第三方的模块 |
| S3：DeepSeek 可选调用 | E03/E07/E09 有解释/确认调用点但无凭据默认不调用 | C01、C04、C05 | 仅涉及对应运行时路径 |
| S4：E 里程碑跨厂商断言 | R27 有路由检查；E 里程碑无独立“无意外跨厂商调用”断言 | C06，后续 F07 复查 | 否，但应补测 |
| E03/E04 | 指南未进入生成 ZIP；`spec.external_requirements` 未回填；`external_readiness` 为读时派生而非应有落库快照 | C03 | 否，依场景启用 |
| E05 | 真实 Live Runner `NOT_RUN`，需供应商测试环境、网络/凭据/审批 | X02 | 否，**明确保持 NOT_RUN** 直至条件就绪 |
| E06 | JSON OpenAPI 演示/校验满足当前记录；YAML 不支持 | X01 | 否，可选 |
| E07 | 文档规则抽取已有；确认后的集成规格尚未真正驱动受限 Adapter 生成 | C04 | 否，依第三方需求启用 |
| E08 | 生命周期和鉴权模拟已测；实际隔离预览实例 `NOT_RUN` | X03 | 否，**明确保持 NOT_RUN** 直至基础设施确认 |
| E09 | 新 spec + 新 run 有部分支持；DeepSeek 交互链路未调用；需真实变更/迁移回归 | C05 | F05 增量扩展场景推荐先完成 |
| E10 | 资产审核闭环部分实现；跨 Profile 的资产测试是文本校验，未编译；提名仍为人工 | C06 | 否，F03 不得假设资产已完成编译认证 |

**严格区分：** `E01/E02` 的“已满足”是文档描述的资产/测试层面的结论，**不是** S1 端到端生成链路已满足；`E05/E08` 的 `NOT_RUN` 不得因增加模拟测试而改为 PASS。

---

# 第一阶段｜C：现存差异最小闭环

## C00｜仓库事实核对、回归基线与变更开关

- **Goal：** 在不修改核心生成逻辑的情况下，建立可靠的当前能力清单、可重放的 MVP/第二阶段回归与新功能 feature flag 基线，确认 S1—S4、E01—E10 的实际状态。
- **Context：** 开发者声明 M01—M28 完成；《第二阶段》文末列出 S1—S4 和 E 分步偏差，指出 `docs/MVP_ACCEPTANCE.md`、`docs/DEMO_P0_ACCEPTANCE.md`、`docs/E_MILESTONE_ACCEPTANCE.md`、E01—E10 实施记录与 Git 历史可作核验线索。R06A 的旧锁哈希与 R06D 的索引快照要求不能因新 schema 改变。此步是 C/F 的共同前置，不是重新实现历史功能。
- **Constraints：** 先使用真实仓库验证文件、迁移、入口、Runner、Provider 记录是否存在；没有权限/环境的外部测试标 `NOT_RUN`。不假定全部 R/E WhenDone 已真实通过；不改旧 ZIP、Profile、`asset-lock.json` 和冻结断言。给 `module_agents_enabled=false`、`integration_pack_generation_enabled=false`、`module_parallel_enabled=false` 等新增开关时应遵循现有配置模式，**仅在确有需要时添加，不杜撰已存在字段**。保留兼容旧数据快照与恢复路径。
- **WhenDone：** 输出 `docs/PHASE3_BASELINE.md`（建议路径，依仓库为准），逐项列出“已证实 PASS/部分/NOT_RUN/阻塞”的依据、commit、测试命令和证据；原 Task MVP 冷启动与旧 asset-lock 重放结果可定位；所有新开关默认为关闭，关闭时原生成/打包行为和固定检查无回归；缺失证据不得伪造。

## C01｜Qwen 真实工具探针与生产模型路由注入（S2/S3 前置）

- **Goal：** 将 R13 技术规划、R16 编码、R18 复杂修复的已实现调用能力按显式开关接入真实 `worker/default_context/GenerationContext`，完成模型地域、Function Calling、账本与超时探针。
- **Context：** 第二阶段 S2 记录 Qwen 三角色“可测但生产入口未注入”；R16 要求 `qwen3-coder-flash` 在所选地域做真实受限工具调用；R20/R21 要求内部模型请求逐次预留与结算。S3 记录 DeepSeek E03/E07/E09 调用点具备但无凭据时不调用。
- **Constraints：** 不新增模型；使用既定 `user_interaction`、`technical_planning`、`coding`、`complex_repair`。无凭据时保持默认安全禁用并报 `NOT_RUN/CONFIG_REQUIRED`（实际错误码依项目标准），**不能**换其他模型、伪造调用成功、打印密钥。探针至少验证实际工具消息、暂存文件写入、Provider/地域/接口版本、思考模式支持、usage/price_version；任何 UNKNOWN 不写零。路由注入只能发生在合法开关、授权预算与兼容性检查后；禁用时标准资产路径零 Qwen 请求。
- **WhenDone：** 有 Provider/地域/日期/真实工具调用证据或明确阻塞报告；生产入口能通过显式注入获得授权模型客户端；模拟/真实调用的 run_id/task_id/call_id、共享预留/结算及重试均可审计；开关关/开各有测试，“无意外跨厂商调用”断言覆盖启动与缺 Key 情况；MVP 冷启动仍 PASS。

## C02｜技术规划、受限 Patch、修复和 Runner 生产链路闭环（S2）

- **Goal：** 实现真实生产生成路径中 `冻结 spec → Asset Finder/Resolver →（必要时 Qwen 规划）Task DAG → Gap 检查 → 受限 DeepAgent Patch/修复 → Runner 全检 → ZIP` 的受控端到端联通。
- **Context：** 依赖 C01；复用 R13 TaskPlan、R14 Gap、R15 Backend、R16 Agent、R17 Patch、R18 修复、R19 完整复验及 R20—R24 预算、限制、取消恢复。当前文档已确认这些能力有可测试实现，但没有保证生产路由接线。无 Gap 时仍应走确定性 COPY/RENDER 路径。
- **Constraints：** **只为真实 uncovered_acceptance_ids 创建 Patch 任务**；不能用模型主观说“缺少方法”绕过 R06A—R06D 的兼容性筛选。Agent 写权限限 `allowed_paths`，不得改测试、模板原件、Profile、锁、Registry、索引或注入任意命令。默认没有通用自主子 Agent 委派，单 worker 串行。Runner 必须实际运行原必需构建、数据库和 HTTP 验收；错误分类和修复次数沿用 R18 限额；完整复验失败不能成功打包。
- **WhenDone：** 至少给出两个真实 run：①标准已覆盖 Task 项目，Qwen 编码调用数为零且旧行为不变；②人为设计一个可授权 Patch 的业务缺口，生产路径真实进入正确模型、受限暂存提交、Runner 复验且报告记录 Gap/资产来源/usage；越权写入、无预算、Provider 无工具能力、Runner 失败、取消/恢复测试按实际结果展示；不可运行项 `NOT_RUN`，不能假称端到端 PASS。

## C03｜Integration Pack 正式装配与 External Requirements 交付（S1/E03/E04）

- **Goal：** 将既有 E01/E02 的 Mock-only 集成资产最小接入生成图、Resolver/装配器、生成项目内部 Port 依赖和打包步骤，使 `.env.example`、`EXTERNAL_SERVICES.md` 与运行就绪状态真正进入所生成产物。
- **Context：** 依赖 C00/C02；第二阶段 S1 指出 Integration Pack 目前是平台模块与示例资产，未在 `package_run` / 生成 Service 中使用；E03 指出 `spec.external_requirements` 未回填；E04 指出 `external_readiness` 未落库。技术文档 v0.3 第 6 节要求内部 Port/Adapter/Mock 与配置指南；E01/E02 已有演示契约，不能重做其核心。
- **Constraints：** 使用显式 feature flag，仅对经验证、在允许范围内的 Mock-only Pack 生效；默认普通 Task 产物**零漂移**。Pack 必须经 Registry/Resolver 锁定版本、兼容关系和 checks，不能绕过或修改已发布资产。生成 Service 仅依赖内部 Port；变量与配置路径需 1:1，拒绝冲突；外部配置仅保留占位，永不写入真实 Key。`external_readiness` 需要按已确认规格/运行快照设计权威持久化语义，同时提供旧数据无侵入迁移；Mock-only 与 Live verified 不得混淆。未知依赖/密钥/服务不得自行下载、请求或伪造。
- **WhenDone：** 同一 Mock-only Pack 在开启开关时可由 spec → lock → 安全装配 → 运行契约测试 → ZIP 打包，生成的 Service 经 Port 调用 Mock，`EXTERNAL_SERVICES.md` 和 `.env.example` 指向实际配置键；report/DB/API 一致显示 `MOCK_ONLY` 或待配置，并记录必要时的 NOT_RUN；普通 Task、旧 ZIP/锁、无 Pack 情况不变；对密钥泄漏、变量冲突和外部网络均有负例测试。

## C04｜仅有供应商文档时的待确认集成规格与受限 Adapter（E07/S3）

- **Goal：** 将 E07 已有的规则式文档提取与待确认清单，串成 `不可信文档 → 待确认 Integration Spec → DeepSeek 提问/用户确认 → 冻结集成契约 → Qwen 受限 Adapter 任务 → Mock 契约测试` 的真实链路。
- **Context：** 依赖 C01/C02/C03；第二阶段 E07 记录当前只完成规则式提取和 DeepSeek 调用点，确认后的集成规格尚未驱动 Adapter 生成。沿用 E06 已认证 JSON OpenAPI 路径和 E02 Port/Adapter/Mock 设计；用户交互必须使用既定 DeepSeek 路由。
- **Constraints：** 首个用例仅模拟服务，无凭据、无真实收费或副作用；来源不全、鉴权/回调不明则保持 pending 并暂停。来自外部文档、OpenAPI 的内容视为数据，禁止把提示注入当工具权限；模型不得无许可安装 SDK、修改 Port 之外的受保护文件或宣称 Live Verified。不得把原始供应商文档或未确认聊天当作冻结业务要求；新 Adapter 仅属于当前项目，**不自动发布公共资产**。
- **WhenDone：** 缺少关键字段的文档真实产生问题并暂停，用户确认后签名/配置/错误约定被冻结；演示 Adapter 仅在批准的目录变更、Mock 成功/失败/超时契约均测过；缺凭据时调用列为 NOT_RUN 且可以保留草稿；工具权限/模型路由/账本记录可核查，普通 Task 无回归。

## C05｜E09 生成项目的真实“已存在模块”增量迭代链路

- **Goal：** 在已完成 ZIP 和旧锁保持不可变的基础上，实现 `确认变更 → 新 spec 版本 → 影响分析 → 必要资产重新锁定 → 最小增量修改 → 数据迁移/回归 → 新 run 与新 ZIP` 的真实路径，为后续模块 Agent 延续记忆提供可靠版本边界。
- **Context：** 依赖 C02，建议在 C03 后验证含 Integration Pack 的项目；E09 现有 `new spec + new run + relock_required` 部分实现，但文档明确 DeepSeek 调用点尚未运行，未能据此证明完整端到端行为。R23 对旧锁、checkpoint 和已提交产物有一致性要求。
- **Constraints：** 只支持 AgentForge 自己生成、携带已验证 asset-lock 的项目，不开放任意仓库改造；用户确认意图由 DeepSeek 负责，技术影响分析由 Qwen 负责，程序冻结新版本。只有语义/契约变化需要重新锁定时才按规则重选资产；**显式重新冻结不要求锁字节必然改变**；不能原位修改旧 run/ZIP/报告或历史记忆。变更迁移必须可复现、可回滚；预算/失败保留旧成果。
- **WhenDone：** 对已验收 Task 项目新增一个确认字段或规则，可生成新的 spec/run/ZIP，旧 ZIP/锁/报告可重放；变更必要的代码和迁移验证通过，未触及模块保持 hash/语义稳定或解释合理变化；确认中断、路由禁用、relock_required、回滚和失败情形具备可定位证据。

## C06｜E10 Method Unit 编译级认证、资产发布审计及 E 路由断言（E10/S4）

- **Goal：** 补齐 E10 文档已指出的“只做文本契约校验、未编译”的缺口；补全 E 里程碑跨厂商调用的明确断言，不改变已有公共资产发布的人审机制。
- **Context：** 依赖 C00/C02，推荐在 C05 后使用实际 Patch 案例；E10 已有候选、审核、索引发布流程和至少部分测试，但当前独立/跨 Profile 契约仅做文本校验。S4 记录 E 范围缺少独立“无意外跨厂商调用”断言；R06A—R06D 已规定 Method Unit 的源真值、哈希、索引与兼容门禁。
- **Constraints：** 测试要使用真实受支持 Profile 编译/运行、独立最小调用者及必要负例，不以 grep/文本相似度代替签名兼容和行为检查；不能仅因某次 run 成功就自动写公共库。继续执行来源/许可证/敏感信息/通用性审查、人工批准、不可变版本/checksum 与原子 FTS5 更新；若缺独立可用运行环境则标 NOT_RUN 并不得发布为“编译认证通过”。E 范围与 F 范围均要检查未经授权的跨厂商调用（含预期零调用）。
- **WhenDone：** 一份真实可编译的候选 Method Unit 具备受控 Profile 构建、契约/业务测试、拒绝案例、人工审批和发布证据；已发布旧版本和老锁不变，新索引按同 manifest 可重建；如果环境不支持完整编译，明确阻塞而非 PASS；E 范围模型路由断言覆盖正常、缺 Key、禁用分支与必要调用路径。

---

# 第二阶段｜F：模块自治与接口驱动的多 Agent 扩展

> **本阶段新增的模块 Agent 指“生成出来的用户项目”的业务模块维护者，**不是**专门监管 AgentForge 平台自身 Python/FastAPI/Vue/Runner 子系统的 Claude Code 助手。平台仓库开发者是否采用类似 Code Review 子 Agent 属另一个工具层决策，不混用权限、记忆或状态存储。
>
> **最小演示：** 在既有 Demo 支持范围内使用两个有真实依赖关系的业务模块（可以是用户资料与订单；如超出现有认证实体/页面限制，改用适配既有验收的活动/报名模块），不引入额外业务角色、微服务或新技术栈。

## 模块 Agent 的职责与依赖原则

- **模块独立：** 每个模块拥有稳定 `module_id`、责任范围、允许文件、私有记忆及对外契约。它不对其他模块的内部实现负责，**但必须遵守公共接口、冻结规格和全局安全约束**。
- **实例按需：** Module Agent 是可恢复的任务执行配置，不要求每个模块永久存活。物理上可以共享一个 DeepAgent 运行组件或相同 Coding 模型，逻辑上严格隔离记忆与文件权限。
- **统一协调：** LangGraph + Python Coordinator 负责 DAG、依赖、共享预算和状态；不让模块 Agent 之间进行无约束聊天或共同写同一源文件。
- **可发现接口：** 项目内公共接口是 `Module Interface Registry`；跨项目审核复用才进入 `Method Unit / Capability Pack Registry`。两者不得混为一谈。
- **三层上下文：** 冻结的全局只读约束（spec/contract/profile）+ 当前模块私有记忆/代码 + 被批准的其他模块**公共接口契约**；不得把其他模块的全部开发聊天/源码/记忆载入上下文。
- **两类版本：** `module memory revision` 用于长期任务连续性；`public contract version` 用于模块依赖兼容性；二者都不能替代冻结 spec/hash 和 asset-lock。

### F 阶段建议逻辑数据契约（非已有实现断言）

```yaml
module:
  project_id: "<project>"
  module_id: "order"
  based_on_spec_hash: "<frozen-spec-hash>"
  profile_ref: "<locked-profile>"
  owner_role: "module_owner"
  owned_paths: ["backend/src/main/java/.../order/**"]
  readable_contract_refs: ["user.find_by_id@1"]
  depends_on_modules: ["user"]
  memory_revision: 1
  contract_revision: 1
  execution_mode: "SERIAL"
  lifecycle: "READY"

public_capability:
  capability_id: "user.find_by_id"
  owner_module_id: "user"
  interface_kind: "JAVA_METHOD"  # 项目内 Java 方法；外部 API 使用 HTTP/OPENAPI
  symbol: "UserQueryService.findById"
  signature: "UserDTO findById(Long userId)"
  args_schema_ref: "<versioned-schema>"
  returns_schema_ref: "<versioned-schema>"
  errors: ["USER_NOT_FOUND"]
  auth_constraints: ["<verified-business-permission>"]
  version: 1
  status: "VERIFIED"  # DRAFT/VERIFIED/DEPRECATED
  verification_ref: "<runner-evidence>"
```

说明：上面只是模型设计示意。真实字段名称、数据库表、源文件位置必须先检查仓库 schema 后确定；`owned_paths` 是受控权限表达式，不允许通过 `..`、绝对路径、符号链接或通配符泛化出沙箱。公共接口的 DTO/错误/鉴权语义由已冻结契约和真实代码/测试共同校验，**模型自报 verified 无效**。

## F01｜模块边界识别、Module Registry 与唯一写入所有权

- **Goal：** 在现有冻结 spec、R13 TaskPlan/DAG 和 R15 `allowed_paths` 基础上引入**可验证的业务模块责任图**，为每个模块记录稳定 ID、职责、依赖、只读公共资源、独占写入路径及模块生命周期。
- **Context：** 依赖 C00、C02 的真实基线；目前 R13 按 task_id 规划，R15 按 task.allowed_paths 授权，尚无独立的“模块所有权”概念。MVP 已有模板/能力包可能共享认证、DTO、响应、全局配置等文件，应先定义这些共享文件由模板/集成层拥有，不能隐式分给多个模块 Agent。
- **Constraints：** 用规则优先从已冻结实体/业务能力/任务依赖生成模块候选，必要规划只能用现有 Qwen 技术规划提议，最终由 Python 校验；不改变 spec 语义或业务范围。模块需支持零/一个/多个任务；写入生效权限为 `task.allowed_paths ∩ module.owned_paths`，共享目录有唯一 owner 或明确的协调任务；空交集、路径冲突、依赖环、未知模块、越界路径一律阻塞。初期关闭 module Agents 时走旧任务调度，无额外模型调用。
- **WhenDone：** 单模块旧 Task 路径无变化；双模块场景生成稳定模块登记与无环依赖图，唯一写入权检查覆盖路由、DTO、测试、migration 和公共文件；两个模块同时声明同一文件/对方目录被拒；Module Registry 可持久化/重放、版本化迁移不破坏老 `RunTask` 与 asset-lock。

## F02｜Module Memory Store：模块长期记忆、版本隔离与可信更新

- **Goal：** 为模块提供可跨任务、跨新 run 使用的**专属版本化长期记忆**，恢复时只选择本模块的可信事实和必要公共信息，并确保记忆不凌驾于冻结规格、接口版本或源码真值之上。
- **Context：** 依赖 F01；R23 的 checkpoint 解决执行恢复，**不是**模块跨 run 的长期记忆。C05/E09 已有“新 spec、新 run”边界可供记忆绑定；技术文档 v0.3 第 6 节要求 TaskContext 仅含必要规格片段、授权文件和关联依赖。不强制引入向量数据库或新的云存储。
- **Constraints：** 以 `(project_id,module_id,spec_hash 或 run 版本,memory_revision)` 和验证证据索引记忆，重要决策关联 `task_id/commit_hash/contract_version`；只读加载模块记忆，写入由受控服务依据**已提交且通过检查的结果**执行，不让模型直接覆盖存储。旧记忆与新冻结 spec/接口不兼容时自动失效或进入待验证，不能生成隐式业务规则；模块 A 不得读写 B 私有记忆。限制摘要体积、保留审计与清理策略；不保存秘密、原始对话或隐含推理。回滚代码版本时记忆指针同步回滚，避免悬空“已完成”记录。
- **WhenDone：** 同一模块两次调用正确恢复经过验证的决策；另一模块无法读取该私有内容，仅能读取允许的接口摘要；规格或接口变化令冲突记忆失效、提示重建；模拟崩溃/重复执行不产生双写或越权记忆；旧 Task 不启用模块记忆仍可原样运行，持久化迁移可回退。

## F03｜Module Interface Registry：项目内能力发布、发现与契约版本

- **Goal：** 让各模块 Agent 在完成自身功能后发布可机器调用/依赖的公共契约（方法、参数、返回、异常、权限、副作用、版本、验证证据），其他模块通过能力 ID 查询和复用，而不读取其私有源码和历史记忆。
- **Context：** 依赖 F01，并与 F02 解耦可独立开发；已有 R06A—R06D Method Unit Registry 记录**跨项目审核代码资产**，R13 已有先冻结的 DTO/SQL/权限契约，E02 也有 Port/Adapter 思路。新 Interface Registry 必须为**当前生成项目运行/版本的已实现接口**建立独立索引，不修改跨项目 Registry 的发布语义。
- **Constraints：** 将已装配 Template/Capability Pack 提供的接口、现有冻结技术契约和新模块实现纳入统一发现层；草稿接口 `DRAFT` 可供规划但不可当作已验证可调用能力；只有源码提交、签名/DTO/错误/权限一致性检查、对应契约测试和 Runner 验证后才能 `VERIFIED`。导出 Java 服务接口时明确 `JAVA_METHOD`，必要 HTTP 接口再用 OpenAPI/HTTP 契约，不能用 URL 混充 Java 类调用；模糊签名、能力 ID 冲突、不兼容变更、过期验证记录均阻塞。允许消费者只读查询，禁止越权修改提供者接口。
- **WhenDone：** User 模块可注册经测试的 `user.find_by_id`，Order 模块以 ID 获取可用签名/错误/权限信息并编译调用、无须读取 User 内部实现；DRAFT 与未经验证的相似方法不被当作可复用已完成能力；破坏性接口升级能阻止或触发依赖迁移；旧 Method Unit/FTS5/asset-lock/Registry 不受影响。

## F04｜按模块实例化 DeepAgent：独立上下文、静态委派与受限 Patch

- **Goal：** 在现有 R16/R17 单任务 patch engine 上引入 `ModuleAgentFactory / ModuleExecutor` 一类的**按需逻辑 Agent**：为每个已分配任务加载模块配置、专属记忆与已批准公共契约，并用原有 DeepAgent 处理真实业务缺口。
- **Context：** 依赖 C02、F01、F02、F03；R15 受限文件 backend、R20 多角色账本、R21 工具限制和 R23 恢复是现成边界。当前 v0.3 明确默认关闭通用自主子 Agent 委派，不能为满足“多 Agent”字面需求直接开放不受控 `task` 工具。
- **Constraints：** 多个模块对应**不同逻辑上下文/权限配置，不一定是不同模型/进程**。调度权归 LangGraph/Python Coordinator，不允许模块 Agent 自发启动其他 Agent、越权追加工具或自行决定发布完成；仅接收当前已确认 spec 片段、模块记忆、可读外部接口、Gap 和受控源码。运行期读写每一步均核对交集，产出 FileChangeSet，提交后 Runner 验收。未命中资产才 Patch；全部子调用继承真实 run/task/call_id 和限额；关闭 feature flag 立刻回退既有单执行器。
- **WhenDone：** 双模块场景中两个逻辑 Agent 的 Prompt/可读上下文互不包含对方私有记忆，文件越权被工具层拒绝；模块无 Gap 时其编码请求数为零；真实 Gap 时仅对应模块发生受限 Patch/重验；每次模型与内部工具调用均可审计，开关关闭回退旧路径 PASS；至少测试一个中断/重试不会重复提交代码或记忆。

## F05｜Module Coordinator：跨模块依赖请求、接口变更及职责冲突协调

- **Goal：** 建立结构化的模块变更请求机制：消费者提出所需能力或契约修改，Coordinator 核验所有权/依赖/DAG/预算后，将变更任务分配给真正负责该模块的 Agent；模块之间只传经过批准的公共接口，不交接内部实现。
- **Context：** 依赖 F01—F04，推荐 C05 已验证真实 E09 规格增量能力；R13 Task DAG 和 R23 检查点可处理串行变更/重放。适用于已有模块的功能扩展，不是创建长时间驻留的“监督者”进程。
- **Constraints：** 明确结构化协议，如 `request_id/requester_module/target_module/capability_id/required_contract/change_reason/acceptance_ids/status/spec_ref`。不得因内部 Agent 请求就改变用户冻结业务规格：涉及新业务范围、权限、技术 Profile 的变更必须回到 DeepSeek 用户确认和新 spec 版本；纯技术缺口由现有 Qwen 提议、Python 确认。模块 A 不得写 B 文件；破坏性公共接口变更要做受影响消费者分析，禁止静默覆盖；自循环/依赖环/反复提议请求设明确上限并转人工阻塞。Coordinator 不是新的“路由决策模型”。
- **WhenDone：** Order 请求 `UserQueryService.findByIds` 时生成可追踪 change request，由 User 模块处理并发布新接口，Order 在依赖满足后继续；双方不得写彼此私有代码；用户拒绝新业务变更会终止或保留草稿而非擅自继续；相同 request_id 重放幂等，过期接口、循环依赖、预算不足和接口版本冲突均有负例。

## F06｜跨模块契约/集成验证、状态提交与恢复回滚

- **Goal：** 以原 Runner、R19 完整复验、R23 幂等/检查点为基础，增加模块级契约检查、模块状态与公共接口发布的可恢复一致性，确保“模块各自成功”不等于“项目整体成功”。
- **Context：** 依赖 F03—F05；R19 规定后端/前端/数据库/HTTP 全部必需验证通过才成功；F03 的 `VERIFIED` 必须引用真实证据，F02 的记忆在代码回滚/新 spec 时必须一致。
- **Constraints：** 静态核对依赖图、契约签名、Java/HTTP 边界、DTO/错误码、权限及状态迁移，再执行原 Runner 必需检查；已验证的模块/接口应绑定 `contract_revision + source_hash + verification_ref`，源码或依赖改变则使旧验证失效。源码提交、公共接口状态和记忆修订要采用可恢复事务/事件对账流程（文件与 SQLite 难以单事务时使用提交状态机），不可先宣布成功后遗漏更新。非业务基础设施错误按原分类报告；失败时丢弃暂存变更或恢复旧版本，不修改老 ZIP。保留原有取消、共享预算、修复上限和冻结验收场景。
- **WhenDone：** 两模块分别通过局部测试但接口类型/权限冲突时整个 run FAIL；一致时完整 HTTP 业务闭环 PASS 才允许生成 SUCCEEDED/ZIP；注入“代码已提交但接口/记忆未落库”和“接口已记录但验证失败”可恢复或回滚到一致状态；取消/预算暂停/崩溃/重放不重复发布或扣费；所有证据关联 module_id/task_id。

## F07｜对照评估、可观测性与模块自治正式启用门槛

- **Goal：** 以固定业务规格、同一 Profile、同一受保护验收和同一预算边界，比较“原单执行器”与“模块 Agent”两种路径，确认模块隔离/复用是否真正提高稳定性，是否反而增加 Token/费用/耗时。
- **Context：** 依赖 F01—F06；延用 R27 的评估习惯，但**不改写原九次独立生成评估和 7/9 建议门槛**。现有成本/索引审计已有 run/task、method candidate、Gap 与资产锁记录，可新增 module_id 与接口查询数据。
- **Constraints：** 至少固定一个单模块基线和一个有跨模块依赖的双模块场景；在可复现条件下重复对照测试（具体重复次数依据可用预算先经开发者确认，不虚报统计显著性）。独立报告首轮通过率、修复后通过率、资产精确/参数化/结构化复用、未覆盖验收 ID、模块接口复用率、越权拦截、单/总模型次数与 tokens、人民币或原币种费用、UNKNOWN、耗时、失败原因、重放一致性。模型不可用时不做“真实成本节省”结论；开销为负收益也要如实记录。新功能以明确开关灰度启用，原单执行器保留为可撤销回退路径。
- **WhenDone：** `docs/MODULE_AGENT_ACCEPTANCE.md`（建议路径）有证据关联的两种路径对照、真实/模拟明确区分、越权与接口变更负例、崩溃恢复和禁用回退测试；满足全部硬性安全/正确性门禁才可把 `module_agents_enabled` 在授权测试环境启用；若性能/成本未达目标，可保留功能但默认关闭，不造假收益。

## F08｜独立模块并行调度（可选，默认禁用）

- **Goal：** 仅在串行模块 Agent 通过 F07 后，探索无依赖关系的模块任务有限并行，同时保证资产锁、模块代码/接口发布、预算与恢复的一致性。
- **Context：** 依赖 F07；原技术文档为单 worker、任务串行、SQLite WAL，尚未将其认证为公众多 worker 并发系统；无状态逻辑 Agent 不意味着安全并行提交。
- **Constraints：** 单独开关和人工批准；先建立依赖 DAG 的无共享写路径证明、每模块暂存隔离、最终合并冲突检测、模型调用并发预算预留与任务幂等，扩容时评估 SQLite 租约/锁、Runner 资源和必要数据库/队列变化。禁止将“文件路径看起来不重叠”当成接口/数据库迁移不会冲突的证明。出现冲突时可退回串行，不延伸到公开任意任务委派。
- **WhenDone：** 无依赖模块可有限并行且完整 Runner 验收一致；有依赖或路径/DTO/migration 冲突的任务自动串行或阻塞；注入并发提交、取消、crash、预算耗尽负例后不覆盖文件、不双扣费用、不破坏模块记忆/接口；关闭开关则完全按 F07 已通过串行模式执行。

---

# 第三阶段｜X：需要单独授权的可选运行时扩展

## X01｜E06 的 YAML OpenAPI 兼容分支（可选）

- **Goal：** 在已通过的 JSON OpenAPI 适配路径上增加经认证的 YAML 格式解析，保证两种输入产生等价、受控的 Client/Port 契约。
- **Context：** 依赖 C00、C03 和已完成的 E06 JSON 测试；现有 E06 记录只支持 JSON，而不是既有功能失效。
- **Constraints：** 新解析器依赖要先审批并锁版本；限制尺寸/深度/引用，禁止危险反序列化、外网任意 `$ref`、未知 SDK 自动安装；解析后统一进入现有结构校验与权限/域名白名单；未申请扩展时可以保持 JSON-only，不影响 F 阶段。
- **WhenDone：** 等价 JSON/YAML 样例得到兼容 Client/Port 结果；恶意引用、超大 YAML、别名爆炸、未知字段等被拒；原 JSON 路径与旧锁无回归。

## X02｜E05 的真实 Live 第三方沙箱验证（人工授权、条件分支）

- **Goal：** 在具备**明确测试供应商与沙箱权限**时接入真实 Live 验证，不将 E02 Mock 通过误报成 Live 已验证。
- **Context：** 依赖 C03/C04 的配置与 Adapter、独立 Runner；第二阶段记录 E05 为运行时 `NOT_RUN`，现 Runner 默认为 `container_network=none`，待确认供应商协议、网络放行、凭据注入、副作用审批与费用。
- **Constraints：** 在开发者提供供应商沙箱/受控秘密注入方案、网络白名单、人工副作用批准和费用上限前**停止实施**；不自行放开任意网络，不向模型/日志/ZIP 暴露凭据，不在原离线 Runner 模式混入 Live。退款、支付、短信等真实外部副作用需要额外显式批准，失败保留 Mock-only 的真实状态。
- **WhenDone：** 经授权的目标供应商沙箱真实契约测试通过且脱敏审计完整才标 `LIVE_VERIFIED`；无凭据/鉴权失败/网络阻断/供应商失败正确分类；未提供条件时保持 `NOT_RUN` 并列清缺少事项，不能只用 Mock 更新状态。

## X03｜E08 的真实隔离在线预览（人工授权、条件分支）

- **Goal：** 将 E08 已有预览鉴权/生命周期测试连接到真实可隔离启动的预览运行环境，预览后 15 分钟自动回收，ZIP/正式运行不受影响。
- **Context：** 依赖完整 Runner/P0 验收基础；第二阶段明确 E08 只完成测试语义、**实际隔离实例未接**，需要预览基础设施、TLS/访问控制、执行主体和演示数据确认。
- **Constraints：** 在运行基础设施/网络暴露方案、演示数据与回收执行主体确定之前**停止实施**；未通过代码必需验收的项目不得预览；预览不能访问正式密钥、宿主 Docker API 或真实用户数据，非授权者拒绝。不得将预览成功作为替代代码交付的条件。
- **WhenDone：** 经授权测试用户可短时访问通过验收的生成项目；未授权、过期、重复停止、崩溃后的清理都有实际运行证据；15 分钟到期回收资源而保留 ZIP；缺基础设施时明确 `NOT_RUN`，不得按模拟测试视为运行成功。

---

## 4. 里程碑与开发执行门禁

| 里程碑 | 必备步骤 | 判定依据 | 不等于 |
|---|---|---|---|
| C0｜基础核实 | C00 | 仓库、MVP 回归、旧锁、差异表证据 | 不代表 S1/S2 已修复 |
| C1｜模型生产闭环 | C01—C02 | 实际受控模型工具调用（或明确 NOT_RUN）、真实 Gap Patch/Runner 证据 | 不代表 E05 Live/E08 预览 |
| C2｜第三方和增量闭环 | C03—C05；C06 可单列资产认证 | Mock Pack 从规格到 ZIP、E09 新 run/旧 ZIP 不变、资产验证差异明确 | 不代表未知供应商 Live 已就绪 |
| F1｜模块协议 | F01—F03 | 模块唯一所有权、记忆隔离、公共接口契约可查询 | 不代表 Module Agent 已被真实调用 |
| F2｜模块执行 | F04—F06 | 双模块依赖任务、受限 Patch、跨模块协调、真实集成验收及恢复 | 不代表并行能力 |
| F3｜模块上线评估 | F07 | 对照评估有真实证据、feature flag 可安全回退 | 不自动改写 R27 原 P0 门槛 |
| 延后能力 | F08 / X01 / X02 / X03 | 逐项满足自身授权与 WhenDone | **不影响**已经通过的串行模块 Agent 验收 |

## 5. 每一步统一验收记录模板（必须填写实际证据）

```markdown
### 实施记录：[Cxx / Fxx / Xxx]
- 基准 Commit / 结束 Commit：
- 与来源文档编号对应：
- 当前状态：PASS / PARTIAL / FAIL / NOT_RUN / BLOCKED
- Goal 是否达到（对应证据）：
- 依赖步骤及前置门禁核对：
- 新增/修改文件（真实路径）：
- Schema/契约/索引/锁版本兼容性：
- 模块责任与可写路径 / 越权拒绝：
- 测试命令与逐项 PASS/FAIL/NOT_RUN：
- MVP Task 冷启动/旧 asset-lock 重放结果：
- Runner 实际执行、Mock 与 Live 明确区分：
- 路由实际 provider/model/region（无秘密值）：
- run_id / task_id / call_id 账本与 UNKNOWN 项：
- 模块记忆 revision / 接口 version / 验证证据（适用时）：
- 方法候选与选择/拒绝原因、Profile 与 checksum：
- 新旧 ZIP、迁移、回滚、取消/恢复证据：
- WhenDone 逐条结论：
- 遗留风险、人工待确认项、下一步依赖：
```

## 6. 需要开发者明确批准的事项

这些事项**不是 AI 编码工具可以自行替用户决定的默认值**：

1. 百炼模型的实际地域/接口与 `qwen3-coder-flash` 工具调用可用性、真实调用预算、价格版本和相关 API 凭据；若不可验证则记录阻塞。
2. 第三方 Live 沙箱供应商、域名/网络放行、Secret Store 或等效安全注入方案、可能产生实际副作用的人工授权（X02）。
3. 在线预览的部署机器、网络/TLS/认证、隔离执行器、演示数据和回收服务（X03）。
4. 多模块并行对 SQLite/worker/Runner 的实际基础设施扩容及预算策略（F08）。
5. 对已有项目的业务范围/权限或公共契约发生破坏性改变时，由用户确认新的冻结 spec；不得让技术 Agent 代替用户同意。

## 7. 执行后的架构结果与不做的内容

实施到 F07 后，预期拥有：

```text
冻结 spec/hash + 版本化 Profile/资产锁
   └─ Python/LangGraph Coordinator（任务 DAG、共享预算/状态）
        ├─ Asset Finder → Registry/Resolver（跨项目、已审核资产）
        ├─ Module Registry（模块责任、依赖、目录所有权）
        ├─ Module Memory Store（私有、可恢复、可失效的可信记忆）
        ├─ Module Interface Registry（项目内公共能力/签名/版本/证据）
        └─ 按需 Module Executor（单 worker 串行）
             ├─ Module A：只读冻结规格 + A 私有记忆 + 公共接口 + 授权源码
             └─ Module B：只读冻结规格 + B 私有记忆 + 公共接口 + 授权源码
                   ↓
                受控 Backend → FileChangeSet → Runner 完整验证
                   ↓
                契约验证/接口发布/记忆可信修订/ZIP + 报告
```

**明确排除：** 不重新实现已完成 MVP；不默认引入向量数据库、新模型、路由决策模型、持续驻留 Supervisor、无限 Agent 自主委派、生产公网执行、自动收费、多租户、任意仓库改造或无审核的自动资产入库。不以“模块 Agent 数量”作为成功指标；以职责隔离、可验证接口复用、真实业务验收和成本变化作为衡量。

---

## 8. 来源文档对照（供编码工具核查，不代表当前仓库必然一致）

- `AgentForge_01_MVP_分步开发(1).md`：M01—M28 开发计划与 MVP 边界。
- `AgentForge_02_迭代增强_分步开发Prompt_方法索引版(1).md`：R06A—R06D 方法仓库/索引；R13 DAG；R14—R19 受限 DeepAgent；R20—R24 预算与恢复；E01—E10 增强；**2026-10-13 S1—S4 与 E 实施偏差记录是本计划“未完成事项”的权威文档依据**。
- `Demo技术文档_修订版_v0.3(2).docx`：§1 首版单 worker/默认关闭自主子 Agent；§2 Profile/Asset Registry；§4 TaskPlan/资产装配；§6 文件后端、上下文、Integration/Port 和外部配置；§7 Runner；§8 预算；§9 后续扩展。
- `Demo需求分析文档(2).docx`：Demo 规模、用户确认/冻结、不可跳过的前后端/数据库/HTTP 验收和 P0/P1 边界。

> **最终准则：** 这份文档是“如何在当前项目上继续做”的增量开发指令，不能代替代码检查或真实运行。任何一步前发现实际仓库与上述资料不一致，应暂停、记录差异、依据版本事实提出最小修订，而不是按想象创建重复组件或宣布已通过。
