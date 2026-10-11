# AgentForge｜第二阶段：MVP 之后的迭代增强 Prompt

> 类型：**按先后依赖执行的增量开发任务单**。  
> 前置交付：`AgentForge_01_MVP_分步开发Prompt.md` 的 M01—M28 全部通过，至少保留一套可冷启动的 Task 项目产物。  
> 需求基线：《Demo需求分析文档》v0.1；技术设计：《Demo技术文档_修订版》v0.3。  
> **区分两个发布边界：** R01—R27 面向“补齐完整 Demo P0”；E01—E10 属于 Demo 之后的技术扩展/P1，不得混入 P0 的验收结果。  
> **模型路由决策补充（2026-10-11）：** 按开发者提议，面向用户的自然语言理解、澄清提问、回答解释及用户可读 AI 反馈固定使用 **DeepSeek V4.1 Flash (`deepseek-flash`)**；技术规划、受限编码及复杂修复分别由 **Qwen3.7 Plus、Qwen3-Coder-Flash、Qwen3.8 Max** 承担。具体厂商/型号来自本次选型，而非需求文档 v0.1 或技术文档 v0.3 的硬性指定。

## 使用方法与统一增量开发总控 Prompt

每次将以下总控 Prompt 与**当前一个** Rxx 或 Exx 步骤一起交给 AI 编码工具；上一任务有测试失败时，先处理失败，不抢跑下一任务。

```text
你负责在已通过 MVP 验收的 AgentForge 仓库上做最小增量。先读取真实仓库、MVP_ACCEPTANCE.md、已冻结规格 schema、Profile、资产 manifest、前一步的测试及提交记录，再执行当前指定的一步。

执行要求：
- 一次仅完成一个编号，不自行扩展需求，不重写 MVP，尽量只修改当前功能对应的模块。
- 给出本次增量的输入/输出契约和受影响文件；依赖代码不存在时先报告，不编造已实现上下文。
- 保持旧的 Task MVP 冷启动回归通过；新增测试覆盖成功、失败、边界和权限场景。
- 模型可以建议与补全，但程序负责版本、权限、资产选择、路径/预算校验、确定性验收及最终提交。
- 保留已经冻结的固定验收断言，禁止修改测试结果以制造 PASS。
- 开发完成时报告：变更文件、运行命令、实际 PASS/FAIL/NOT_RUN、回归结果、费用/安全影响、WhenDone 判定以及下一步依赖。
- 遇到 API Key、Provider 价格、Runner 权限、真实供应商协议、运行环境不确定时暂停并请求开发者提供确认，不从网络文档中自行猜测安全配置。
- 必须使用本文模型路由：用户交互及语言化反馈 → DeepSeek `deepseek-flash`；技术规划/契约提议 → Qwen `qwen3.7-plus`；DeepAgent 局部编码/第一轮修复 → `qwen3-coder-flash`；具备明确升级条件的疑难修复 → `qwen3.8-max`。不添加额外模型，不调用 LLM 来选择路由。
- 模型之间只传递经过用户确认的冻结 spec/contract/task 上下文；技术模型不得根据原始聊天记录改变业务需求。技术模型产出的机器报告不能直接伪装成用户确认后的解释。
```

### 阶段约束速查

| 主题 | 必须遵循的边界 |
|---|---|
| 需求范围 | 完整 Demo 最多 1—3 个**业务实体**、2 类**业务角色**、8 个**业务页面**；认证实体/页面另计。超范围必须协商缩减，拒绝缩减则停止。 |
| 访谈 | 每轮最多 3 个问题、最多 4 轮；题型单选/多选/文本；待确认核心项清零才能冻结。 |
| 生成栈 | 生成端 Vue 3 JS/TS + Spring Boot MyBatis/MyBatis-Plus + MySQL/Maven，四种组合全部经过构建验证才启用。 |
| 资产优先 | COPY → RENDER → STRUCTURED_EDIT → AGENT_PATCH → 有严格理由的 AGENT_GENERATE；DeepAgent 仅解决真实缺口和局部修复。 |
| 安全 | Runner 独立隔离；Agent 无 shell、无任意网络/仓库、无平台密钥、无受保护文件写权限；外部文档是不可信数据。 |
| 限额 | 每任务最多 2 次业务修复、全 run 最多 6 次；初始建议调用上限 30、工具步数 40、单次模型等待 120 秒、总运行 20 分钟；参数可在配置与测试后调整。 |
| 验收 | 必需项 `PASS` 才可判定 SUCCEEDED；`FAIL` / `NOT_RUN` 不可隐匿；外部 Live 就绪与代码交付结果分开。 |

### 固定模型 API 路由与跨模型交接协议

| 功能场景 | 主调用角色 / API 真实 ID | 推荐调用策略 | 与代码的边界 |
|---|---|---|---|
| 用户需求理解、缺失信息分析、澄清问题提出、回答解释、用户可读需求摘要与反馈 | **DeepSeek 官方 API：`deepseek-flash`（V4.1 Flash）** | 日常解释和简单提问非思考；矛盾分析 `low`；复杂规格汇总 `high`（均需实测） | 所有增删业务规则需以用户确认作为最终依据；不能绕过 `pending` 校验 |
| 技术契约、架构和装配任务建议 | **阿里云百炼：`qwen3.7-plus`** | 每份已冻结 spec 按需调用；无复杂性时继续优先确定性解析 | 只读已确认 spec/资产 manifest/兼容 Profile；Python Validator/Resolver 决定采纳与否 |
| 缺口局部编码、普通编译和业务代码修复 | **百炼：`qwen3-coder-flash`** | 存在真实 Gap/可修复失败才调用 | DeepAgent 只读授权代码/契约、只写 staging allowed_paths；所选地域的工具调用能力须通过探针 |
| 复杂错误第二次修复升级（或在预算内分析疑难点） | **百炼：`qwen3.8-max`** | 仅明确标记复杂性、首修失败且预算/次数允许 | 不扩大权限、不刷新 specs、不修改测试；升级仍计入原有修复次数 |
| 确定性校验、Profile/资产冲突、HTTP 验收、运行状态、预算与 ZIP | **不用模型** | 程序执行 | 不能由模型自评代替 Runner 的 PASS |
| 面向用户的 AI 文本解释（如超范围缩减、复杂技术说明、可读错误解释、外部服务配置的通俗说明） | **DeepSeek：`deepseek-flash`** | 只有固定模板无法清楚表达时才额外调用；不必每次调用 | 只用经后端脱敏的结构化事实生成说明；不能改事实、权限、费用、检查状态 |

