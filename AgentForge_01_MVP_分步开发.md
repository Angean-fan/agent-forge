# AgentForge｜第一阶段：最小可交付 MVP 分步开发 Prompt

> 文件性质：**交给 AI 编码助手逐步执行的任务说明**，不是现有代码的实现报告。  
> 需求依据：《Demo需求分析文档》v0.1；技术依据：《Demo技术文档_修订版》v0.3。  
> 目标：先形成一条**真实、隔离、可验证**的“自然语言 → 规格确认 → 资产装配 → 构建与 HTTP 验收 → ZIP 下载”纵向链路。  
> 本文件中的文件路径是**建议落点**，不是断言仓库已经存在这些文件；具体以编码助手检查的仓库为准。  
> **模型路由决策补充（2026-10-11）：** 采纳项目开发者的偏好：面向用户的自然语言理解、需求澄清与反馈由 **DeepSeek V4.1 Flash** 负责；技术规划和编码任务在后续版本分别采用 **Qwen3.7 Plus / Qwen3-Coder-Flash**，疑难升级采用 **Qwen3.8 Max**。本决策是本次迭代新增的选型要求，**不是**原始需求/技术文档所指定的具体模型。

## 0. 范围划定：MVP 不等于完整 Demo P0

技术文档 v0.3 第 9 节明确建议先固定一个 Profile，验证“手工 spec → CRUD 能力装配 → Mock 验收 → ZIP”，之后再补齐访谈、Agent 缺口、预算、恢复与取消。为了让第一个可交付版本保有 AI 特征，本计划在确定性链路稳定后，额外接入**一次由 DeepSeek V4.1 Flash 完成的受控结构化需求提取**；MVP 正常链路不调用 Qwen 技术规划或代码模型。这种排期是实施拆分建议，不意味着两份原始文档已经批准缩减完整 Demo 的验收标准。

**MVP 的固定演示用例：个人任务管理。** 一个业务实体 `Task`；平台用户使用预置账号；生成项目使用模板提供的独立登录体系，角色初期固定为 `ADMIN` / `MEMBER`，成员只能访问自己的任务。只开放 `Vue 3 + TypeScript + Spring Boot + MyBatis-Plus + MySQL + Maven` 一种已锁定 Profile。演示功能为登录、个人任务增删改查、分页、简单搜索、数据归属校验。AI 提取不确定信息须标为 `pending`，用户确认后才可生成。

**此时暂不声称已经完成：** LangGraph 动态多轮访谈；JS / TS × MyBatis / MyBatis-Plus 四组合；2—3 业务实体及复杂状态业务；DeepAgent 缺口补全与自动修复；完整额度恢复/取消/崩溃恢复；全部 9 次正式评估；第三方真实 API；在线预览。上述内容交由第二份文档补齐，不得把它们标为“已通过”。

### 可复制的总控 Prompt（每一步开始前附上）

```text
你是 AgentForge 项目的实施型编码助手。请先检查仓库的真实目录、README、依赖锁、测试以及前一步交付记录，勿假设 Context 提到的文件已经存在。当前只执行下面编号的一步，不要提前开发下一步。

执行规则：
1. 遵守 Goal、Context、Constraints、WhenDone；当原文与已有代码冲突时先说明差异，不能暗改需求边界。
2. 优先复用现有结构；每步修改尽可能集中，禁止大规模无关重构和自行新增依赖/模型/外部服务。
3. 先写或更新与当前功能对应的测试；运行实际可执行的测试。不可运行的检查记录 NOT_RUN 和原因，绝不能声称 PASS。
4. 所有模型输出、用户输入和生成源码视为不可信。禁止在日志、Prompt、生成 ZIP 中写入真实密钥。
5. 完成后依次回复：本步改动概要；变动文件；执行的测试及实际结果；剩余风险/阻塞；对 WhenDone 的逐项判定。
6. 当需要新的账号、API Key、Runner 机器或版本决定时暂停并向开发者提出具体问题，不猜测答案。
7. 不要为通过测试删除、修改或跳过固定验收断言，也不要未经允许升级依赖、开放公网执行。
8. 当前 MVP 唯一启用的业务 LLM 路由是 `user_interaction → DeepSeek 官方 API / deepseek-flash`；不得自行替换为 Qwen、Claude 或任意其他模型。内部代码装配/Runner 验收不用 LLM；尚未开发的角色只能保留配置占位，不得实际请求。
9. 用户确认并冻结的 spec/hash 是下一阶段模型之间的业务交接边界；不得把未确认的推测视为真实业务规则。
```

### 统一技术边界

