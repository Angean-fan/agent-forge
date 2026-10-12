# AgentForge｜第二阶段：MVP 之后的迭代增强 Prompt

> 类型：**按先后依赖执行的增量开发任务单**。  
> **已完成基线（开发者声明，2026-10-11）：** MVP `M01—M28`（含 `M08A/M15A`）已经全部实现；本文件**不安排重新实现或改写 MVP**。开始增量前只读取仓库、`docs/MVP_ACCEPTANCE.md` 和既有测试证据，保留可冷启动的 Task 产物；如果现场证据与“已完成”声明不符，记录差异并阻塞相关增量，不擅自补做 MVP。  
> 需求基线：《Demo需求分析文档》v0.1；技术设计：《Demo技术文档_修订版》v0.3。  
> **区分两个发布边界：** `R01—R27`（新增 `R06A—R06D`）面向“补齐完整 Demo P0”，其中方法资产索引属于 **2026-10-11 新增工程增强**，不是原需求文档 v0.1 已规定的验收项；`E01—E10` 属于 Demo 之后的技术扩展/P1，不得混入 P0 的验收结果。  
> **本次方法仓库决策补充（2026-10-11）：** 继承已实现的 `Template Registry / Capability Pack Registry / Asset Resolver`，增量增加可复用方法/类/片段（`Method Unit`）及本地索引；优先检索、兼容性筛选、参数化渲染和受约束结构化改动，确有缺口才使用 Coding Agent；新代码通过项目验收**不等于**自动成为公共资产，须独立审查和版本发布。
> **模型路由决策补充（2026-10-11）：** 按开发者提议，面向用户的自然语言理解、澄清提问、回答解释及用户可读 AI 反馈固定使用 **DeepSeek V4.1 Flash (`deepseek-flash`)**；技术规划、受限编码及复杂修复分别由 **Qwen3.7 Plus、Qwen3-Coder-Flash、Qwen3.8 Max** 承担。具体厂商/型号来自本次选型，而非需求文档 v0.1 或技术文档 v0.3 的硬性指定。

## 使用方法与统一增量开发总控 Prompt

每次将以下总控 Prompt 与**当前一个** Rxx 或 Exx 步骤一起交给 AI 编码工具；上一任务有测试失败时，先处理失败，不抢跑下一任务。

```text
你负责在已完成 MVP 的 AgentForge 仓库上做最小增量。M01—M28（含 M08A/M15A）视为已有实现，禁止重做。先读取真实仓库、docs/MVP_ACCEPTANCE.md、已冻结规格 schema、Profile、模板/能力包 Registry、Resolver、资产 manifest、前一步的测试及提交记录，再执行当前指定的一步。

执行要求：
- 一次仅完成一个编号，不自行扩展需求，不重写 MVP，尽量只修改当前功能对应的模块；R06A—R06D 只是增量扩展 M13/M14/M15，不允许为了新索引推倒原 Registry/Resolver。
- 给出本次增量的输入/输出契约和受影响文件；依赖代码不存在时先报告，不编造已实现上下文。
- 保持旧的 Task MVP 冷启动回归通过；新增测试覆盖成功、失败、边界和权限场景。只读已发布 Profile、模板、能力包及其 manifest 不可原地修改；旧 spec/asset-lock 须按原版本语义读取并重放，新版本、索引快照、兼容性结论、拒绝原因要可追溯。
- 模型可以建议与补全，但程序负责版本、权限、资产选择、路径/预算校验、确定性验收及最终提交。按“已审核资产精确匹配 → 可参数化复用 → 受约束结构化适配 → 局部 Patch → 严格受限的新写”执行；索引命中不等于语义覆盖，不能为提高命中率绕过契约与验收。
- 保留已经冻结的固定验收断言，禁止修改测试结果以制造 PASS。
- 开发完成时报告：变更文件、运行命令、实际 PASS/FAIL/NOT_RUN、回归结果、费用/安全影响、WhenDone 判定以及下一步依赖。
- 遇到 API Key、Provider 价格、Runner 权限、真实供应商协议、运行环境不确定时暂停并请求开发者提供确认，不从网络文档中自行猜测安全配置。
- 必须使用本文模型路由：用户交互及语言化反馈 → DeepSeek `deepseek-flash`；技术规划/契约提议 → Qwen `qwen3.7-plus`；DeepAgent 局部编码/第一轮修复 → `qwen3-coder-flash`；具备明确升级条件的疑难修复 → `qwen3.8-max`。不添加额外模型，不调用 LLM 来选择路由。
- 模型之间只传递经过用户确认的冻结 spec/contract/task 上下文；技术模型不得根据原始聊天记录改变业务需求。技术模型产出的机器报告不能直接伪装成用户确认后的解释。
- 对每个 run 记录方法索引版本、检索查询与候选、兼容性拒绝理由、最终 asset_refs/hash、覆盖的 acceptance_ids、Agent 缺口和回退路径；失败或找不到资产时只能产生明确缺口，不得把相似代码当成正确代码。
```

### 阶段约束速查

| 主题 | 必须遵循的边界 |
|---|---|
| 需求范围 | 完整 Demo 最多 1—3 个**业务实体**、2 类**业务角色**、8 个**业务页面**；认证实体/页面另计。超范围必须协商缩减，拒绝缩减则停止。 |
| 访谈 | 每轮最多 3 个问题、最多 4 轮；题型单选/多选/文本；待确认核心项清零才能冻结。 |
| 生成栈 | 生成端 Vue 3 JS/TS + Spring Boot MyBatis/MyBatis-Plus + MySQL/Maven，四种组合全部经过构建验证才启用。 |
| 资产优先 | 候选检索/确定性兼容过滤 → COPY → RENDER → STRUCTURED_EDIT → AGENT_PATCH → 有严格理由的 AGENT_GENERATE；DeepAgent 仅解决真实缺口和局部修复。 |
| 方法库与索引 | 已审核源码与 manifest 由 Git/文件仓库保存；SQLite FTS5 作为**可重建的本地索引**，不把原始源码混入平台业务元数据库；P0 不引入向量数据库。 |
| 复用的判断 | `Method Unit` 可用于方法、类、小型片段，但仅有相似关键字不能认定覆盖；必须检查 Profile、依赖、参数、权限、冲突、扩展点和验收契约，不确定则标缺口。 |
| 资产沉淀 | run 内成功 Patch 先留在项目产物；经过来源/许可证、通用化、独立测试、人工审核、不可变版本发布与索引更新，才能进入公共仓库。 |
| 安全 | Runner 独立隔离；Agent 无 shell、无任意网络/仓库、无平台密钥、无受保护文件写权限；外部文档是不可信数据。 |
| 限额 | 每任务最多 2 次业务修复、全 run 最多 6 次；初始建议调用上限 30、工具步数 40、单次模型等待 120 秒、总运行 20 分钟；参数可在配置与测试后调整。 |
| 验收 | 必需项 `PASS` 才可判定 SUCCEEDED；`FAIL` / `NOT_RUN` 不可隐匿；外部 Live 就绪与代码交付结果分开。 |