**跨模型数据流（固定）：**

```text
用户原始输入 / 回答
    ↓ DeepSeek V4.1 Flash（需求提取 / 问答 / 用户反馈）
候选 spec + pending（经 Pydantic 校验）
    ↓ 用户显式确认 + 服务器冻结版本/hash
confirmed spec.json + contract（唯一权威业务输入）
    ↓ Qwen3.7 Plus（技术规划提议）→ Python Resolver/Validator（实际装配决策）
资产 COPY/RENDER → gap_check
    ├─ 无缺口：不调用 Coding Model
    └─ 有缺口：Qwen3-Coder-Flash / DeepAgent（局部 Patch）
                              └─ 必要时 Qwen3.8 Max（受限升级）
    ↓ Runner 独立验收（不调用 AI）
事实报告 → 结构化状态/模板显示；需要通俗 AI 解释时 → DeepSeek
```

**接口与实现要求：**

1. 增强 MVP M21 的统一路由表，新增 `technical_planning`、`coding`、`complex_repair`（默认禁用，达到对应 R 步骤且完成探针后逐一启用）；`user_interaction` 始终指向 DeepSeek 官方的 `deepseek-flash`。所有任务按**任务类型/失败等级**由 Python 静态路由，不新增“路由决策模型”。
2. `ModelCallResult` 记录 Provider、API 模型 ID、任务角色、思考模式、请求标识、usage、价格版本与错误分类；访谈发生在 run 前须用 `project_id + draft_id` 记录费用，生成期以 `run_id + task_id` 记录；每次内部 Agent 模型请求独立计账。
3. 在 DeepSeek SDK 适配层实测 `thinking.type=disabled/enabled` 与 `reasoning_effort=low/high`。非思考模式用于简单问答，思考模式由缺失/冲突等**确定性规则**触发；不得把隐藏思考文本记录在提示词、日志或结果包。原始中文访谈不转交 Qwen，Qwen 不重新判定用户已确认的业务意图。
4. DeepSeek 的用户可读说明只能重述可信结构化状态（例如真实 `FAIL` / `NOT_RUN` / 成本），不能将失败写成成功；如 DeepSeek 不可用，使用确定性模板说明并暴露“AI 解释不可用”，不静默改用 Qwen。
5. **模型可用性按地域验证。** `qwen3-coder-flash` 的 Function Calling 能力随部署地域/服务方式有差异；必须在准备运行 R16 时用真实工具调用测试，不可仅依据型号名称。Provider ID、版本、区域、支持能力、定价版本及验证日期都要归档；不可硬编码当前价格或接入真实密钥。

**建议配置示意（角色在完成指定步骤前保持 disabled）：**

```yaml
model_routes:
  user_interaction:
    provider: deepseek_official
    model: deepseek-flash
    api_key_env: DEEPSEEK_API_KEY
    enabled: true
    default_thinking: disabled
    complex_thinking: low
    final_spec_thinking: high
  technical_planning:
    provider: dashscope
    model: qwen3.7-plus
    api_key_env: DASHSCOPE_API_KEY
    enabled_after: R13
  coding:
    provider: dashscope
    model: qwen3-coder-flash
    api_key_env: DASHSCOPE_API_KEY
    enabled_after: R16
  complex_repair:
    provider: dashscope
    model: qwen3.8-max
    api_key_env: DASHSCOPE_API_KEY
    enabled_after: R18
```

以上只是说明性 YAML，不代表现有项目已经实现 `enabled_after` 字段；实现时可用有版本的 Profile/路由配置与显式 feature flag。**每个里程碑的 WhenDone 须增加“无意外跨厂商调用”的测试断言。**