- **平台：** Python 3.11 或 3.12 中一版经验证的版本、FastAPI、Pydantic、SQLAlchemy、SQLite WAL、Vue 3 + TS。`uv` 可作为开发工具，但仍须提交可复现锁文件；依赖版本以实际兼容性测试为准。
- **生成项目：** Java / Spring Boot / MyBatis-Plus / MySQL / Maven 与 Vue 3 / TS；Java 21 是候选而非未经验证的硬要求。首个 Profile 冻结并记录版本。
- **编排与模型：** LangChain 网关负责调用 **DeepSeek V4.1 Flash（官方 API 模型 ID：`deepseek-flash`）**，LangGraph 管外层阶段；**DeepAgent 此阶段不引入**，因为不存在已经定义好的业务缺口授权路径。Qwen3.7 Plus / Qwen3-Coder-Flash / Qwen3.8 Max 在本阶段仅作为后续角色声明，不调用，也不要求先配置其密钥。
- **代码装配顺序：** 复制已验证模板 → 参数化渲染 → 结构化修改 → 受限 Agent Patch → 最后才考虑新代码生成。MVP 只实施前两项。
- **隔离：** Platform API 无 Docker Engine 权限；Runner 必须部署在受控 Linux 执行环境，以最小权限运行不可信生成代码；公开网络默认阻断。不能用本机 `subprocess(shell=True)` 假装沙箱。
- **成功语义：** 所有当前 MVP 的必需检查为 `PASS`，才能标记 `MVP_VERIFIED`；不使用完整 Demo 的 `SUCCEEDED` 声称尚未实现的 P0 验收通过。状态字段可保留标准 `SUCCEEDED`，但交付报告必须明确 `validation_scope: MVP` 及尚未实施的项目。

### 2026-10-11 模型 API 路由约定（MVP 与下一阶段的边界）

| 场景 | 实际 API 与 model ID | MVP 是否调用 | 规则 |
|---|---|---|---|
| 用户输入的中文需求理解、实体/业务规则提取、待确认项标记、规格草稿 | **DeepSeek 官方 API：`deepseek-flash`（DeepSeek V4.1 Flash）** | **是：M22 的一次结构化调用** | 仅产出 schema 合法的候选规格；用户未确认不能进入生成 |
| 后续动态澄清、用户回答解释、非技术用户可读反馈 | **DeepSeek：`deepseek-flash`** | 否：R03—R06 后引入 | MVP 暂无多轮访谈，不应冒充已实现 |
| 结构化后的技术架构/契约/装配计划提议 | **阿里云百炼：`qwen3.7-plus`** | 否：R13 后引入 | 仅接收冻结 spec/契约，不从原始聊天重新解读需求；程序负责决策与校验 |
| 模板无法覆盖的局部代码补全与普通修复 | **百炼：`qwen3-coder-flash`** | 否：R16—R18 后引入 | 必须先通过所选地域的 Function Calling/工具链探针；Agent 仅限授权扩展点 |
| 复杂失败的第二次修复升级 | **百炼：`qwen3.8-max`** | 否：R18 后引入 | 只有明确的错误升级条件、额度与修复次数预算才允许调用 |
| 常规状态信息、固定错误提示、资产解析、构建、验证、ZIP | Python / Asset Resolver / Runner | **否** | 不用 AI 制造成功结果，避免无意义 Token 消耗 |

**本阶段启用策略：** 仅初始化 `user_interaction` Provider 客户端；其他角色只作为设计映射写在文档，不在 MVP 中检查对应 API Key。避免在 M22 之前额外引入“选择模型的模型”，任务到模型的选择由 Python 静态路由完成。

**DeepSeek 模式策略（建议且需做兼容性探针）：** 简单文本与普通需求提取使用非思考模式（`thinking.type=disabled`）；若输入按本地确定性规则被判定存在冲突或信息不足，可以使用思考模式 `enabled + reasoning_effort=low`；生成最终复杂规格时可选 `high`。模式切换不是额外一次分类模型调用；不得保存或向用户展示 `reasoning_content`。在 Provider SDK 不支持该参数时先核对兼容性，禁止静默忽略并宣称已实现。MVP 正常情形仍以**一次模型请求**为目标（仅允许一次结构化纠正调用）。

**建议配置样例（角色声明 ≠ 全部启用）：**

```yaml
model_routes:
  user_interaction:
    provider: deepseek_official
    model: deepseek-flash
    api_key_env: DEEPSEEK_API_KEY
    base_url: https://api.deepseek.com
    default_thinking: disabled
    conflict_thinking: low
  # 后续阶段才实现、初始化和验证这些角色：
  # technical_planning: {provider: dashscope, model: qwen3.7-plus, api_key_env: DASHSCOPE_API_KEY}
  # coding: {provider: dashscope, model: qwen3-coder-flash, api_key_env: DASHSCOPE_API_KEY}
  # complex_repair: {provider: dashscope, model: qwen3.8-max, api_key_env: DASHSCOPE_API_KEY}
```