### 方法代码资产体系：新增约定（不倒改 MVP）

本节属于**新增工程实施决策**，不声称 MVP 现有 Registry 已支持方法粒度。实现前检查真实代码，以适配器或注册表扩展为先，不假定某个推荐路径已经存在。

| 层次 | 含义与边界 | 主要复用方式 |
|---|---|---|
| `Template` | Spring Boot/Vue 基础工程、约束和依赖锁，沿用 MVP 已发布版本 | `COPY / RENDER` |
| `Capability Pack` | CRUD、权限、分页、状态守卫等跨文件完整功能模块，沿用既有 manifest/Resolver | `COPY / RENDER` |
| **`Method Unit`（新增）** | 经过审核的单方法、类、算法辅助函数或小片段；含源文件、调用契约、所在层、依赖、导入、扩展点、测试和许可证 | `RENDER / STRUCTURED_EDIT`；不可用任意字符串替换注入 |
| `Recipe` | 组合多个资产和任务依赖的业务模式；先保留后续扩展边界 | 已锁定资产引用 + DAG |
| `Integration Pack` | 第三方 API 的 Port/Adapter/Mock 和外部配置，仍归 E01—E07 | 优先复用适配器；Live 不等同 Mock |

**Method Unit 最小 manifest：** `id/version/kind/language/framework/compatible_profiles/provides/requires/conflicts/parameter_schema/extension_points/owned_paths/checks/checksum/license`（与现有契约同名的字段保持一致）；额外记录 `symbol/signature/layer/imports/preconditions/postconditions/side_effects/security_constraints/acceptance_ids/source_provenance`。新增字段对旧 Template/Capability Pack 应有安全默认值或使用 `kind` 分支 schema；Method Unit 必需的签名/来源等仍须严格校验，不能因兼容默认值而放宽发布门槛。其中“已通过哪个独立测试”比“包含哪些关键词”更重要。`owned_paths` 仅描述资产可安装的目标范围，不赋予 Agent 任意目录权限；依赖许可证不兼容或来源不明时不入库。

**建议存储形态：** 审核通过的 `methods/<asset-id>/<semver>/manifest.yaml`、`src/`、`tests/` 置于版本控制下（路径仅为建议）；SQLite/FTS5 仅存规范化索引键、标签、摘要、Profile/依赖关联和资产路径/哈希。索引支持从 manifest 全量重建与增量更新；源代码与 manifest 才是权威数据，丢失索引不能丢失资产。快照 `index_schema_version/index_revision` 用于检索审计；**锁重放只信 `asset-lock.json` 内的不可变 ID/版本/checksum，不依赖再次搜索得出同样排名**。

**复用判定顺序：** 冻结 `spec/contract/acceptance_ids` → 提取规范化能力需求 → 精确能力 ID 和技术约束检索 → FTS5 关键词/标签候选召回 → Profile/语言/版本/依赖/冲突/扩展点的确定性筛选 → 对照 `provides`、业务前后置条件、权限和独立验收覆盖 → 稳定排序/产出拒绝原因 → Resolver 锁定 → 安全装配/全套 Runner 验收。语义不确定、仅“看起来类似”或缺少独立证据时，不得自动认定满足需求；转为明确 `Gap Task`。**P0 不增加专门“判断是否存在模板”的大模型**，未来模型只可建议候选，不能替代验证器。

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
confirmed spec.json + contract + acceptance_ids（唯一权威业务输入）
    ↓ Python 规范化能力需求 → Method/Capability/Template Finder（精确索引 + FTS5）
候选资产及拒绝原因 → 确定性 Profile/依赖/契约/覆盖校验
    ↓ 复杂场景按需 Qwen3.7 Plus 提议任务（R13 后启用；无规划必要则跳过）
Python Resolver/Validator 选择不可变资产与任务 DAG → asset-lock.json
    ↓ COPY/RENDER/经验证 STRUCTURED_EDIT（优先复用可用方法）
验收规则对照 gap_check
    ├─ 全覆盖：不调用 Coding Model
    └─ 未覆盖：Qwen3-Coder-Flash / DeepAgent（仅局部授权 Patch）
                                └─ 必要时 Qwen3.8 Max（预算内受限升级）
    ↓ Runner 独立验收（不调用 AI；失败 Patch 不覆盖公共资产）