**厂商功能验证依据（2026-10-11 本次选型补充，非源设计文档硬性结论）：** [DeepSeek 模型 ID 与能力](https://api-docs.deepseek.com/quick_start/pricing/)、[DeepSeek 思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)、[百炼 Qwen 文本模型列表](https://help.aliyun.com/zh/model-studio/text-generation-model)、[Qwen3-Coder-Flash 地域能力](https://help.aliyun.com/zh/model-studio/qwen3-coder-flash)。

### 推荐版本与里程碑

- **里程碑 D1｜需求访谈闭环（R01—R06）**：满足动态访谈与规格冻结的完整 P0 需求。
- **里程碑 D2｜资产覆盖闭环（R07—R13）**：四 Profile、多实体、唯一约束和校园活动主线。
- **里程碑 D3｜受限 Agent 与修复（R14—R19）**：只有明确业务缺口才调用 DeepAgent，修复后重跑固定检查。
- **里程碑 D4｜运维可靠性与完整验收（R20—R27）**：成本、取消、恢复、历史、9 次评估，形成完整 Demo P0 证据。
- **里程碑 E｜后续能力（E01—E10）**：一个个增加第三方集成、Live/Mock、多样化模板、在线预览和已有项目迭代。

---

# 第一部分｜R01—R27：补齐完整 Demo P0

## D1｜需求访谈与可确认规格

### R01｜扩充业务范围校验器

- **Goal：** 把 MVP 固定 `Task` 的限制升级为规则可配置的 1—3 业务实体、最多 2 角色、最多 8 业务页面，同时建立明确的超范围反馈。
- **Context：** 参考 MVP M06 的 `SoftwareSpecification` 和 M07 确认 API；需求文档第 2、4 节 FR01；技术文档第 9 节细化相同限制。
- **Constraints：** 不直接开始多实体源码生成；不自动删除超范围功能；要求用户确认缩减后的规格才可以进入下一阶段；认证实体/页面从业务限额中排除。
- **WhenDone：** 三个边界的内/外测试通过；超限可返回 `UNSUPPORTED_SCOPE` 和可解释的缩减建议；未确认的缩减绝不能直接提交 run。

### R02｜建立访谈题目与答案 Schema

- **Goal：** 定义 `question_id/type/prompt/options/allow_custom/required/affects/version` 及回答协议，支持单选、多选、文本、自定义和“不确定”。
- **Context：** 依赖 R01 及 MVP M06/M07；需求文档 FR02、技术文档第 5 节的问答契约；下一步 R03 的模型输出和 R05 的 UI 都依赖它。
- **Constraints：** 答案必须绑定题目版本；`affects` 明确本问题改变哪项业务决定；不要求普通用户理解 ORM 或框架；本步不调用模型。
- **WhenDone：** 题型与非法输入均有 Pydantic 校验；旧版题目答案无法写入新版本；schema 测试通过。

### R03｜增加基于缺失字段的动态问题生成

- **Goal：** 使用 **DeepSeek V4.1 Flash（`user_interaction` 路由）** 根据已确认信息与缺失/冲突项一次生成最多 3 个具体的中文澄清问题，同时输出未收敛字段和影响业务决定的 `affects`。
- **Context：** 依赖 R02、M21/M22 的 `deepseek-flash` 网关及生成前记账；需求文档第 5 节角色、字段、约束、权限、规模等缺口清单。
- **Constraints：** 严禁改用 `qwen3.7-plus` 来生成用户问题；简单问题默认非思考、已识别冲突可选择经实测的 `low`；不重复提问，“不确定”不得当作已知；校验结构与次数/费用，不准模型自行宣称完整。
- **WhenDone：** 真实 DeepSeek 调用对校园活动需求输出相关问题且最多 3 题，含 `question_id/affects`；无冲突已答字段不重复提问；测试断言请求路由只命中 `deepseek-flash`，并记录 usage。

### R04｜接入 LangGraph 访谈中断/恢复

- **Goal：** 创建 interview graph：`understand → detect_missing → ask/interrupt → resume/validate → ready_or_wait`，把用户回答关联至同一 thread。
- **Context：** 依赖 R02/R03、M03 的 SQLite、M22 的结构化理解；技术文档第 3 节对 `thread_id`、持久化 checkpointer、`Command(resume=...)` 与节点重放有明确要求。
- **Constraints：** 最多 4 轮，超额后进入 `NEEDS_INPUT`；恢复后的答复解释、缺失补问继续调用 DeepSeek `user_interaction`，不得换 Qwen 重新理解用户意图；interrupt 恢复前后不得重复扣费或重复提交答案；模型调用和数据库写入使用独立幂等键。
- **WhenDone：** 连续两轮经 DeepSeek 更新同一草稿；进程重启后依旧能恢复等待中的访谈；重复 resume 不产生多份回答/模型费用；路由日志无 Qwen 用户交互请求，轮次上限测试通过。

### R05｜平台 UI 动态问答卡片

- **Goal：** 实现可根据 R02 协议展示题目、选项、自定义回答、保存草稿和继续访谈的页面。
- **Context：** 依赖 MVP M25/M26 与 R02—R04；需求文档 FR02 要求业务语言而非技术术语；技术文档第 5 节建议前端安全渲染。
- **Constraints：** 一次最多展示 3 题；页面展示 R03 DeepSeek 返回并通过 schema 校验的文案，不将 Qwen 的架构/Agent 原始输出直接呈现给用户；前端不猜测题型；网络中断不丢已确认答案；待输入和生成失败分别展示。
- **WhenDone：** 用户可完成至少两轮问答、离开并回来接续；过期题目回答有清晰提示；构建与交互测试通过。

### R06｜冻结验收场景与规格修改版本

- **Goal：** 使用 DeepSeek V4.1 Flash 整理面向用户的最终需求摘要（复杂汇总可用经验证的 `high`），经用户确认后由程序冻结功能、排除项、业务规则及**独立验收场景**，变更后创建新 spec 版本。
- **Context：** 依赖 R01—R05 及 MVP M07；需求文档 FR03、第 8 节要求验收断言和规格一同冻结，编码 Agent 无权修改。
- **Constraints：** `pending` 核心项未清零不得确认；DeepSeek 只是起草和解释，不能替用户同意；技术规划模型不得在冻结后重解释原对话或改业务规则；生成中的 spec 不允许原地编辑，冻结哈希进入 run。
- **WhenDone：** 可查看规格版本差异；旧 run 指向旧 spec 的 hash；人为篡改冻结的验收断言会在检查中被拒绝。

---

## D2｜四种模板组合与复杂业务资产

### R07｜补充 Vue 3 + JavaScript Profile 分支

- **Goal：** 从已通过的 TS 基线派生 JS 模板，只替换真正不同的前端结构与构建配置。
- **Context：** 依赖 MVP M08—M12；需求文档 FR04 要求 JS/TS 可选择，技术文档第 2 节通过不可变 Platform Profile 管理兼容性。
- **Constraints：** 已发布 TS Profile 不原地覆盖；不得将 TS 类型文件无脑复制到 JS 模板；保持相同接口与业务验收。
- **WhenDone：** JS 模板可完成 `npm ci`、`npm run build` 及同一 Task HTTP 验收；两个 profile 版本均有锁定记录。

### R08｜补充 Spring Boot + MyBatis 分支

- **Goal：** 制作 MyBatis（非 Plus）的后端模板/映射能力包，复用同一实体及 REST 业务契约。
- **Context：** 依赖 MVP M08—M11 和 R07 的版本思路；需求文档 FR04 要求两种 ORM 可选择。
- **Constraints：** 不改写已认证的 MyBatis-Plus 资产；普通 CRUD 可以多一些显式 SQL，但权限/分页验收结果必须一致；保留适配层边界。
- **WhenDone：** MyBatis Task 工程可以构建启动并通过同一业务测试；不使用 Plus 专属 API；回归通过。

### R09｜四组合 Profile 认证与高级技术选项

- **Goal：** 完成 JS/TS × MyBatis/MyBatis-Plus 四种 Profile 的独立锁、兼容矩阵、构建及前端高级选择；**仅在需要 AI 进行面向用户的技术通俗解释时调用 DeepSeek `deepseek-flash`**。
- **Context：** 依赖 R07/R08、M08、M13/M14；需求文档第 6、8 节要求四组合都通过基础模板构建，界面说明特点、局限与推荐理由。
- **Constraints：** 选型与 Profile 兼容判断由确定性规则执行，DeepSeek 只解释经验证的功能差异，不凭空承诺性能或暗改配置；启用前四组合需经过基础构建和 Task 回归；用户不能指定任意依赖版本。
- **WhenDone：** 四种组合均有可追溯 `profile_id/version`、构建报告及验收记录；选择无效组合被拒；默认继续推荐 TS + MyBatis-Plus。

### R10｜支持第 2—3 个业务实体与关系映射

- **Goal：** 将装配器从单 Task 扩展为有明确一对多/多对一或关联实体关系的 2—3 业务实体装配。
- **Context：** 依赖 M06/M11/M12/M15、R01/R06；技术文档第 6 节强调“数据库、DTO、REST 契约先冻结，再装配后端/前端”。
- **Constraints：** 先仅处理确定性关系模式；外键、字段、路由命名冲突需在写文件前拒绝；不自动允许模型新增第 4 个实体。
- **WhenDone：** 图书借阅或活动报名的一套 2—3 实体规格能生成外键正确的 migration、服务和页面；重复渲染无漂移；测试通过。

### R11｜唯一约束与状态守卫能力包

- **Goal：** 新增数据库唯一约束、业务状态检查及错误码映射能力包，覆盖“不能重复报名”“活动结束不可报名”。
- **Context：** 依赖 R10；需求文档第 5、8 节给出了校园活动报名的关键规则；技术文档第 2、9 节要求 `CRUD + RBAC + 数据归属 + 状态守卫` 组合回归。
- **Constraints：** 不只做前端防重；并发重复请求靠数据库约束兜底；状态条件必须服务端检查；错误不能返回成功掩盖重复失败。契约中的 `unique_constraints` 必须由确定性的 migration 渲染路径消费；若新增能力包负责渲染，不得与既有 CRUD 包重复创建同一约束或静默忽略契约字段。
- **WhenDone：** 非空 `unique_constraints` 的生成 migration 含相应数据库唯一约束，并经真实数据库迁移和重复插入测试验证；并发报名后同一成员-活动最多有一条有效记录；结束后报名被拒；数据库约束/接口测试均有 PASS 证据。

### R12｜校园活动主线：角色、页面与固定业务验收

- **Goal：** 用已增加的实体/能力包装配校园活动管理，并覆盖管理员发布、成员报名、列表分页、报名查看权限。
- **Context：** 依赖 R09—R11；需求文档第 3、8 节确定该项目为必需演示主线；M18 的 Runner 需要扩展固定业务测试模板。
- **Constraints：** 管理员发布，成员不得发布或访问他人记录；验收断言根据确认规格及平台固定模板冻结，不交给 DeepAgent 编辑。
- **WhenDone：** 至少一套校园活动全栈产物真实通过规定 HTTP 场景：不可重复报名、不可越权、活动结束拒绝报名、刷新持久化及分页总数。

### R13｜接口契约先行的多任务 DAG 规划

- **Goal：** 在需要 AI 规划时由 **Qwen3.7 Plus（`qwen3.7-plus`）** 基于**用户确认的冻结 spec** 提出接口/DTO/SQL/权限契约和 `TaskPlan`；Python 冻结、校验并决定最终任务 DAG。
- **Context：** 依赖 R10—R12、M07 的已确认 spec/hash 与 M14/M15；技术文档第 4 节定义 `task_id/execution_mode/asset_refs/depends_on/allowed_paths/inputs/acceptance_ids/token_limit`；由 R06 的 DeepSeek 用户摘要交接，但**不得读取未确认原始聊天**。
- **Constraints：** 静态任务继续使用确定性转换，只有确有规划复杂性才调用 `technical_planning`（Qwen3.7 Plus）；模型只能提议能力和任务，不得改 confirmed spec、权限、验收或依赖版本；Python 做 DAG 无环、冲突和覆盖校验，按依赖串行提交。
- **WhenDone：** 基于同一个冻结 spec/hash 的计划具备可追溯输出、输入摘要 hash 和通过的 schema 验证；无环、越界 allowed_paths、同路径冲突被拒；固定场景重复执行稳定，模型调用日志仅出现允许的 `technical_planning` 路由。

---

## D3｜有明确边界的 DeepAgent 补缺与有限修复

### R14｜实现能力覆盖检查与 Gap Task 生成

- **Goal：** 将装配计划与已冻结验收场景逐项对比，输出 `covered_acceptance_ids/uncovered_rules/gap_reason`；仅对真实缺口创建 `AGENT_PATCH` 任务。
- **Context：** 依赖 M13/M14、R13；技术文档第 4 节要求资产覆盖依据是验收规则而非代码行数；第 3 节明确无缺口直接跳过 Agent。
- **Constraints：** 覆盖判断由 Python Resolver/固定验收 ID 决定，Qwen 规划提议不能直接算“已覆盖”；资产无法覆盖不能标记已完成或放宽需求；缺口标明授权代码位置与预期验收；没有缺口时不调用 `coding`。
- **WhenDone：** 已覆盖的标准 Task run 模型编码次数为 0；用固定特殊规则构造的缺口能形成可解释、可执行且路径有限的 TaskPlan。

### R15｜为 Agent 构建受限暂存文件后端

- **Goal：** 让编码任务只能读取授权源码/只读契约，写入当前 run/task 专属暂存目录；提交前生成 `FileChangeSet`。
- **Context：** 依赖 M15/M16、R13/R14；技术文档第 4、6 节定义 `allowed_paths`、基准 hash、临时区、受保护文件和原子提交。
- **Constraints：** 文件 backend 在每次读写时执行权限/路径检查，最终提交再校验；拒绝 `..`、绝对路径、符号链接逃逸、过大文件、认证模板、`asset-lock.json` 和固定验收断言修改。
- **WhenDone：** 正常业务扩展文件可暂存、校验并提交；越界读写和受保护修改在工具层即时拒绝；失败不污染正式源码，测试通过。

### R16｜核对并接入 DeepAgent 的实际工具集

- **Goal：** 使用经兼容性验证的 `deepagents` 创建单任务 patch engine，并接入 **Qwen3-Coder-Flash（`qwen3-coder-flash`）**，验证所选地域的工具调用、backend、账本及输出协议。
- **Context：** 依赖 M21 的跨 Provider 网关、R15 文件 backend；技术文档第 1、6、9 节要求 `create_deep_agent` 是 fallback，不是独立模型；选型补充的 `coding` 角色指定百炼 `qwen3-coder-flash`。
- **Constraints：** **先对实际使用的百炼地域/接口执行 Function Calling 探针**，若该地域不支持则本步 `NOT_RUN/阻塞`，不得假装模型有工具能力或未经批准换型号；审计真实工具表、禁止 shell/任意网络/未受控委派；每个内部模型请求继承 run/task/call_id，独立预留结算，不只统计外层 invoke。
- **WhenDone：** `qwen3-coder-flash` 在目标地域完成真实受限工具调用与暂存写入；危险工具/越界均被拒；内部所有调用入账。若工具能力不支持，记录地域/接口证据和阻塞，不声明 R16 PASS。

### R17｜实现“装配优先，缺口局部 Patch”

- **Goal：** 将 R14 产生的真实缺口交给由 **Qwen3-Coder-Flash** 驱动的 R16 patch engine，并在变更校验通过后按 R15 原子提交。
- **Context：** 依赖 R13—R16；技术文档第 3 节的 `gap_check → patch → commit → verify`，第 6 节要求 TaskContext 包含冻结规格片段、契约、依赖和授权文件。
- **Constraints：** Agent 只消费 confirmed spec、冻结契约和 task.allowed_paths，不直接获取 DeepSeek 对话历史；不可扩大需求、改模板原件、自动升级依赖或重写模块；不提供固定测试写权限；未通过 schema/文件校验不提交。
- **WhenDone：** 一条受控的“资产未覆盖但在项目扩展点内”的业务规则由 Agent 小范围补全，变更集可审查且新增测试 PASS；标准 Task 无缺口时仍不启动 Agent。

### R18｜失败分类与有限自动修复

- **Goal：** 建立 `classify_failure → coding(Qwen3-Coder-Flash) → 可选 complex_repair(Qwen3.8 Max) → verify` 的修复支路，并记录 task/run 的修复次数。
- **Context：** 依赖 M17/M18、R15—R17；需求文档 FR06 限每任务 2 次、全 run 6 次；技术文档第 8 节允许第二次修复升级；本版路由默认先 `qwen3-coder-flash`，首修失败或确属复杂问题才考虑 `qwen3.8-max`。
- **Constraints：** 仅可复现的代码/契约错误进入修复；基础设施/资产冲突/外部配置不能盲改；第二次修复是否升级由 Python 明确分类并检查预算，升级仍消耗该任务第 2 次修复机会；只传脱敏相关日志、冻结契约与授权源码；上限即 FAILED。
- **WhenDone：** 注入编译错误时第一轮调用 Qwen3-Coder-Flash 并复验；构造确定性的复杂错误满足升级条件且在额度内才调用 Qwen3.8 Max；超限停止，基础设施不计业务修复次数，全部模型调用有路由/成本日志。

### R19｜完整复验与最多一轮语义审查

- **Goal：** 在打包前执行完整必需验证；可选由 **Qwen3.7 Plus** 对照冻结业务规则做最多一轮技术语义审查，问题须转换为可验证的具体修复任务。
- **Context：** 依赖 R18 和原有 Runner；技术文档第 3、7 节规定 `full_verify → review → package`，模型评价不能覆盖确定性检查结果。
- **Constraints：** 语义审查只接收冻结 spec、代码和固定验收结果，不重新解释 DeepSeek 原对话；不因 Qwen 自评就绕过失败测试；如代码变化必须重新完整验收；审查 Agent 不得改冻结规格与固定测试。若需要 AI 面向用户说明技术审查结果，由 DeepSeek 在脱敏事实之上转述。
- **WhenDone：** 失败用例无法走到 SUCCEEDED；修复后所有必需项确实重新 PASS；报告保留首次失败及最终复验记录。

---

## D4｜预算、可恢复执行与 Demo P0 验收

### R20｜完善 Provider 价格、usage 与多角色共享预算

- **Goal：** 将 MVP 的 DeepSeek 单 Provider 账本完善为 **DeepSeek 用户交互 + Qwen 技术规划/编码/升级修复 + 各内部重试** 共享预算与预留。
- **Context：** 依赖 M21 的生成前 project/draft 花费、R03/R06 用户交互、R13 Qwen 规划及 R16/R18 内层调用；技术文档第 8 节给出预留公式、缓存/输出分项与 `price_version`，10 元只是可修改内测参数。
- **Constraints：** 各 Provider 保存实际 model ID、mode、币种、定价版本与 usage；未知价格/usage 暂停对账，不写零；原币单位记账，不同计费币种的共享预算必须按锁定汇率快照统一计算可用额度并记录汇率时间；不要混算或遗漏 run 前访谈成本；本地限额不替代 Provider 限额。
- **WhenDone：** 覆盖 DeepSeek 问答 + Qwen 规划 + Qwen Agent 内层重试 + Max 升级的真实/模拟账本测试；预算不足零新请求；报表区分访谈期与 run 期金额、模型/usage/定价版本/未知状态；跨币种汇率可追溯。

### R21｜模型调用次数、工具循环与运行时限统一拦截

- **Goal：** 实现可配置的全 run 模型次数、单次输出、模型等待、Agent 工具循环及运行总时限。
- **Context：** 依赖 R20、R16—R18；技术文档第 8 节建议上限分别为 30 次模型调用、12,000 Token 输出、120 秒等待、40 次工具操作及总 20 分钟。
- **Constraints：** DeepSeek 非思考/思考模式及 Qwen 规划/编码/升级调用全部经过同一调用次数/预算守卫；任何 AI 解释请求同样计费、计数。限制由网关与任务控制器执行，未知在途账单不自动重发；运行超时触发 Runner 清理；访谈发生在 run 前须有独立且可追溯的项目级限额。
- **WhenDone：** 每一种限额有独立边界测试；达到上限的模型调用或工具写入被拒；报错分类与成本记录完整。

### R22｜取消 run 与 Runner 资源回收

- **Goal：** 实现幂等 `POST /api/runs/{id}/cancel`，设置 cancel_requested、阻止新任务分发并尽力停止在途工作。
- **Context：** 依赖 M19/M20、R21；需求文档 FR07 与技术文档第 5、7、8 节说明取消不保证撤回 Provider 已处理调用。
- **Constraints：** 取消后不能再发新模型请求/新 Runner 检查；对已花费或在途预留如实保留；不能仅把 UI 改成 CANCELLED 而让后端继续执行。
- **WhenDone：** 连点取消不会重复执行；模拟慢任务可停止后续派发并释放隔离容器；账本保留在途/已确认额度，界面说明可能发生的费用。

### R23｜worker 崩溃恢复、检查点与原子提交对账

- **Goal：** 完整实现租约接管、LangGraph checkpointer 和按任务 hash/幂等键恢复执行。
- **Context：** 依赖 M19/M20、R04、R13—R22；技术文档第 3 节明确节点重放不能保证外部副作用“恰好一次”，必须核对已提交任务、文件 hash 与账本。
- **Constraints：** 恢复时不能重复提交源码或盲目重发状态不明的模型请求；UNKNOWN 调用须人工/账单对账；相同 run 的既有产物不得被覆盖；checkpoint 不得保存密钥。
- **WhenDone：** 在装配完成/模型在途/Runner 验证中三个节点各模拟进程崩溃，重启后有一致、可解释的结果；已提交任务不会重做，未知调用不会重复收费。

### R24｜预算暂停与经确认的恢复

- **Goal：** 实现 `PAUSED_BUDGET`、可追溯预算调整，以及 `POST /api/runs/{id}/resume` 的恢复流程。
- **Context：** 依赖 R20/R23；技术文档第 3、5 节要求预算不足暂停，增加本次预算并**明确恢复**后才能继续。
- **Constraints：** 不允许系统默默换廉价模型或省掉测试；权限校验必须由 API 执行；恢复前核对未知预留和已提交检查点。
- **WhenDone：** 低预算能真实暂停；未授权/未增加预算恢复被拒；授权增加预算后从安全检查点继续而非重复运行已提交任务。

### R25｜运行事件、历史、失败包与下载授权完善

- **Goal：** 补齐历史列表、规格版本/run 状态、递增事件分页、阶段成本与失败产物下载。
- **Context：** 依赖 MVP M27、R22—R24；需求文档 FR07/FR08 要求取消、状态、失败原因与历史；技术文档第 5 节以 `after_sequence` 增量轮询。
- **Constraints：** 下载严格校验 owner 和 artifact_id；失败包显著标“未通过验证”，不虚构完成百分比；普通状态/失败码用确定性模板，只有必要的用户解释才走 DeepSeek `user_interaction` 并计账；绝不把 Qwen 的工具日志原文或隐藏思考输出给前端；日志脱敏并按配置保留周期清理。
- **WhenDone：** 用户能查看自己的历史规格、失败详情与重入状态；跨用户无法查看/下载；成功/失败包中的状态和验证报告一致。

### R26｜核心项目矩阵回归与文档完整性审查

- **Goal：** 对校园活动、个人任务、图书借阅三种模板用例及四 Profile 进行契约、构建、启动、业务权限与交付清单回归。
- **Context：** 依赖 R01—R25；需求文档第 8 节规定所有必需检查不能缺项；技术文档第 9 节要求 Profile、资产冲突、锁重放、修复、安全与配置测试。
- **Constraints：** Mock/Live 必须分层；当前无第三方服务时 `EXTERNAL_SERVICES.md` 明确说明无须配置；新增路由回归核对“用户交互只用 DeepSeek、技术规划 Qwen3.7 Plus、缺口 Qwen3-Coder-Flash、按条件升级 Qwen3.8 Max”；不可删测试求全绿。
- **WhenDone：** 生成 ZIP 均包含 frontend/backend、SQL migration、`.env.example`、Dockerfile、compose、README、spec、接口说明、`asset-lock.json`、外部服务指南、验证与成本报告；无真实秘密；所有启用 Profile 有回归证据。

### R27｜正式 9 次独立生成评估与 Demo P0 发布判定

- **Goal：** 按固定配置独立完成校园活动、个人任务、图书借阅**各 3 次**，汇总质量、费用、延迟、资产覆盖率及失败分布。
- **Context：** 依赖 R26；需求文档第 8 节目标为 9 次中至少 7 次通过全部必需检查，并要求校园活动有完整成功产物；技术文档第 9 节区分首次通过和修复后通过。
- **Constraints：** 不人工修改源码后把该次计为成功；缺失/超时的必需测试计为失败；同时报告“成功项目平均成本”和“所有尝试费用总和/成功次数”，成功数为零写不可计算。
- **WhenDone：** `docs/DEMO_P0_ACCEPTANCE.md` 有 9 份可定位 run、报告与失败样本，逐项 PASS/FAIL/NOT_RUN、耗时、**DeepSeek/Qwen 分角色实际请求量、模式、usage、成本**、修复记录和模型路由断言；满足 7/9 且校园活动成功才标“达到文档建议验收目标”；不达标如实标注。

---

# 第二部分｜E01—E10：完整 Demo 之后的扩展（非 P0）

> **重要版本边界：** 需求文档 v0.1 第 4 节明确排除首版第三方业务接口、支付、抓取、微服务、BYOK、收费；第 9 节将在线预览、已有项目迭代、更多模板等列为后续方向。技术文档 v0.3 第 4、6、7 节描述了未来接入第三方服务时的架构约束，但**不代表首版必须实现真实支付**。以下都是**可选扩展**，每步都要新增版本与相应测试。

## E1｜第三方能力：先做一个无凭据、可 Mock 的范例

### E01｜建立 Integration Pack manifest 与版本发布策略

- **Goal：** 为第三方适配模板增加只读 Registry，定义 pack ID、版本、兼容 Profile、Port、Adapter、配置、Mock、校验和与许可证。
- **Context：** 依赖 R12/R13 和 MVP M13/M14 的资产注册机制；技术文档 v0.3 第 2、6 节关于 Integration Pack 和资产发布审核的描述。
- **Constraints：** 先选择**非支付、无真实外部凭据**的代表性模拟服务；不启用来源未验证的 SDK；Agent 的一次性 patch 不自动升格为公共资产。
- **WhenDone：** 一个 Mock-only integration pack 可以被 Resolver 锁定且通过 Profile 兼容测试；重复、冲突与缺失依赖被拒。

### E02｜Port/Adapter/Mock 契约链路

- **Goal：** 让生成业务 Service 仅依赖内部 Port，Adapter 实现第三方协议，Mock 实现确定性测试行为。
- **Context：** 依赖 E01；技术文档第 6 节约定 `integration/port`、`integration/adapter`、`integration/mock`，并指出生成 Client 不等于完成幂等和业务语义。
- **Constraints：** 外部响应属于不可信数据；不得把供应商 DTO 直接传播到核心业务规则；Mock 测试成功不能宣称 Live 成功。
- **WhenDone：** 同一 Port 可在 Mock 下演示成功、失败、超时；至少一套契约测试对真实 Adapter 的接口约束有明确断言。

### E03｜External Requirement 结构化声明与配置指南生成

- **Goal：** 定义用户需要申请/填写的账号、标识、密钥、回调、白名单等结构化待办，生成 `.env.example` 和 `EXTERNAL_SERVICES.md`。
- **Context：** 依赖 E01/E02；技术文档第 4、6 节列出 `kind/secret/required_in_modes/acquisition_url/target_file/env_name/config_path/validation_rule/docs_verified_at` 等字段。
- **Constraints：** `secret` 和 `required` 独立；AppID、公钥不默认当秘密；程序生成实际配置映射和固定指南，需要 AI 将已验证配置事实解释给用户时才调用 DeepSeek，不用 Qwen 原始输出直接面向用户；同名变量冲突需拒绝/命名空间化；真实凭据绝不入 spec/Prompt/ZIP/日志。
- **WhenDone：** 从 pack manifest 自动生成“申请什么—在哪里申请—填写哪里—如何验证”的指南；环境变量与代码绑定 1:1；秘密扫描通过。

### E04｜区分 Mock 可交付与 Live 就绪状态

- **Goal：** 让运行报告把 `code_delivery_status` 与 `external_readiness` 分开，支持 `NOT_REQUIRED/MOCK_ONLY/AWAITING_USER_CONFIG/LIVE_VERIFIED/LIVE_FAILED`。
- **Context：** 依赖 E02/E03、R25 的历史结果页；技术文档第 3、7 节明确 Mock 通过不证明 Live 凭据/回调/网络可用。
- **Constraints：** 缺少 Live 配置时标 `NOT_RUN`/待配置，不可伪称验证；只有确认规格要求 Live 为交付必需时，Live 缺失才影响“完整就绪”判断；代码检查与第三方故障分别报告。
- **WhenDone：** 页面能分别呈现代码交付、Mock、Live、缺配置；所有组合状态均有测试；无真实密钥仍可在 Mock 生成和验收。

### E05｜有限 Live 沙箱验证与受控秘密注入

- **Goal：** 在明确用户已配置第三方测试凭据后，增加独立 Live 验证工作方式，输出脱敏结果。
- **Context：** 依赖 E04 和既有 Runner；技术文档第 6、7 节要求受控网络、最小凭据注入和提供商测试环境。
- **Constraints：** 平台不收集真实第三方密钥给模型；Live 专用 Runner 仅注入该 Adapter 必需秘密并允许精确域名/端口；可能扣款、发短信等真实副作用需额外人工批准，不与普通测试混用。
- **WhenDone：** 未配置时明确阻止 Live 启动；配置正确的供应商测试环境可执行规定契约用例；鉴权失败和网络不通能区分且不泄漏凭据。

## E2｜进一步扩充适配来源（按风险逐级推进）

### E06｜基于经过校验 OpenAPI 的 Client 生成

- **Goal：** 支持上传/引用已审核的 OpenAPI 规范，以锁定版本的生成器生成 Client/DTO，再适配既有 Port。
- **Context：** 依赖 E01—E03；技术文档第 6 节的“Integration Pack → OpenAPI → 文档 → 受限 Agent”四级策略。
- **Constraints：** 验证 OpenAPI 格式、来源与允许域名；规范正文是不可信数据，不能解释为执行指令；生成 Client 后仍需人工明确签名、鉴权、幂等和回调语义。
- **WhenDone：** 固定 OpenAPI 样例成功生成并通过 Port 契约测试；恶意 URL、超大 schema 与未经批准 SDK 被拒。

### E07｜只有第三方文档时生成待确认 Integration Spec

- **Goal：** 在缺少 OpenAPI 时，先提取带出处/不确定项的集成规格；**向用户提出待确认问题和解释时使用 DeepSeek**，确认后由技术规划/编码角色受限补充 Adapter。
- **Context：** 依赖 E06、R15—R17；技术文档第 6 节要求提取操作、认证、错误、回调、外部待办，且不能编造供应商协议。
- **Constraints：** 用户向问题 DeepSeek 提交确认意见后才冻结集成规格；技术侧可由 Qwen3.7 Plus 提议契约、Qwen3-Coder-Flash 补授权 Adapter，但不能拿原始聊天自行重写需求；文档提示注入无权改变执行权限；未知 SDK 不自动安装；缺失鉴权/回调保留 pending，不启动 Live。
- **WhenDone：** 不完整文档会产生明确问题清单并暂停；已确认样例仅修改允许的 Adapter 文件；固定安全与契约测试通过。

## E3｜体验与后续产品化

### E08｜P1 在线预览与 15 分钟自动回收

- **Goal：** 只为已通过全部代码必需验收的生成项目提供短时在线预览，结束后自动释放隔离实例。
- **Context：** 依赖 R27 的 Demo P0 基线及 Runner；需求文档 FR09、技术文档第 9 节规定演示数据、随机授权地址和默认 15 分钟回收。
- **Constraints：** 非已验证产物不得开启；不向公网任意运行未知代码；停止预览不能删除源码；预览不能混用用户生产数据或第三方正式凭据。
- **WhenDone：** 授权用户可访问短时预览；非授权者拒绝；15 分钟到期清理容器与数据，ZIP 仍可下载。

### E09｜既有生成项目的规格增量迭代

- **Goal：** 为**由 AgentForge 生成**且携带资产锁的项目提供“DeepSeek 访谈确认变更 → 冻结新规格 → Qwen 技术影响分析 → 最小变更”工作流。
- **Context：** 依赖 R13/R17/R23 和 `asset-lock.json`；需求文档第 9 节明确“既有项目迭代”为后续方向，技术文档第 6 节要求模板版本与契约变更可追溯。
- **Constraints：** 起步只支持 AgentForge 自有产物；变更意图和用户可读反馈由 DeepSeek 处理，Qwen 只处理已确认的技术任务；不承诺任意仓库改造；契约变更需重新冻结并回归，旧 run 不得原地修改。
- **WhenDone：** 一个已完成 Task 项目增加经过确认的字段或业务规则后，生成新版本并保留旧 ZIP/报告；迁移与回归测试通过。

### E10｜数据驱动扩充资产与可检索上下文

- **Goal：** 基于 R27 的失败样本与真实缺口频率，挑选一个高价值通用能力沉淀为**新版本**能力包；必要时加入基于目录/契约的上下文检索。
- **Context：** 依赖 R14/R27、E09；技术文档第 2、6、9 节要求 Agent patch 通过回归和维护审核后才能成为资产，并明确首版不使用向量数据库。
- **Constraints：** 不以行数多、模型调用少就推断业务正确；优先文件与契约索引，确有度量收益再评估向量检索；不为“多 Agent 并行”提前引入多 worker 或未审计的子 Agent。
- **WhenDone：** 新能力包有 manifest、版本、兼容矩阵、回归和对照数据，旧资产不变；至少提供一份加入前后覆盖率/成本/通过率的对比。

---

## 何时才需要考虑更远的功能？

收费、BYOK、公众多用户、多租户、永久预览/部署、复杂微服务、任意现有仓库改造、Agent 并行以及更强沙箱隔离都**不是**上述 P0 的默认内容。只有完整 Demo 的可重复评估、密钥管理、法律/支付与公网运行风险分别解决后，才另立需求文档、技术决策和验收标准，**不要让编码 AI 看到这些方向就自行开工**。

## 最后一次发布检查模板（每个里程碑都填写）

```markdown
### 里程碑验收：[D1 / D2 / D3 / D4 / E*]
- 基准 Commit / Profile / Asset Lock：
- 本期新增功能与明确排除项：
- 使用的运行机器与隔离配置：
- 新增测试 PASS / FAIL / NOT_RUN：
- MVP Task 基线冷启动回归：
- 新增业务场景 / 权限回归：
- 冻结断言未被修改的证据：
- 模型路由（DeepSeek 用户交互 / Qwen 规划 / Coder / Max 升级）与非预期调用检查：
- 模型调用、思考模式、Token、成本及 UNKNOWN 调用（含访谈期费用）：
- 运行/磁盘/容器清理结果：
- ZIP 与秘密扫描：
- 遗留风险与阻塞（需人工确认）：
- 是否达到本里程碑 WhenDone：是 / 否（证据链接）
```

## 源文档对应关系

| 本文件阶段 | 需求文档 v0.1 依据 | 技术文档 v0.3 依据 |
|---|---|---|
| D1 / R01—R06 | FR01—FR03、需求访谈、确认冻结 | 第 3—5 节状态、spec 与接口、interrupt |
| D2 / R07—R13 | FR04—FR05、四组合、校园活动验收 | 第 2—4、6、9 节 Profile、资产、任务 DAG |
| D3 / R14—R19 | FR05—FR06、有限修复 | 第 3—4、6—8 节 Agent backend、验证与故障分类 |
| D4 / R20—R27 | FR07—FR08、非功能要求、9 次评估 | 第 3—5、7—9 节费用、恢复、历史、验证报告 |
| E01—E07 | 第 4、9 节：第三方服务明确后置 | 第 4、6、7 节 Integration/External Requirements/Live |
| E08—E10 | FR09(P1) 与第 9 节后续方向 | 第 2、6、9 节资产发布、扩展与回归 |

**执行准则：** 从一个可验证闭环增加一个能力，不在同一轮同时引入新 Profile、新 Agent 工具、新 Runner 权限和新第三方 API。