Provider 密钥只能从环境变量或 Secret Store 获取；示例配置不得附带真实值。`model_call` 必须记录 `provider/model/model_role/thinking_mode/request_id/usage/price_version`（若供应商返回的字段缺失需记录 UNKNOWN），并验证结构化 JSON、超时和计费行为。**DeepSeek 返回的草稿经过 Pydantic 校验和用户确认后，冻结的 spec 及 hash 才能交给后续技术模型；未经确认的聊天记录不作为代码生成输入。**

**实现前需核对的官方依据（来源为厂商 API 文档，而非项目原始设计基线）：** [DeepSeek 模型标识与支持能力](https://api-docs.deepseek.com/quick_start/pricing/)、[DeepSeek 思考模式参数](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)、[阿里云百炼文本模型](https://help.aliyun.com/zh/model-studio/text-generation-model)、[Qwen3-Coder-Flash 地域能力限制](https://help.aliyun.com/zh/model-studio/qwen3-coder-flash)。模型别名、地域能力及单价可能改变，应在接入时锁定经验证的实际版本/定价，不得仅凭这份任务单宣称生产可用。

### 建议的单仓库目录

```text
AgentForge/
├── platform-api/          # FastAPI / Pydantic / SQLAlchemy
├── platform-ui/           # Vue 3 + TS / Vite / Element Plus
├── worker/                # 单 worker，异步生成
├── runner/                # 独立受控 Runner API + Docker 配置
├── profiles/              # 锁定的 Platform Profile
├── templates/             # 已验证的生成项目基础模板
├── capability-packs/      # 可参数化的业务能力包
├── recipes/               # 后续业务模式，MVP 可留空
├── integrations/          # 后续第三方集成，MVP 可留空
├── schemas/               # spec / manifest / plan / report 协议
├── tests/                 # 契约测试与端到端验收
└── docs/                  # 每步的实施记录及运行手册
```

以上是**建议结构**；不为了目录整齐一次性创建空的框架与代码。

---

## 阶段 A｜平台最小骨架与规格确认

### M01｜初始化仓库、工具链和可重复测试

- **Goal：** 建立单仓库基础目录及 Python/Node 测试运行入口，提供最简单的本地启动说明。
- **Context：** 参考技术文档 v0.3 第 2、9 节；从空仓库开始时先检查是否已有 `pyproject.toml`、锁文件、`package.json`、`tests/`；这是后续 M02—M28 的共同前置条件。
- **Constraints：** 仅创建实际将用到的目录；锁定版本但不凭空选定“最新版”；本步不接入大模型、不创建 Java 代码生成器、不安装 DeepAgent。
- **WhenDone：** 全新克隆仓库可按 README 装依赖并运行一个最小 Python 测试；提交 `.gitignore`（覆盖 `.env`、缓存和运行产物）；记录经验证的 Python 版本。

### M02｜搭建 FastAPI 与统一错误响应

- **Goal：** 实现平台 API 启动、`/health`、请求 ID、统一成功/失败响应结构。
- **Context：** 依赖 M01 的 `platform-api/`；技术文档第 5 节约定 `request_id + data/error`，错误含 `code/message/retryable`。
- **Constraints：** 只处理 API 骨架；不得执行生成代码、调用模型或绑定 Runner；不在错误堆栈中暴露环境变量。
- **WhenDone：** 健康接口返回可解析数据；成功与错误响应均携带 request_id；至少有一例 HTTP 自动化测试且实际通过。

### M03｜建立 SQLite 数据访问与迁移

- **Goal：** 落地平台 `project` 与 `spec_version` 的最小 SQLAlchemy 模型、事务会话和数据库迁移机制。
- **Context：** 依赖 M02；技术文档第 4 节定义 `project(owner_id...)`、`spec_version(project_id/version/body_json/hash...)`；后续 M05、M07 将依赖它们。
- **Constraints：** SQLite 开启 WAL；短事务；平台元数据与生成项目 MySQL 严格区分；不可把大段源码或日志塞入 SQLite。
- **WhenDone：** 一条数据库迁移可在空 SQLite 文件上执行；重启保留数据；项目与规格版本唯一约束经测试有效。

### M04｜预置账号、会话与项目归属

- **Goal：** 实现平台内测预置账号登录、HttpOnly session、CSRF 防护及项目归属检查依赖。
- **Context：** 依赖 M02/M03；需求文档第 4 节规定不做注册/找回；技术文档第 5 节要求 owner 检查并区分平台账号和生成项目账号。
- **Constraints：** 不引入注册、短信或第三方登录；禁止明文存储密码；在状态变更接口检查 CSRF；不能依靠前端隐藏按钮替代后端授权。
- **WhenDone：** 已登录用户可访问自己的资源，另一预置账号跨用户访问遭拒；无效会话与缺少 CSRF 的变更请求遭拒；安全测试通过。

### M05｜创建项目与保存自然语言需求草稿

- **Goal：** 实现 `POST /api/projects` 和当前用户项目列表/详情读取，并保存原始需求文本。
- **Context：** 依赖 M03/M04；需求文档 FR01：项目名 1—60 字、初始需求 20—5000 字；下步 M06 将把需求转为规格草稿。
- **Constraints：** 仅创建/读取草稿；校验长度和所有权；不在创建项目时直接生成代码；不凭 AI 猜测增加功能。
- **WhenDone：** 合法输入落库；长度越界、未登录或跨用户访问返回明确错误；接口测试通过。

### M06｜定义版本化 Software Specification Schema

- **Goal：** 用 Pydantic 定义 `SoftwareSpecification` 及 `Entity/Field/Permission/BusinessRule/AcceptanceCase` 的最小可扩展结构。
- **Context：** 依赖 M05；技术文档第 4 节要求 `schema_version/project_type/goal/actors/entities/relations/permissions/business_rules/acceptance_cases/technical_profile/constraints/exclusions/capability_requests/recipe_requests/integrations/external_requirements`；后续按此 schema 生成。
- **Constraints：** MVP 可让暂不使用的字段为空数组，但不得删去契约字段；对需求来源标注 `user_explicit/user_confirmed/pending`；只支持 1 个业务实体、固定任务管理 Profile；不接受超范围输入后静默裁剪。
- **WhenDone：** 已有一份可通过校验的 `task-management.spec.json` 样例；缺实体、权限、关键规则或非法 Profile 的输入不能被错误地判为“可确认”；schema 单元测试通过。

### M07｜冻结规格版本与确认 API

- **Goal：** 增加 `GET/PUT /api/projects/{id}/spec` 和 `POST /api/projects/{id}/confirm`，实现规格的编辑、确认和不可变冻结。
- **Context：** 依赖 M03—M06；需求文档 FR03 要求确认版本与 hash；技术文档第 5 节要求乐观锁、冲突 409；M20 的运行入口必须只接收冻结版本。
- **Constraints：** 关键 `pending` 不为零则不许确认；编辑已确认需求要创建新草稿/新版本，不能原地修改；相同内容的 hash 计算必须稳定。
- **WhenDone：** 正常草稿可确认并生成 `spec_id/hash/confirmed_at`；并发旧版本 PUT 返回 409；确认后修改被拒或生成新版本；回归测试通过。

---

## 阶段 B｜从固定模板到单实体可运行源码

### M08｜锁定第一份 Platform Profile

- **Goal：** 定义并锁定第一个 Profile：生成前端 TS、后端 MyBatis-Plus，记录 Java/Node/MySQL/Maven 版本与 Runner 镜像引用。
- **Context：** 依赖 M01；技术文档第 2 节“版本与模板管理”；M09 的基础模板、M14 的 asset-lock 都以它为兼容性依据。
- **Constraints：** 版本须通过本地工具和构建验证后写入；Profile 一经发布不可原地覆盖；只启用这一组合，不提前实现其余三组合。
- **WhenDone：** `profiles/` 有可机器解析、可校验版本号的 Profile；不存在不受控依赖范围；记录兼容性和未验证项。

### M09｜制作能够独立启动的全栈基础模板

- **Goal：** 在 `templates/` 落地 Spring Boot + MyBatis-Plus + MySQL 与 Vue 3 + TS 的最小可运行工程，提供固定登录/异常处理/基础页面。
- **Context：** 依赖 M08；技术文档第 6 节要求后端 `controller/service/mapper/entity/dto/vo/config/exception/security` 等结构；模板应声明自身已提供的认证、路由等能力。
- **Constraints：** 本步不做“按用户需求自动生成”；生成项目与平台登录体系完全独立；样例凭据必须用部署环境安全注入，勿硬编码真实秘密；固定认证逻辑要经过测试。
- **WhenDone：** 在受控开发环境中，基础模板的 `mvn test/package`、`npm ci/build` 实际通过；启动后能访问健康检查并完成模板登录；提交依赖锁及操作记录。

### M10｜先冻结数据库与 REST API 业务契约

- **Goal：** 将已确认 `SoftwareSpecification` 编译为可机器校验的数据库表/字段/约束、REST 路由、DTO、权限矩阵与错误码契约，并生成 `contract_hash`。
- **Context：** 依赖 M06/M07 的冻结 spec、M09 的认证模板接口；技术文档第 6 节明确“先定契约，再生成业务实现”，后续 M11/M12 能力包必须以同一契约渲染。
- **Constraints：** 不在本步骤生成 CRUD 业务代码；契约只允许通过经验证的规则/模板转换产生，命名冲突或不受支持的字段类型必须拒绝；模型不能修改冻结权限和验收要求。
- **WhenDone：** `schemas/` 下有 Task 的数据库/API/权限契约及版本/hash；正常规格转换稳定，冲突规格有可解释错误；后续业务包可以直接消费该契约。

### M11｜实现后端单实体 CRUD 能力包

- **Goal：** 为 `Task` 实体制作可参数化的后端 CRUD、分页、简单搜索与用户数据归属能力包。
- **Context：** 依赖 M06、M09、M10；放入 `capability-packs/`；复用模板提供的 JWT / RBAC、异常体系，不重复实现认证；技术文档第 2、6 节的“能力包”概念是依据。
- **Constraints：** 参数化字段和命名只能从经校验的 spec/contract 获取；成员访问其他成员的数据必须在服务端拒绝；不接受模型输出的 SQL 原样执行。
- **WhenDone：** 使用固定 Task 参数可生成并编译后端；成员 A 读取/修改 B 任务的测试失败于授权层；分页总数字段和搜索行为有独立测试。

### M12｜实现前端单实体 CRUD 能力包

- **Goal：** 生成 Task 列表、查询、表单和 API 请求模块，接入模板的登录状态与路由。
- **Context：** 依赖 M09/M10/M11 的接口契约；`capability-packs/` 保存前端模版；技术文档第 6 节要求 `api/views/components/router/stores/types` 等模块。
- **Constraints：** 使用选定 TS Profile，不生成 JS 变体；前端字段和接口来自冻结契约；禁止渲染模型返回的未净化 HTML；不得在页面中直接设置服务端秘密。
- **WhenDone：** 样例 Task 页面构建通过；登录后能完成一次新增与查询；未授权接口错误可在页面识别，而非空白失败。

### M13｜定义资产 manifest 与最小 Registry

- **Goal：** 为基础模板和两个 CRUD 能力包建立 manifest，并实现只读本地资产检索。
- **Context：** 依赖 M08—M12；技术文档第 2、3 节定义 `id/version/kind/compatible_profiles/requires/provides/conflicts/parameter_schema/extension_points/owned_paths/checks/checksum/license`。
- **Constraints：** 所有字段保留，暂时不需要复杂后台资产市场；资产内容只读；模板提供的能力登记于 `provided_capabilities`，避免重复注入认证。
- **WhenDone：** 注册表可按 ID + 版本定位资产及校验 checksum；缺失资产或不兼容 Profile 返回结构化错误；有单元测试。

### M14｜实现最小 Asset Resolver 与锁清单

- **Goal：** 为 Task 生成链路选择模板和能力包，在写文件前检测重复能力、路径冲突与依赖关系，并输出 `asset-lock.json`。
- **Context：** 依赖 M13；技术文档第 3 节要求先解析、校验，再写入；M15 会消费解析后的只读资产引用。
- **Constraints：** 不让 LLM 直接指定任意磁盘路径、包地址和依赖版本；不使用“有冲突就覆盖”的策略；输出包含资产版本及 hash。
- **WhenDone：** 正常 Task 计划得到稳定锁清单；构造的重复能力/路径冲突被拒；相同输入重复解析锁清单一致。

### M15｜编写确定性代码装配器

- **Goal：** 实现 `spec + contract + asset-lock → run 独立目录的完整源码`；基础工程 COPY，业务文件通过 Jinja2 或结构化渲染生成。
- **Context：** 依赖 M06/M09—M14；技术文档第 3、6 节规定复制、渲染、受限变更，禁止无边界源码字符串替换；M16 将交付装配产物给 Runner。
- **Constraints：** 限制安全路径、文件数量/大小、非法文件名与符号链接逃逸；写入暂存目录并核对哈希后提交；生成器不得改原版模板或固定测试文件。
- **WhenDone：** 使用手工确认 Task spec 可在独立目录生成前后端、migration、README 初稿和 lock；两次独立生成结构一致、无文件覆盖；路径越界测试通过。

---

## 阶段 C｜真实沙箱、验证与错误报告

### M16｜创建独立受控 Runner 服务与隔离契约

- **Goal：** 创建 Runner 的内部认证 API、固定任务白名单与执行容器约束，供后续实际构建/启动使用。
- **Context：** 依赖 M15 的待测产物；技术文档第 7 节要求专用 Linux 主机或 VM、非 root、受控网络、资源/时间限制、清理；平台 API 不接触 Docker Engine。
- **Constraints：** 不暴露任意 shell；不挂载宿主 Docker socket、平台数据库、模型 Key 或宿主敏感目录；配置 `no-new-privileges`、`cap-drop`、seccomp 和资源限制；Docker 不等于公众级安全隔离。
- **WhenDone：** 未认证 Runner 请求被拒；越界路径与非白名单任务被拒；Runner 能启动和清理一个受限测试容器；真实主机配置/未落实项被记录。

### M17｜实现静态与前后端构建验证

- **Goal：** Runner 接收装配产物，执行静态清单检查、`mvn test/package`、`npm ci/build`，生成结构化结果。
- **Context：** 依赖 M09、M15、M16；技术文档第 7 节的检查层和 PASS/FAIL/NOT_RUN 定义；注意 TS 类型检查须执行模板配置的真实命令。
- **Constraints：** 仅运行固定任务；时间/内存/进程及依赖代理受控；缺少命令、依赖离线或没有执行测试一律不能算 PASS；输出日志脱敏并截断。
- **WhenDone：** 正常 Task 工程的构建检查真正通过；注入 Java 编译错误后返回 `FAIL/BUILD_FAILED`，缺少网络依赖时正确归类基础设施故障。

### M18｜实现全新 MySQL 初始化与启动/HTTP 验收

- **Goal：** Runner 在隔离测试环境内应用 migration、启动前后端，执行登录、CRUD、分页、搜索、数据归属固定 HTTP 用例。
- **Context：** 依赖 M11/M12/M16/M17；需求文档第 8 节要求“编译成功≠功能通过”；技术文档第 7 节规定数据库、健康检查及业务验收分层。
- **Constraints：** 每个 run 使用隔离数据库与演示数据；验收断言在源码生成前由平台固定，不允许装配器修改；容器端口/网络按白名单开放。
- **WhenDone：** 全新环境可完成完整 Task HTTP 场景；故意移除数据权限后验收失败，且不被“编译成功”覆盖；超时与容器退出均形成可复现报告。

---

## 阶段 D｜异步运行、AI 结构化理解和最小编排

### M19｜建立 run/task/event 元数据与最小运行状态机

- **Goal：** 在 SQLite 添加 `run/task/event/artifact` 记录，支持 `QUEUED → PLANNING → GENERATING → VERIFYING → SUCCEEDED/FAILED`，外加实际错误码。
- **Context：** 依赖 M03/M07；技术文档第 3—4 节定义运行状态与 run_id、事件 sequence、artifact sha256；M20、M23 会依赖。
- **Constraints：** 状态只能通过白名单迁移；每个 run 的目录隔离；状态是数据库权威值，不能以 UI 时间进度冒充；完成必须经 M17/M18 全部 MVP 必需检查。
- **WhenDone：** 迁移后可创建 run/task 和单调递增事件；非法状态转移及另一用户读取 run 被拒；状态测试通过。

### M20｜实现单 worker 任务领取与幂等运行 API

- **Goal：** 实现 `POST /api/projects/{id}/runs` 和后台**独立 worker** 的事务领取、租约、运行幂等键及 `GET /api/runs/{id}`。
- **Context：** 依赖 M07/M19；技术文档第 1、5、9 节规定单 worker、租约、Idempotency-Key、`202`，不能用 FastAPI BackgroundTasks 承载长期构建。
- **Constraints：** 仅确认规格可运行；重复点击生成返回同一个运行记录；worker 不与 API 进程绑定；初期不承诺完整中断恢复，异常必须落 `FAILED/UNKNOWN` 而非假成功。
- **WhenDone：** API 可提交任务并轮询状态；同一幂等键重复请求不新建 run；关闭 API 不应使独立 worker 立即丢失已持久化任务；有集成测试。

### M21｜实现模型调用计费前置护栏（最小版）

- **Goal：** 引入 `model_call` 账本、模型角色 → Provider 静态路由、最大 Token/次数/预算预留；MVP 只实现 `user_interaction → deepseek_official/deepseek-flash`。
- **Context：** 依赖 M19/M20；技术文档第 8 节统一模型网关/账本，结合本版“模型 API 路由约定”；M22 将在**创建 run 之前的需求草稿阶段**调用 DeepSeek。
- **Constraints：** `model_call` 同时支持 `project_id + draft_id` 的生成前访谈用量和后续 `run_id` 的生成期用量（不能把 M22 的花费虚报成 0）；使用整数微单位与实际 `price_version`，不得硬编码价格；缺 usage / 账单不确定则保留预留并等待对账；Provider 密钥只从环境注入；Qwen 路由在本步仅文档占位，不实例化。
- **WhenDone：** 测试路由 `user_interaction` 必须选择 DeepSeek `deepseek-flash`、任意未启用角色必须拒绝且没有外部请求；预算不足时客户端**未被调用**；同 call_id 结算幂等；有 usage/无 usage/超额与“生成前草稿计费”测试均通过。

### M22｜LangChain 结构化需求提取（一次调用）

- **Goal：** **只调用 DeepSeek V4.1 Flash（`deepseek-flash`）**，从中文自然语言生成 Pydantic 规格草稿、需求摘要、缺失字段及待确认问题提示，供用户编辑确认，不直接生成源码。
- **Context：** 依赖 M06/M07/M21 的 project/draft 级模型账本与路由；技术文档第 1、4、8 节规定结构化模型接口，但**具体 DeepSeek 型号属于 2026-10-11 选型决策**；M23 只消费用户确认后的 spec。
- **Constraints：** 不调用 `qwen3.7-plus` 或其他模型；仅使用已通过 JSON/usage/模式参数探针的 DeepSeek 官方接口。常规提取非思考，确有复杂度规则时按已验证配置启用低强度思考；不信任返回的库地址/命令；推断必须为 `pending` 或建议；结构错误最多纠正 1 次并单独计费；失败时允许人工编辑草稿，不能伪称 AI 成功。
- **WhenDone：** 中文 Task 需求经真实 `deepseek-flash` 请求获得可校验草稿并可在 API 读取；明确标识 `user_explicit/user_confirmed/pending`，待确认时不可冻结；报告记录 Provider/型号/模式/真实 usage/成本（缺失标 UNKNOWN）；断言没有 Qwen 调用且测试通过。

### M23｜LangGraph 固定生成图接入装配与验证

- **Goal：** 建立最小 LangGraph 生成图：`load_confirmed_spec → resolve_assets → assemble → verify → package_or_fail`，由 worker 驱动。
- **Context：** 依赖 M14/M15/M17—M22；技术文档第 3 节规定外层图管理阶段和终态，数据库是权威状态；需求理解在用户确认前由 DeepSeek 完成，生成图只接收已确认的 `spec_id/spec_hash`。
- **Constraints：** 此步不引入 DeepAgent、Qwen 技术规划、自动修复或并行节点；禁止生成图读取未确认聊天并“重新解释需求”；每个节点入口检查运行有效性，记录 task_id、输入/输出 hash 与阶段结果；未 PASS 不得跳转到成功打包。
- **WhenDone：** 真实 Task run 可经图走到全部验证检查；故意制造模板错误能走到失败分支并留下事件；至少一次图执行集成测试通过。

---

## 阶段 E｜可交付的 UI、ZIP 和验收

### M24｜确定性交付报告与打包

- **Goal：** 为已通过检查的 run 生成源码 ZIP、`verification.json`、`cost-report.json`、`spec.json`、`asset-lock.json` 和 `EXTERNAL_SERVICES.md`。
- **Context：** 依赖 M15/M17/M18/M21/M23；需求文档 FR08 及技术文档第 6、9 节要求 frontend/backend/migration/.env.example/Dockerfile/compose/README/接口说明等。
- **Constraints：** 移除 `.env`、真实密钥、`node_modules`、构建缓存和敏感日志；无第三方服务时指南明确写“无须配置”；`cost-report.json` 如实列出草稿阶段 DeepSeek 用量和生成期用量；`verification.json` 标注 MVP 检查范围、PASS/FAIL/NOT_RUN，失败包必须标“未验证”。
- **WhenDone：** ZIP 可解压；静态文件清单和密钥扫描通过；按 README 在干净目录复现构建/启动；验证报告与实际 Runner 结果一致。

### M25｜平台 UI：创建项目与自然语言输入

- **Goal：** 在 `platform-ui/` 做最小登录、项目列表、项目创建和需求输入页面，接入 M04/M05/M22。
- **Context：** 依赖 M04/M05/M22；技术文档第 1、5 节建议 Vue 3 + TS + Vite + Element Plus；需求文档 FR01 对非技术用户的业务语言要求。
- **Constraints：** 此步只做项目/需求输入，不同时搭完整工作台；所有模型文本按普通文本或安全 Markdown 展示；处理 401/403/422 和加载失败。
- **WhenDone：** 登录后可创建 Task 项目并经 `user_interaction` 路由触发 **DeepSeek V4.1 Flash** 结构化草稿生成；UI 不出现来自其他模型的用户交互文本；刷新后项目仍在；前端构建与最少一条界面测试通过。

### M26｜平台 UI：规格编辑、显式确认与运行按钮

- **Goal：** 展示并编辑结构化摘要、业务规则与权限，提供确认后才启用的“开始生成”按钮。
- **Context：** 依赖 M07/M22/M25；需求文档 FR03 要求用户看清功能、实体、角色、验收场景和排除项；M20 是运行提交接口。
- **Constraints：** DeepSeek 的任何推断不可直接视为用户确认；固定 Profile 必须明显标注且暂不可切换；重要 pending 未处理时禁用确认；后端仍要独立校验，确认后生成不可变 spec/hash；不能让后续 Qwen 根据原始对话覆盖其业务含义。
- **WhenDone：** 未确认规格无法生成；编辑—确认—生成可走通；状态冲突/过期版本可以显示并重新读取；前端测试通过。

### M27｜平台 UI：运行阶段、失败与下载

- **Goal：** 实现轮询式运行页，显示当前阶段、已完成任务数、最新事件、调用/预算概要、失败详情和授权下载链接。
- **Context：** 依赖 M19/M20/M24/M26；技术文档第 5 节建议两秒增量轮询，并使用 `GET /api/runs/{id}/events?after_sequence=...`；需求文档 FR07/FR08 要求状态真实。
- **Constraints：** 不显示虚构“进度百分比”；事件返回脱敏数据；下载先校验 owner，不接受用户任意文件路径；成本信息按已确认/预留区分。
- **WhenDone：** 页面刷新后仍可查看 run；成功展示 ZIP 和报告，失败展示错误及 NOT_RUN 项；另一账号的下载请求被拒绝。

### M28｜MVP 冷启动端到端验收与交接

- **Goal：** 在一套全新受控环境中，把 M01—M27 串成一次“输入 → 草稿 → 确认 → 生成 → 验证 → 下载 → 重启”的演示。
- **Context：** 依赖全部先前步骤；需求文档第 3、8 节定义编译、数据库、启动、HTTP 业务场景均必须通过；技术文档第 9 节强调真实多次评估及失败可见性。
- **Constraints：** 不允许人工修改生成源码再把结果计为成功；测试账号与密码经安全配置注入；缺少 Runner 或真实 Provider 连接就如实记录 NOT_RUN，不录制模拟“全绿”结果。
- **WhenDone：** 至少一套 Task 项目在清洁环境中通过“**DeepSeek `deepseek-flash` → 结构化草稿 → 用户确认冻结 → 不调用 Qwen 的模板装配 → Runner 验证**”的真实链路；源码可解压并按 README 启动；保存一例故意失败样例及真实报告；`docs/MVP_ACCEPTANCE.md` 列出模型路由、Provider/型号/模式、实际调用与成本，并明确“已完成/未完成/下一阶段入口”。

---

## MVP 发布判据（不可跳过）

1. **生成链路不是假流程：** 自然语言确实经 **DeepSeek V4.1 Flash（`deepseek-flash`）** 结构化提取，用户确实确认冻结 spec；源码按锁定资产装配的真实工程。MVP 不应出现未启用的 Qwen 调用。
2. **所有 MVP 必需检查真实 PASS：** 静态清单、后端测试与构建、前端安装与构建、全新 MySQL migration、健康启动、认证/CRUD/分页/搜索/数据归属 HTTP 验收，以及 ZIP 冷启动复核。
3. **执行边界成立：** Platform API 无直接 Docker 权限、Runner 使用隔离环境、跨用户授权拒绝、跨 run 文件隔离、密钥不入 ZIP；缺任一项则不得声称安全交付。
4. **失败与花费如实披露：** 模型调用前预算拦截有效，实际 usage/未知账单区分，异常不伪装成功；所有未实施的完整 Demo P0 测试列为缺口。
5. **交付物：** 源码 ZIP + 规格、资产锁、运行说明、接口说明、验证报告、费用报告、无外部服务指南，以及 MVP 验收记录。

## 与第二份文件的衔接

- 第一份完成后，应有可运行的**确定性装配骨架**。先冻结一次可回滚的代码基线（例如 `mvp-verified` 标签），再开始第二份文档的增量开发。
- 第二份首先补齐 **Demo P0 合规**：DeepSeek 多轮问答、Qwen 技术规划、四种 Profile、2—3 实体与状态规则、Qwen3-Coder-Flash 驱动的 DeepAgent 受限 patch、有限修复、取消恢复、9 次评估；第三方 Integration Pack 与在线预览属于后续扩展。
- 每一增量都必须回跑 MVP 的冷启动回归，不得以“增加功能”为由破坏 Task 基线。

## 文档出处索引

- 《Demo需求分析文档》v0.1：第 2—3 节交付边界与成功标准；第 4—7 节 FR01—FR09；第 8 节固定验收与 9 次评估；第 9 节阶段与后续方向。
- 《Demo技术文档_修订版》v0.3：第 1—2 节 Python 架构/资产和 Profile；第 3—5 节状态/规格/API；第 6 节模板与 DeepAgent 限制；第 7 节 Runner 和验证；第 8 节账本；第 9 节实施建议与测试。

**使用建议：** 每次只把“总控 Prompt + 模型路由约定 + 当前一个 Mxx 步骤”交给编码 AI。WhenDone 不满足时，先在本步骤修复或记录外部阻塞，不进入 Mxx+1。