事实报告 → 结构化状态/模板显示；需要通俗 AI 解释时 → DeepSeek
```

**接口与实现要求：**

1. 增强 MVP M21 的统一路由表，新增 `technical_planning`、`coding`、`complex_repair`（默认禁用，达到对应 R 步骤且完成探针后逐一启用）；`user_interaction` 始终指向 DeepSeek 官方的 `deepseek-flash`。所有任务按**任务类型/失败等级**由 Python 静态路由，不新增“路由决策模型”。
2. `ModelCallResult` 记录 Provider、API 模型 ID、任务角色、思考模式、请求标识、usage、价格版本与错误分类；访谈发生在 run 前须用 `project_id + draft_id` 记录费用。现有 `ModelCall` 只有可空 `run_id`，R13 给生成期增加可空 `task_id` 并逐次传递，R20 再完成按 `run_id + task_id` 的汇总/对账；旧账本行与无任务上下文的 run 级调用仍可读取，不能伪造 task_id。每次内部 Agent 模型请求独立计账。
3. R03 在 DeepSeek 客户端与统一网关实测 `thinking.type=disabled/enabled` 与 `reasoning_effort=low/high`，并记录实际请求模式；普通问题非思考，已识别冲突按确定性规则选择已验证的 `enabled + low`，复杂最终汇总的 `high` 留给 R06。若所用接口不支持强度参数，明确记录探针结论并阻止该模式启用，不能静默忽略后声称已支持。不得把隐藏思考文本记录在提示词、日志或结果包。原始中文访谈不转交 Qwen，Qwen 不重新判定用户已确认的业务意图。
4. DeepSeek 的用户可读说明只能重述可信结构化状态（例如真实 `FAIL` / `NOT_RUN` / 成本），不能将失败写成成功；如 DeepSeek 不可用，使用确定性模板说明并暴露“AI 解释不可用”，不静默改用 Qwen。
5. **模型可用性按地域验证。** `qwen3-coder-flash` 的 Function Calling 能力随部署地域/服务方式有差异；必须在准备运行 R16 时用真实工具调用测试，不可仅依据型号名称。Provider ID、版本、区域、支持能力、定价版本及验证日期都要归档；不可硬编码当前价格或接入真实密钥。

6. 新增 `AssetFinder` 作为 MVP M13/M14 的只读检索适配层；Registry 提供资产真值，索引只做召回，Resolver 最终判定并生成锁，避免新旧两套互不一致的资产选择规则。
7. `AssetSearchResult` 至少记录 `query_hash/index_revision/candidate_refs/match_type/filter_reasons/selected_refs/uncovered_acceptance_ids`；查询日志仅含脱敏结构化条件，不保存用户原文或第三方秘密。方法声明的 `provides` 必须能够映射到冻结契约与受保护验收 ID。
8. 限定 `Method Unit` 的适配边界：只有匹配已声明 `extension_points` 的参数化渲染或经过测试的结构化源码修改可以自动执行；任意拼接源码、简单全文替换、写到模板原件或覆盖生成项目固定测试均被拒绝。兼容但无法安全植入时仍应产生 gap。
9. `asset-lock.json` 新增可选的 `method_units` 及 `index_schema_version/index_revision/selection_reason` 审计字段时必须保持旧锁可读取；`schema_version=0.1` 的锁哈希只使用原 0.1 字段集，明确排除新增字段的默认值，不得直接对扩展后的模型做无版本区分的 `model_dump()` 再哈希，也不得笼统用 `exclude_unset` 改变旧字段的原有序列化结果；新锁按新版本字段集计算。发布后的 asset ID+版本+checksum 不可更改；检索索引变化不能修改正在执行的 run 已冻结的资产选择。
10. 代码生成成功不是公共资产发布成功。公共方法入库应在后续 E10 执行提取、最小依赖声明、通用化、许可证与安全审核、独立测试/兼容矩阵、人工确认、发布新版本和更新索引；不让运行中的 Agent 直接写 Registry。

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
- **里程碑 D2｜方法资产检索与复杂业务覆盖闭环（R06A—R06D + R07—R13）**：在 MVP Registry/Resolver 上增加方法级资产、可重建索引和确定性选用，再完成四 Profile、多实体、唯一约束和校园活动主线。
- **里程碑 D3｜受限 Agent 与修复（R14—R19）**：先留存资产检索/不匹配证据，只有真实未覆盖规则才调用 DeepAgent，修复后重跑固定检查。
- **里程碑 D4｜运维可靠性与完整验收（R20—R27）**：成本、取消、恢复、历史、9 次评估，形成完整 Demo P0 证据。
- **里程碑 E｜后续能力（E01—E10）**：逐步增加第三方集成、Live/Mock、在线预览、已有项目迭代；E10 从已验收 Patch 中人工提炼、发布新方法资产并测量复用收益。

---

# 第一部分｜R01—R27：补齐完整 Demo P0

## D1｜需求访谈与可确认规格

### R01｜扩充业务范围校验器

- **Goal：** 把 MVP 固定 `Task` 的限制升级为规则可配置的 1—3 业务实体、最多 2 角色、最多 8 业务页面，同时建立明确的超范围反馈。
- **Context：** 参考 MVP M06/M07 与 `spec.py` 的 `MVP_ENTITY_LIMIT=1`、固定 Task 字段/角色规则，以及 `contract.py` 的单实体编译门禁；需求文档 FR01 与技术文档第 9 节要求 1—3 个业务实体。R10 才扩展契约和装配，不把范围校验通过误当作当前生成器已支持。
- **Constraints：** 在 `errors.py:ErrorCode` 统一新增 `UNSUPPORTED_SCOPE`（超出 Demo 范围）与 `SCOPE_CAPABILITY_NOT_READY`（规格合法，但 R10 多实体生成能力未就绪），固定各自 HTTP 422 响应；R10 解除门禁时复用同一代码，不另造错误名。将通用的实体数、角色数、业务页面数、字段安全性和权限规则，与仅属于旧 `Task` MVP 的固定字段/单实体规则分离；旧版已确认 Task spec 及冻结 hash 不改写。R01 可识别并校验 2—3 实体候选，但 R10 前生成入口必须返回 `SCOPE_CAPABILITY_NOT_READY`，不得落入旧 `contract.py` 单实体路径或宣称可生成。不自动删除超范围功能；要求用户确认缩减后的规格；认证实体/页面从业务限额中排除。
- **WhenDone：** 1/3/4 实体、2/3 角色、8/9 页面边界与字段/权限规则测试通过；旧 Task 确认与生成回归 PASS；合法多实体候选不被 MVP 固定 Task 校验误拒，而 R10 前提交生成返回 HTTP 422/`SCOPE_CAPABILITY_NOT_READY`；超限返回 HTTP 422/`UNSUPPORTED_SCOPE` 和可解释的缩减建议，未确认的缩减不能提交 run。

### R02｜建立访谈题目与答案 Schema

- **Goal：** 定义 `question_id/type/prompt/options/allow_custom/required/affects/version` 及回答协议，支持单选、多选、文本、自定义和“不确定”。
- **Context：** 依赖 R01 及 MVP M06/M07；需求文档 FR02、技术文档第 5 节的问答契约；下一步 R03 的模型输出和 R05 的 UI 都依赖它。
- **Constraints：** 答案必须绑定题目版本；`affects` 明确本问题改变哪项业务决定；不要求普通用户理解 ORM 或框架；本步不调用模型。
- **WhenDone：** 题型与非法输入均有 Pydantic 校验；旧版题目答案无法写入新版本；schema 测试通过。

### R03｜增加基于缺失字段的动态问题生成

- **Goal：** 使用 **DeepSeek V4.1 Flash（`user_interaction` 路由）** 根据已确认信息与缺失/冲突项一次生成最多 3 个具体的中文澄清问题，同时输出未收敛字段和影响业务决定的 `affects`。
- **Context：** 依赖 R02、M21/M22 的 `deepseek-flash` 网关及生成前记账；当前 `clients.py` 只下发 `thinking.type`，`requirement_extraction.py` 固定 `thinking_mode="disabled"`，`ModelCall` 只有 `thinking_mode` 列，尚无 `reasoning_effort`；需求文档第 5 节给出缺口清单。
- **Constraints：** 在本步为 `model_call` 增加可空 `reasoning_effort` 列并做向后兼容迁移，旧调用保持 NULL；客户端、网关预留/结算与账本/审计分别传递 `thinking_mode` 和 `reasoning_effort`，不把 `low/high` 塞进 `thinking_mode`。仅对实测支持的思考模式下发 `low/high`；普通缺失字段默认 `thinking.type=disabled` 且 `reasoning_effort=NULL`，由 Python 对已答信息的明确冲突作确定性检测后使用 `enabled + low`，不得由模型自行决定切换。适配层验证参数组合和供应商响应，未知/不支持时返回可解释错误或保持非思考路径，不能静默丢弃强度参数并标记思考已启用。严禁改用 Qwen 生成用户问题；不重复提问，“不确定”不得当作已知；校验结构、调用次数与费用；不保存 `reasoning_content`。
- **WhenDone：** `disabled` 与冲突触发的 `enabled + low` 均有真实接口探针和请求载荷/usage 证据；支持与不支持强度参数的模拟响应均有断言；迁移前账本行仍可读取且 `reasoning_effort=NULL`，新行可区分模式与强度；校园活动需求最多返回 3 个相关问题并含 `question_id/affects`，无冲突已答字段不重复提问，路由只命中 `deepseek-flash`。

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
- **Constraints：** `pending` 核心项未清零不得确认；复杂汇总若选择 `enabled + high`，须复用 R03 已验证的参数/账本路径，不支持时不得声称已使用 high；DeepSeek 只是起草和解释，不能替用户同意；技术规划模型不得在冻结后重解释原对话或改业务规则；生成中的 spec 不允许原地编辑，冻结哈希进入 run。
- **WhenDone：** 可查看规格版本差异；旧 run 指向旧 spec 的 hash；人为篡改冻结的验收断言会在检查中被拒绝。

---

## D2｜方法资产仓库、索引与复杂业务覆盖

> **执行顺序：** `R06A → R06B → R06C → R06D → R07—R13`。新增 R06A—R06D 在第二阶段实施，MVP M13/M14/M15 保持已完成状态且不回填新能力。

### R06A｜扩展资产契约：增加可复用 Method Unit 类型

- **Goal：** 以**兼容 MVP 已发布资产**的方式扩展 manifest/schema 与 Registry 读接口，新增 `Method Unit`（方法/类/片段）分类、稳定 ID、不可变版本和结构化调用契约。
- **Context：** 依赖已完成 M08/M13/M14/M15 和 R06 冻结规格；现有 `AssetManifest`、`LockedAsset`、`AssetLock` 均设置 `extra="forbid"`，旧锁哈希由模型序列化字段计算。本步属于新增工程增强而非 MVP 补课，后续 R06B 构建方法存储和索引。
- **Constraints：** 保留原 manifest 字段及旧资产读取/锁格式；为旧 Template/Capability Pack 的新增元数据提供安全默认值，或以 `kind/schema_version` 分支解析；Method Unit 的 `symbol/signature/layer/imports/preconditions/postconditions/side_effects/security_constraints/acceptance_ids/source_provenance` 按新类型严格验证，不得用默认值掩盖缺签名/来源。新增锁字段须可选并有旧版本解析策略；校验 `schema_version=0.1` 旧锁时使用原字段白名单构造哈希输入，显式排除新字段默认值，避免扩展后的 `model_dump()` 改变 canonical payload；不得用全局 `exclude_unset` 影响原字段，新锁按新版本字段集计算。只允许声明已存在于 Profile/契约的安全扩展点；源代码/manifest 的写入权限与运行期 Agent 隔离；许可证和来源不明的代码拒绝发布。
- **WhenDone：** 原有 Task 的 Registry/Resolver/lock 和冷启动回归原样 PASS；用已发布旧 manifest 和 MVP 基线生成并保存的原版 `asset-lock.json` 作回归夹具，无需编辑即可解析、哈希校验、按固定 ID/version/checksum 重放；新锁可解析且篡改被拒；最小 Method Unit 示例通过检查，缺签名、非法依赖、未知 Profile、危险 owned_paths、冲突或无合法来源的样例被拒绝。

### R06B｜建立方法代码仓库与可重建 SQLite/FTS5 索引

- **Goal：** 对经过审核的 Method Unit 建立文件/Git 版本化存储与本地 SQLite/FTS5 只读检索索引，实现按资产版本原子发布、全量重建与增量重索引。
- **Context：** 依赖 R06A；可复用方法包含源码、manifest、最小测试与来源证明；M03 已有平台 SQLite 元数据机制，但方法**原始源码仍保存在文件仓库**，索引是可丢弃的派生视图。
- **Constraints：** 检查现有目录后再决定新增 `methods/` 等路径；索引保存 `id/version/kind/language/framework/profile/tags/provides/description/asset_path/checksum` 等检索字段和适用关系，不以索引内容替代 manifest 校验；构建索引使用受控文件树和事务/原子切换，禁止跟随符号链接扫描宿主敏感路径；不在数据库存密钥或未经清洗的私有业务源码，不引入向量库/远程索引服务。
- **WhenDone：** 建立至少三个小型、可独立测试的示例方法资产（可为字段校验、日期处理、业务状态守卫等；不得暗示已经能覆盖任意业务），通过全量构建、增量更新、删除后重建、checksum 篡改拒绝、索引损坏恢复及并发读取测试；源代码仓库不变时重建得到等价结果，MVP 回归 PASS。

### R06C｜实现 Asset Finder：精确检索、关键词召回与确定性兼容筛选

- **Goal：** 从 confirmed spec、冻结 contract 和 `acceptance_ids` 派生规范化能力查询，先按能力 ID/tag/技术栈精确匹配，再用 FTS5 检索相近候选，对 Template/Capability/Method Unit 统一做可审计兼容筛选。
- **Context：** 依赖 R06A/R06B 和既有 MVP M13/M14；Finder 是 Registry/Resolver 之前的**只读候选层**，不依靠大模型选择真实资产；R06D 将消费候选结果。
- **Constraints：** 候选必须经过 Profile、语言/框架版本、`requires/provides/conflicts`、参数、`extension_points`、许可证、权限规则、前后置条件及验收覆盖校验；排序固定且可重复，返回 `EXACT/PARAMETERIZED/STRUCTURED_CANDIDATE/NO_MATCH` 与拒绝原因；关键词相似度只用于召回，不能替代语义/权限/接口证据；不能确定则 `NO_MATCH`，转为 gap，严禁偷偷调用 Qwen/DeepSeek 帮忙裁决。
- **WhenDone：** 对同一冻结查询返回稳定候选列表、版本与拒绝原因；可区分“没有检索到”“检索到但 Profile 不兼容”“候选兼容但业务语义/验收不足”；构造相似方法名、依赖冲突、权限不等价和不安全扩展点均不会被误选；未命中不导致生成失败伪装为成功，测试 PASS。

### R06D｜把方法索引接入既有 Resolver/装配器与 Asset Lock

- **Goal：** 以扩展适配器接通 `Asset Finder → MVP Resolver → asset-lock → M15 装配器`，支持已验证方法的直接 COPY/参数化 RENDER/受约束 STRUCTURED_EDIT，并保留失败时的明确 Gap。
- **Context：** 依赖 R06A—R06C 以及已实现 M14/M15/M17/M18/M24；R07—R13 的新增业务能力优先经此流程查找；已有 Task MVP 继续可只用模板/能力包、无需 Method Unit。
- **Constraints：** 先由完整模板/能力包占有已有 `provides/owned_paths`，方法不得重复实现认证、权限或覆盖原能力；引用冻结 `asset_id/version/checksum` 与索引快照元数据，运行期间不动态换版本；只允许白名单扩展点的结构化植入，改动后运行原有固定检查；索引故障应进入可解释的阻塞/降级策略，不能使用未经审核的代码或静默越过必需检查；复用 R06A 的版本化锁解析/哈希规则，旧锁不因新字段默认值而改变字节内容或哈希。
- **WhenDone：** 固定 Task 无额外方法时仍按 MVP 原路径生成/验收；一个含预审核方法的测试规格可通过 Finder 锁定、装配并执行 Runner HTTP 回归；重复检索、重复装配结果可复现；未命中/冲突/无安全扩展点会产生结构化 Gap 或错误而非文件覆盖；旧 `asset-lock.json` 仍能重放并校验。

### R07｜补充 Vue 3 + JavaScript Profile 分支

- **Goal：** 从已通过的 TS 基线派生 JS 模板，只替换真正不同的前端结构与构建配置。
- **Context：** 依赖 MVP M08—M12；需求文档 FR04 要求 JS/TS 可选择，技术文档第 2 节通过不可变 Platform Profile 管理兼容性；新增方法级检索基线 R06D 已就绪，新 JS 资产发布时须刷新索引与兼容关系。
- **Constraints：** 将 `spec.py:is_supported_profile` 的单一 `demo-ts-mp@0.3` 常量判断改为读取已发布、已认证 Profile 的确定性规则，校验 `profile_id/profile_version/template_version` 一致性及资产兼容性；只有经 R07 构建认证的 JS 组合才加入允许集，未认证的其余组合继续拒绝。MVP 固定 Task 字段规则应按旧 spec/场景适用，不能绑定在全局 Profile 判断中。已发布 TS Profile/模板不原地覆盖；不得将 TS 类型文件无脑复制到 JS 模板；保持相同接口与业务验收。
- **WhenDone：** JS 模板完成 `npm ci`、`npm run build` 及同一 Task HTTP 验收；已认证 TS/JS Profile 均能通过规格确认、Resolver 和锁定流程，未知版本/模板不匹配被拒；旧 TS Profile 与锁可重放。

### R08｜补充 Spring Boot + MyBatis 分支

- **Goal：** 制作 MyBatis（非 Plus）的后端模板/映射能力包，复用同一实体及 REST 业务契约。
- **Context：** 依赖 MVP M08—M11 和 R07 的版本思路；需求文档 FR04 要求两种 ORM 可选择。
- **Constraints：** 不改写已认证的 MyBatis-Plus 资产；MyBatis Profile 与模板/能力包用新 ID 或新版本发布，经构建认证后才加入 R07 建立的规则允许集。普通 CRUD 可以多一些显式 SQL，但权限/分页验收结果必须一致；保留适配层边界；Finder 必须按 ORM/Profile 过滤 Method Unit，不得复用只针对 Plus 的实现。
- **WhenDone：** MyBatis Task 工程可以构建启动并通过同一业务测试；不使用 Plus 专属 API；新 Profile 经规格确认与 Resolver 锁定后可生成，旧 Plus 资产/锁回归通过。

### R09｜四组合 Profile 认证与高级技术选项

- **Goal：** 完成 JS/TS × MyBatis/MyBatis-Plus 四种 Profile 的独立锁、兼容矩阵、构建及前端高级选择；**仅在需要 AI 进行面向用户的技术通俗解释时调用 DeepSeek `deepseek-flash`**。
- **Context：** 依赖 R07/R08、M08、M13/M14；需求文档第 6、8 节要求四组合都通过基础模板构建，界面说明特点、局限与推荐理由。
- **Constraints：** 选型与 Profile 兼容判断由 R07 建立的确定性规则执行，支持四组合且仅启用经认证的精确版本/模板/资产矩阵；旧 `demo-ts-mp@0.3` 保持可读可用，不原地改已发布资产。DeepSeek 只解释经验证的差异；启用前四组合需经过基础构建和 Task 回归；用户不能指定任意依赖版本。
- **WhenDone：** 四种组合均有可追溯 `profile_id/version`、构建报告及验收记录；四组合分别走通规格确认 → Resolver → 锁 → 构建/Task 回归，非法组合或未认证版本被拒；默认继续推荐 TS + MyBatis-Plus；四组合各至少验证一次 Method Unit 候选的兼容/不兼容筛选，旧 Profile 锁重放不受索引刷新影响。

### R10｜支持第 2—3 个业务实体与关系映射

- **Goal：** 将装配器从单 Task 扩展为有明确一对多/多对一或关联实体关系的 2—3 业务实体装配；每一关系/辅助逻辑先查询已审核 Capability Pack/Method Unit，未覆盖的关系才按已有确定性路径实现。
- **Context：** 依赖 M06/M10—M12/M15、R01/R06；当前 `contract.py` 仍有 `MVP_ENTITY_LIMIT=1` 且只编译 `entities[0]`，MVP 能力包/装配器也是 Task 定制。技术文档第 6 节强调“数据库、DTO、REST 契约先冻结，再装配后端/前端”。
- **Constraints：** 先扩展确定性契约编译，再扩展相应能力包、装配器和 Runner；对每个实体/关系生成独立表、DTO、REST、权限及外键，不得只放开 R01 校验却继续丢弃第二实体。先仅处理确定性关系模式；外键、字段、路由命名冲突需在写文件前拒绝；不自动允许模型新增第 4 个实体。R01 设置的多实体生成门禁仅在完整链路通过后解除。
- **WhenDone：** 图书借阅或活动报名的一套 2—3 实体规格能生成含全部实体和正确外键的 migration、服务、页面及权限/HTTP 验收；缺一实体即失败；重复渲染无漂移；旧单 Task 生成和验收 PASS。

### R11｜唯一约束与状态守卫能力包

- **Goal：** 新增数据库唯一约束、业务状态检查及错误码映射能力包，覆盖“不能重复报名”“活动结束不可报名”。
- **Context：** 依赖 R10；需求文档第 5、8 节给出了校园活动报名的关键规则；技术文档第 2、9 节要求 `CRUD + RBAC + 数据归属 + 状态守卫` 组合回归。
- **Constraints：** 不只做前端防重；并发重复请求靠数据库约束兜底；状态条件必须服务端检查；错误不能返回成功掩盖重复失败。契约中的 `unique_constraints` 必须由确定性的 migration 渲染路径消费；若新增能力包负责渲染，不得与既有 CRUD 包重复创建同一约束或静默忽略契约字段；优先用现有状态守卫/唯一约束的已审核资产或方法单元，不允许仅用“存在状态守卫方法”推定数据库唯一性和并发正确性；最终以真实测试为准。
- **WhenDone：** 非空 `unique_constraints` 的生成 migration 含相应数据库唯一约束，并经真实数据库迁移和重复插入测试验证；并发报名后同一成员-活动最多有一条有效记录；结束后报名被拒；数据库约束/接口测试均有 PASS 证据。

### R12｜校园活动主线：角色、页面与固定业务验收

- **Goal：** 用已增加的实体/能力包装配校园活动管理，并覆盖管理员发布、成员报名、列表分页、报名查看权限。
- **Context：** 依赖 R09—R11；需求文档第 3、8 节确定该项目为必需演示主线；M18 的 Runner 需要扩展固定业务测试模板。
- **Constraints：** 管理员发布，成员不得发布或访问他人记录；验收断言根据确认规格及平台固定模板冻结，不交给 DeepAgent 编辑。
- **WhenDone：** 至少一套校园活动全栈产物真实通过规定 HTTP 场景：不可重复报名、不可越权、活动结束拒绝报名、刷新持久化及分页总数；报告中额外列出该 run 的候选方法、实际复用方法 ID/版本及未覆盖规则，原有校园活动验收断言不变。

### R13｜接口契约先行的多任务 DAG 规划

- **Goal：** 在需要 AI 规划时由 **Qwen3.7 Plus（`qwen3.7-plus`）** 基于**用户确认的冻结 spec** 提出接口/DTO/SQL/权限契约和 `TaskPlan`；Python 冻结、校验并决定最终任务 DAG；技术规划之前先执行 Asset Finder，向 Qwen 只提供已校验的候选/缺口摘要，尽量避免重复规划可直接装配的常规任务。
- **Context：** 依赖 R10—R12、M07 的已确认 spec/hash 与 M14/M15；技术文档第 4 节定义 `TaskPlan.task_id`，第 8 节要求每次内部请求继承 `run_id/task_id/call_id`。当前 `RunTask` 已有 `task_id`，`ModelCall` 只有 `run_id`；由 R06 的 DeepSeek 用户摘要交接，但**不得读取未确认原始聊天**；任务计划须引用 R06D 选定资产与冻结索引快照。
- **Constraints：** 在本步为 `model_call` 增加可空 `task_id` 的向后兼容迁移，并从 DAG 任务上下文经网关预留/结算传到每次生成期模型请求；`task_id` 必须属于该 `run_id`，无任务上下文的 run 级调用保持空值，生成前 `project_id + draft_id` 调用的 `run_id/task_id` 均为空，旧行无需回填。静态任务继续使用确定性转换，只有确有规划复杂性才调用 `technical_planning`；模型只能提议能力和任务，不得改 confirmed spec、权限、验收或依赖版本；Python 做 DAG 无环、冲突和覆盖校验，按依赖串行提交；Qwen 的方法建议不得越过兼容筛选。
- **WhenDone：** 同一冻结 spec/hash 的计划具备可追溯输出、输入摘要 hash 和 schema 验证；无环、越界 allowed_paths、同路径冲突被拒；用已登记的 `RunTask` 测试任务内网关调用，账本行同时含正确 `run_id/task_id/call_id`，错误 run/task 配对被拒，迁移前旧账本行及生成前账本仍可读取；DAG 建立前的规划调用保留空 `task_id`；模型日志仅出现允许的 `technical_planning` 路由，TaskPlan 标注执行模式与检索拒绝原因，已覆盖任务可跳过技术模型。

---

## D3｜有明确边界的 DeepAgent 补缺与有限修复

### R14｜实现能力覆盖检查与 Gap Task 生成

- **Goal：** 将装配计划与已冻结验收场景逐项对比，输出 `covered_acceptance_ids/uncovered_rules/gap_reason`；仅对真实缺口创建 `AGENT_PATCH` 任务；覆盖比较同时消费 R06C/D 的方法级候选与拒绝证据，区分“方法找到但不适配”和“真正没有方法”，并形成可定位到 `acceptance_ids` 的缺口。
- **Context：** 依赖 M13/M14、R13；技术文档第 4 节要求资产覆盖依据是验收规则而非代码行数；第 3 节明确无缺口直接跳过 Agent。
- **Constraints：** 覆盖判断由 Python Resolver/固定验收 ID 决定，Qwen 规划提议不能直接算“已覆盖”；资产无法覆盖不能标记已完成或放宽需求；缺口标明授权代码位置与预期验收；没有缺口时不调用 `coding`；method 的 `provides` 声明或相似关键词不能单独证明业务规则已覆盖，必须结合冻结权限/前后置条件、可插入性以及独立检查证明；已装配资产未通过验收不算覆盖。
- **WhenDone：** 已覆盖的标准 Task run 模型编码次数为 0；用固定特殊规则构造的缺口能形成可解释、可执行且路径有限的 TaskPlan；对“已有适配方法”和“仅名字相似的错误方法”分别验证 coding 调用为 0 与产生安全 Gap 的行为。

### R15｜为 Agent 构建受限暂存文件后端

- **Goal：** 让编码任务只能读取授权源码/只读契约，写入当前 run/task 专属暂存目录；提交前生成 `FileChangeSet`。
- **Context：** 依赖 M15/M16、R13/R14；技术文档第 4、6 节定义 `allowed_paths`、基准 hash、临时区、受保护文件和原子提交。
- **Constraints：** 文件 backend 在每次读写时执行权限/路径检查，最终提交再校验；拒绝 `..`、绝对路径、符号链接逃逸、过大文件、认证模板、`asset-lock.json` 和固定验收断言修改；Agent 不得修改版本化方法仓库、Registry、FTS5 索引或发布队列；只能修改 run 专属产物，Asset Finder/manifest 为只读输入。
- **WhenDone：** 正常业务扩展文件可暂存、校验并提交；越界读写和受保护修改在工具层即时拒绝；失败不污染正式源码，测试通过。

### R16｜核对并接入 DeepAgent 的实际工具集

- **Goal：** 使用经兼容性验证的 `deepagents` 创建单任务 patch engine，并接入 **Qwen3-Coder-Flash（`qwen3-coder-flash`）**，验证所选地域的工具调用、backend、账本及输出协议。
- **Context：** 依赖 M21 的跨 Provider 网关、R15 文件 backend；技术文档第 1、6、9 节要求 `create_deep_agent` 是 fallback，不是独立模型；选型补充的 `coding` 角色指定百炼 `qwen3-coder-flash`。
- **Constraints：** **先对实际使用的百炼地域/接口执行 Function Calling 探针**，若该地域不支持则本步 `NOT_RUN/阻塞`，不得假装模型有工具能力或未经批准换型号；审计真实工具表、禁止 shell/任意网络/未受控委派；每个内部模型请求继承 run/task/call_id，独立预留结算，不只统计外层 invoke。
- **WhenDone：** `qwen3-coder-flash` 在目标地域完成真实受限工具调用与暂存写入；危险工具/越界均被拒；内部所有调用入账。若工具能力不支持，记录地域/接口证据和阻塞，不声明 R16 PASS。

### R17｜实现“装配优先，缺口局部 Patch”

- **Goal：** 将 R14 产生的真实缺口交给由 **Qwen3-Coder-Flash** 驱动的 R16 patch engine，并在变更校验通过后按 R15 原子提交；如果存在经验证、可安全装配的 Method Unit，则优先回到 Resolver 执行该资产，不得仅因为 Planner 提议新写就直接调用 Agent。
- **Context：** 依赖 R13—R16；技术文档第 3 节的 `gap_check → patch → commit → verify`，第 6 节要求 TaskContext 包含冻结规格片段、契约、依赖和授权文件。
- **Constraints：** Agent 只消费 confirmed spec、冻结契约和 task.allowed_paths，不直接获取 DeepSeek 对话历史；不可扩大需求、改模板原件、自动升级依赖或重写模块；不提供固定测试写权限；未通过 schema/文件校验不提交；`NO_MATCH`、契约/权限不符或扩展点无法安全使用时才允许 Patch；Model 输出不得自动登记为新 Method Unit，且每次 Patch 与检索轨迹互相关联。
- **WhenDone：** 一条受控的“资产未覆盖但在项目扩展点内”的业务规则由 Agent 小范围补全，变更集可审查且新增测试 PASS；标准 Task 无缺口时仍不启动 Agent；同一个特殊规则在补充一份经审核的兼容方法资产之后重跑，能在无 Coding Agent 调用的情况下复用该资产并通过测试。

### R18｜失败分类与有限自动修复

- **Goal：** 建立 `classify_failure → coding(Qwen3-Coder-Flash) → 可选 complex_repair(Qwen3.8 Max) → verify` 的修复支路，并记录 task/run 的修复次数。
- **Context：** 依赖 M17/M18、R15—R17；需求文档 FR06 限每任务 2 次、全 run 6 次；技术文档第 8 节允许第二次修复升级；本版路由默认先 `qwen3-coder-flash`，首修失败或确属复杂问题才考虑 `qwen3.8-max`。
- **Constraints：** 仅可复现的代码/契约错误进入修复；基础设施/资产冲突/外部配置不能盲改；第二次修复是否升级由 Python 明确分类并检查预算，升级仍消耗该任务第 2 次修复机会；只传脱敏相关日志、冻结契约与授权源码；上限即 FAILED；修复前可查询是否有兼容、已验证的资产版本可以替换失败代码，但禁止无审批修改不可变公共方法，且替换不得省略完整复验。
- **WhenDone：** 注入编译错误时第一轮调用 Qwen3-Coder-Flash 并复验；构造确定性的复杂错误满足升级条件且在额度内才调用 Qwen3.8 Max；超限停止，基础设施不计业务修复次数，全部模型调用有路由/成本日志。

### R19｜完整复验与最多一轮语义审查

- **Goal：** 在打包前执行完整必需验证；可选由 **Qwen3.7 Plus** 对照冻结业务规则做最多一轮技术语义审查，问题须转换为可验证的具体修复任务。
- **Context：** 依赖 R18 和原有 Runner；技术文档第 3、7 节规定 `full_verify → review → package`，模型评价不能覆盖确定性检查结果。
- **Constraints：** 语义审查只接收冻结 spec、代码和固定验收结果，不重新解释 DeepSeek 原对话；不因 Qwen 自评就绕过失败测试；如代码变化必须重新完整验收；审查 Agent 不得改冻结规格与固定测试。若需要 AI 面向用户说明技术审查结果，由 DeepSeek 在脱敏事实之上转述。
- **WhenDone：** 失败用例无法走到 SUCCEEDED；修复后所有必需项确实重新 PASS；报告保留首次失败及最终复验记录；报告保存索引版本、采用/拒绝方法、装配及 Agent 修改的来源链；方法命中且构建成功但固定业务 HTTP 失败时不得标为已覆盖。

---

## D4｜预算、可恢复执行与 Demo P0 验收

### R20｜完善 Provider 价格、usage 与多角色共享预算

- **Goal：** 将 MVP 的 DeepSeek 单 Provider 账本完善为 **DeepSeek 用户交互 + Qwen 技术规划/编码/升级修复 + 各内部重试** 共享预算与预留。
- **Context：** 依赖 M21 的生成前 project/draft 花费、R03/R06 用户交互、R13 增加的可空 `ModelCall.task_id` 及 R16/R18 内层调用；技术文档第 8 节给出预留公式、缓存/输出分项与 `price_version`，10 元只是可修改内测参数。
- **Constraints：** 所有生成期任务内请求，包括 Agent 工具循环、重试和升级修复，逐次携带 `run_id/task_id/call_id` 并进入同一预留/幂等结算路径；按 `run_id + task_id` 汇总时保留 run 级空 task、旧行空 task 及生成前 project/draft 费用的独立类别，不把空 task 误归到任意任务。各 Provider 保存实际 model ID、mode/强度、币种、定价版本与 usage；未知价格/usage 暂停对账，不写零；跨币种共享预算按锁定汇率快照计算并记录时间；本地限额不替代 Provider 限额。
- **WhenDone：** 覆盖 DeepSeek 问答 + Qwen 规划 + Qwen Agent 内层重试 + Max 升级的真实/模拟账本测试；同一 run 的两个 task 与 run 级调用分别汇总，旧行/生成前账本不丢，重复结算不重复收费，预算不足零新请求；报表区分访谈期、run 级与各 task 的金额、模型/实际模式与强度/usage/定价版本/未知状态；跨币种汇率可追溯；检索/装配不计模型 Token，复用前后实际调用与费用不得以估算冒充。

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
- **Constraints：** 恢复时不能重复提交源码或盲目重发状态不明的模型请求；UNKNOWN 调用须人工/账单对账；相同 run 的既有产物不得被覆盖；checkpoint 不得保存密钥；恢复 run 必须复用原 `asset-lock` 中的 Method Unit ID/version/checksum，不能因为 FTS5 索引已更新而重新选取其他代码。
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
- **WhenDone：** 用户能查看自己的历史规格、失败详情与重入状态；跨用户无法查看/下载；成功/失败包中的状态和验证报告一致；历史和报告可查询已采用的公共方法版本、拒绝原因及未覆盖规则，下载仍遵守 owner 和安全脱敏要求。

### R26｜核心项目矩阵回归与文档完整性审查

- **Goal：** 对校园活动、个人任务、图书借阅三种模板用例及四 Profile 进行契约、构建、启动、业务权限与交付清单回归。
- **Context：** 依赖 R01—R25；需求文档第 8 节规定所有必需检查不能缺项；技术文档第 9 节要求 Profile、资产冲突、锁重放、修复、安全与配置测试。
- **Constraints：** Mock/Live 必须分层；当前无第三方服务时 `EXTERNAL_SERVICES.md` 明确说明无须配置；新增路由回归核对“用户交互只用 DeepSeek、技术规划 Qwen3.7 Plus、缺口 Qwen3-Coder-Flash、按条件升级 Qwen3.8 Max”；不可删测试求全绿；加入方法索引全量重建、旧锁重放、Profile/依赖/权限冲突过滤、已审方法装配、索引故障回退及 Agent 无资产写权限的回归矩阵。
- **WhenDone：** 生成 ZIP 均包含 frontend/backend、SQL migration、`.env.example`、Dockerfile、compose、README、spec、接口说明、`asset-lock.json`、外部服务指南、验证与成本报告；无真实秘密；所有启用 Profile 有回归证据。

### R27｜正式 9 次独立生成评估与 Demo P0 发布判定

- **Goal：** 按固定配置独立完成校园活动、个人任务、图书借阅**各 3 次**，汇总质量、费用、延迟、资产覆盖率及失败分布；额外统计 Method Unit 与 Capability Pack 的检索命中/兼容通过/实际复用/验收通过四层指标，不把关键词命中当作可复用覆盖率。
- **Context：** 依赖 R26；需求文档第 8 节目标为 9 次中至少 7 次通过全部必需检查，并要求校园活动有完整成功产物；技术文档第 9 节区分首次通过和修复后通过。
- **Constraints：** 不人工修改源码后把该次计为成功；缺失/超时的必需测试计为失败；同时报告“成功项目平均成本”和“所有尝试费用总和/成功次数”，成功数为零写不可计算。
- **WhenDone：** `docs/DEMO_P0_ACCEPTANCE.md` 有 9 份可定位 run、报告与失败样本，逐项 PASS/FAIL/NOT_RUN、耗时、**DeepSeek/Qwen 分角色实际请求量、模式、usage、成本**、修复记录和模型路由断言；满足 7/9 且校园活动成功才标“达到文档建议验收目标”；不达标如实标注；9 份报告包含各 run 的索引快照、已锁定 Method Unit 清单、未命中/拒绝原因、Agent 实际调用量与“复用路径 vs Patch 路径”可追溯对比；仍以原定 7/9 发布门槛为准。

---

# 第二部分｜E01—E10：完整 Demo 之后的扩展（非 P0）

> **重要版本边界：** 需求文档 v0.1 第 4 节明确排除首版第三方业务接口、支付、抓取、微服务、BYOK、收费；第 9 节将在线预览、已有项目迭代、更多模板等列为后续方向。技术文档 v0.3 第 4、6、7 节描述了未来接入第三方服务时的架构约束，但**不代表首版必须实现真实支付**。以下都是**可选扩展**，每步都要新增版本与相应测试。

## E1｜第三方能力：先做一个无凭据、可 Mock 的范例

### E01｜建立 Integration Pack manifest 与版本发布策略

- **Goal：** 为第三方适配模板增加只读 Registry，定义 pack ID、版本、兼容 Profile、Port、Adapter、配置、Mock、校验和与许可证。
- **Context：** 依赖 R12/R13 和 MVP M13/M14 的资产注册机制；技术文档 v0.3 第 2、6 节关于 Integration Pack 和资产发布审核的描述；基础方法库与索引已于 R06A—R06D 建立，Integration Pack 新增类型应复用资产寻址/锁定机制而非另起一套搜索系统。
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
- **Constraints：** 验证 OpenAPI 格式、来源与允许域名；规范正文是不可信数据，不能解释为执行指令；生成 Client 后仍需人工明确签名、鉴权、幂等和回调语义；若已有经过审核的 Port/Adapter 方法资产应先检索复用；OpenAPI 生成的新 Client 仍不因通过一次构建就自动成为公共 Method Unit。
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
- **Constraints：** 起步只支持 AgentForge 自有产物；变更意图和用户可读反馈由 DeepSeek 处理，Qwen 只处理已确认的技术任务；不承诺任意仓库改造；契约变更需重新冻结并回归，旧 run 不得原地修改；升级项目应优先重用锁定的旧方法资产与兼容新版本；契约发生改变必须显式重新检索与重新冻结新锁，不能直接改旧 ZIP 的方法引用。
- **WhenDone：** 一个已完成 Task 项目增加经过确认的字段或业务规则后，生成新版本并保留旧 ZIP/报告；迁移与回归测试通过。

### E10｜从已验收代码提炼公共方法资产并发布索引新版本

- **Goal：** 在 R06A—R06D 已实现的 Method Unit 仓库/索引上，建立**人工审核的资产提炼与回灌流程**：从高频 Agent Gap/修复案例挑出可复用代码，去项目化、整理依赖与调用契约、补独立测试，发布为不可变资产并刷新索引，测量成本/质量收益。
- **Context：** 依赖 R14/R17/R19/R27、R06A—R06D 和 E09；原设计仅在 E10 引入数据驱动的资产扩充，本次将基础索引提前至 D2，故 E10 专注“从通过验收的个案到可靠公共资产”的治理闭环。
- **Constraints：** 只从**已经通过项目完整 Runner 验收**且具有可审计来源/许可证的变更中提炼候选；已验收不意味着通用性已证实，**不得自动入库**。流程为 `候选提名 → 来源/许可证/敏感信息审查 → 提取最小功能单元 → 抽离项目私有字段及密钥 → 声明参数/依赖/副作用/前后置条件/权限 → 独立单元和跨 Profile 契约测试 → 人工批准 → 发布新 asset ID/version/checksum → 原子更新/重建 FTS5 索引`。失败候选留在审查区，运行中 Agent 无公共仓库写权限；不直接修改已发布版本；不要凭方法名或向量相似度提升覆盖率；首版仍不引入向量数据库。
- **WhenDone：** 从至少一个真实缺口案例提炼出一个通用 Method Unit，保留候选 Patch、审核决定、独立回归、发布 manifest、源码哈希、索引版本和旧锁重放证据；复测相同业务场景能优先匹配并安全装配新资产，且明确测量对比**检索命中率/兼容通过率/实际复用率/验收成功率、模型调用量、真实费用和耗时**；拒绝一个不具备通用性/来源证明/安全测试的候选，证明不会污染公共库。

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
- 方法资产索引 schema/revision、查询和拒绝原因（只保留脱敏事实）：
- 新增/引用的 Method Unit ID/version/checksum 与授权扩展点：
- 资产检索命中、兼容判定、实际装配、验收覆盖和 Agent 缺口统计：
- 旧 asset-lock 重放和公共资产只读性证明：
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
| D2 / R06A—R06D、R07—R13 | FR04—FR05、四组合、校园活动验收；**R06A—R06D 为新增工程决策，不是原 FR 强制项** | 第 2—4、6、9 节 Profile、资产、任务 DAG；增量扩展 Method Unit + SQLite/FTS5 本地索引 |
| D3 / R14—R19 | FR05—FR06、有限修复 | 第 3—4、6—8 节 Agent backend、验证与故障分类 |
| D4 / R20—R27 | FR07—FR08、非功能要求、9 次评估 | 第 3—5、7—9 节费用、恢复、历史、验证报告 |
| E01—E07 | 第 4、9 节：第三方服务明确后置 | 第 4、6、7 节 Integration/External Requirements/Live |
| E08—E10 | FR09(P1) 与第 9 节后续方向；E10 加强资产沉淀治理 | 第 2、6、9 节资产发布、扩展与回归，复用 R06A—R06D 新增索引能力 |

**执行准则：** 从一个可验证闭环增加一个能力，不在同一轮同时引入新 Profile、新 Agent 工具、新 Runner 权限和新第三方 API。MVP M01—M28 已完成且不重做；R06A—R06D 先为 Method Unit 建立最小可信的“存储 → 索引 → 检索 → 安全装配”闭环，再让其他 R 步骤消费；E10 才建设从真实 Patch 提炼、人工审核并回灌公共资产的闭环。
