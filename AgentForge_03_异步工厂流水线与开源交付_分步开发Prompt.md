# AgentForge｜文档三：异步工厂流水线与开源交付（FP01—FP10）｜分步开发 Prompt

> **文档性质：** 面向 AI 编码助手的**新架构增量实施流程**。本文件在《文档一》基线与《文档二（A01—A09）》底座改造之上，落地完整的 **“后端先行 → 多部门异步并行小循环 → 总装总验归因打回大循环 → 后端权威接口驱动前端并行 → 全栈强制 Docker Runner 验收”** 多循环最小职责工厂流水线，并以“电商模拟沙箱”完成开源端到端验证。
>
> **前置依赖：** 必须完成《文档二》`A01—A09` 并保持原有基线测试全绿。
>
> **核心架构约定（已锁定）：**
> - **默认开关保护：** `AGENTFORGE_FACTORY_PIPELINE_ENABLED=false` 默认关闭；关闭时旧串行 `generation_graph.py` 行为 100% 不变。
> - **规模与外部边界：** 默认模块软上限 **≤ 8**（`AGENTFORGE_FACTORY_MAX_MODULES=8`）；支付等第三方能力一律采用 **Port + Adapter + 本地沙箱/Mock 模拟**。
> - **资产与存储：** 本地 PostgreSQL 存元数据 + `asset_release`/`asset_file` 逐文件表 + 只读物化缓存；子 Agent 可自主决定“查 PG 模板/方法库复用”或“在私有目录直接编写”。
> - **分层沙箱：** 部门内小循环允许本机开发态用轻量沙箱/受限进程快跑单模块编译与单测；**后端总验收与全栈总验收必须强制使用独立 Docker Runner**。
> - **模型路由：** 全角色支持用户通过 `config/models.json` 自定义 OpenAI 兼容模型。

---

## 0. 依赖图与总控 Prompt

### 0.1 实施依赖图

```text
文档二（A01—A09 全部完成）
  └─ FP01 工厂运行数据模型、持久化状态机与取消/恢复语义
       └─ FP02 后端项目总管（ChiefArchitect）：模块拆分、SQL 号段与跨模块契约冻结
            └─ FP03 只读后端公共底座装配与跨模块 api/*Facade.java 骨架自动生成
                 └─ FP04 部门三角色最小职责引擎（DeptLead + DeptCoder + DeptTester）与 DefectTicket
                      └─ FP05 后端多部门异步并行调度池（asyncio + 双信号量 + 小循环自愈 + 契约变更升级）
                           └─ FP06 后端总装、权威 openapi.json 聚合与总验收归因打回大循环（强制 Docker Runner）
                                └─ FP07 前端项目总管、只读前端底座与基于 openapi.json 的 TS 契约类型生成
                                     └─ FP08 前端多部门异步并行流水线与声明式路由自动聚合
                                          └─ FP09 全栈总装、Docker Runner 端到端验收与工厂交付包打包
                                               └─ FP10 平台 UI 多部门泳道进度页与「电商沙箱」开源端到端标杆验收
```

### 0.2 总控 Prompt（每一步开始前附上）

```text
你是 AgentForge 异步工厂流水线阶段的增量实施型编码助手。本次只执行指定的一个 FPxx 步骤，严禁擅自提前实现后续步骤。

【执行铁律】
1. 先核查 git status、前置步骤 docs/FP*.md 实施记录与仓库真实模块签名，再编码。
2. 禁止修改既有串行 generation_graph.py 的默认执行逻辑（除非在顶层通过显式 feature flag 分发）；禁止修改或放宽既有测试断言与已发布公共资产原件。
3. 严守最小职责与安全红线：
   - 项目总管与部门副总管**不直接写业务源码**；
   - 编码子 Agent 只能写 `staging/<phase>/<dept_id>/` 下的 `owned_paths`，无 shell、无外网、不可修改受保护底座/Facade 骨架/测试文件；
   - 测试 Agent 只能写 `test_paths` 并驱动真实编译/单测工具，**禁止修改业务源码**，**禁止仅凭模型口头审查放行**；
   - 后端总验收与全栈总验收必须强制调用独立 Docker Runner，不可降级为本地子进程。
4. 所有模型调用必须经 A04 自定义路由与 A07 PG 事务账本（绑定 run_id / phase / dept_id / round / call_id）；无真实模型凭据或无运行中 Docker Runner 时，集成实测用例明确标 NOT_RUN，禁止伪造 PASS。
5. 本步完成后，必须运行 `./scripts/ci_gate.sh` 与本步新增测试，编写 `docs/FPxx_<简名>记录.md`（套用文档一 §4 模板），并独立提交 git（如 `feat(fp01): ...`）。
```

---

## 1. 分步详细实施流程（FP01—FP10）

### FP01｜工厂运行数据模型、持久化状态机与取消/恢复语义

- **接口选用：**
  - **复用：** `models.py`（`Run`, `RunEvent`, `Base`）、`db.py`（`session_scope`, `lock_row`）、`runs.py`（状态迁移与取消标记）、`generation_graph.py`（入口分发点）
  - **新增迁移：** `migrations/versions/0018_factory_pipeline_tables.py`
  - **新增模型与模块：**
    - `models.py` 新增 `FactoryBlueprint`（`run_id, phase, version, blueprint_json, blueprint_hash, status`）、`DepartmentRun`（`run_id, phase, dept_id, status, current_round, max_rounds, staging_dir, api_fragment_json, evidence_json, error_code, started_at, finished_at`）、`DefectTicketRow`（`run_id, phase, dept_id, scope, round, error_class, payload_json, resolved`）
    - `factory_state.py`（`FactoryPhase = "backend" | "frontend"`、`DeptStatus = "QUEUED" | "PLANNING" | "CODING" | "TESTING" | "PASSED" | "REWORK" | "FAILED" | "CANCELLED"`、`FactoryStateStore`：原子更新部门状态、记录事件、检查取消、崩溃后扫描已 `PASSED` 部门跳过重跑）
    - `factory_pipeline.py`（`factory_pipeline_enabled() -> bool`、顶层编排器骨架 `run_factory_pipeline(context, run_id)`）
- **Goal：** 在本地 PostgreSQL 建立工厂流水线的持久化状态真值表与可恢复状态机，确保多部门异步执行过程中支持随时取消、断点续跑（已通过的部门不重做）和全链路可观测。
- **Context：**
  - 现有 `task` 表只面向线性任务，无法完整表达“阶段（backend/frontend）× 部门 × 内部小循环轮次 × 缺陷单”的层级状态；独立建表可做到对旧 `RunTask` 零侵入。
  - 与原有 `Run.cancel_requested` 共享同一取消信号：每个部门小循环边界与总装大循环边界统一轮询取消状态。
- **Constraints：**
  - `AGENTFORGE_FACTORY_PIPELINE_ENABLED` 默认为 `false`；关闭时 `worker.py` / `generation_graph.py` 完全不触发 `factory_pipeline`；
  - 部门状态迁移必须走显式白名单（例如 `PASSED` 仅在总装归因打回时可迁回 `REWORK`，不可被外部非法覆盖）；
  - 不修改现有 `Run` / `RunTask` 表既有字段与约束；完成后独立提交 git（`feat(fp01): ...`）。
- **WhenDone：**
  - 迁移 `0018` 在 SQLite 与本地 PG 均可升级/回滚；
  - `FactoryStateStore` 支持多部门并发更新状态无冲突、取消信号即时生效、模拟进程崩溃重启后自动跳过已 `PASSED` 部门仅重跑未完成部门；
  - 新增 `tests/test_fp01_factory_state.py` 全绿；`./scripts/ci_gate.sh` 全绿；记录写入 `docs/FP01_工厂状态机与数据模型记录.md` 并独立 git commit。

---

### FP02｜后端项目总管（`ChiefArchitect`）：模块拆分、SQL 号段与跨模块契约冻结

- **接口选用：**
  - **复用：** `spec.py`（`SoftwareSpecification`）、`scope_policy.py`（`validate_scope_for_mode`、`max_modules=8`）、`blueprint_contracts.py`（`ArchitectureBlueprint`、`validate_blueprint`）、`model_gateway.py` & `production_routing.py`（`chief_architect` 角色）、`budget_pg.py`（`PgBudgetLedger`）、`factory_state.py`
  - **新增模块：** `factory_chief.py`（`ChiefArchitect`: `plan_backend_blueprint(spec, profile, run_id) -> ArchitectureBlueprint`、`triage_integration_failures(...)` 占位接口）
- **Goal：** 实现后端项目总管角色：接收冻结 `spec`（如电商 5 模块需求），调用用户配置的强推理模型（`chief_architect`）产出结构化 `ArchitectureBlueprint`，经 Python 5 项硬校验通过后在 PG 中冻结为 `APPROVED_FOR_BUILD`。
- **Context：**
  - 项目总管“不需要关心模块内部怎么写代码”，但**必须**负责三件事：① 划分业务部门（≤ 8 个）及每个部门独占包路径与 SQL 号段（如 `100–199`、`200–299`…）；② 定义所有跨模块 `JAVA_METHOD`（`api/*Facade`）和对外 `HTTP` 契约并冻结；③ 声明第三方能力（如支付）必须通过 `Port + Adapter + Mock` 沙箱接入。
  - 若模型首次输出的蓝图未通过 `validate_blueprint()` 的 5 项硬校验（如号段重叠、依赖环、漏覆盖需求、模糊签名），把确定性校验错误列表回喂给总管纠正，**最多重试 2 轮**，超限即终止报 `BLUEPRINT_INVALID`。
- **Constraints：**
  - 总管**零文件写入权限**，只输出严格 Pydantic Schema 校验后的 JSON；
  - 每次调用（含纠正重试）均走 `PgBudgetLedger` 记账，标明 `phase="backend", dept_id="chief"`；
  - 对明显超 8 模块上限或要求真实生产支付扣款的蓝图必须由程序硬拒绝；
  - 完成后独立提交 git（`feat(fp02): ...`）。
- **WhenDone：**
  - 给定“电商项目”冻结规格（登录注册、首页展示、加入购物车、创建订单、模拟购买支付），使用脚本化模型（及可选真实模型）能产出合法 `ArchitectureBlueprint`，经 `validate_blueprint` 通过并持久化入 `factory_blueprint` 表；
  - 覆盖 3 类负例测试：首次蓝图号段冲突→第 2 轮纠正成功；连续 2 轮非法→报 `BLUEPRINT_INVALID` 停止；超过 8 模块→被拒绝；
  - 新增 `tests/test_fp02_chief_architect.py` 全绿；`./scripts/ci_gate.sh` 全绿；记录写入 `docs/FP02_后端项目总管与蓝图冻结记录.md` 并独立 git commit。

---

### FP03｜只读后端公共底座装配与跨模块 `api/*Facade.java` 骨架自动生成

- **接口选用：**
  - **复用：** `asset_cache.py`（`AssetMaterializer` 从本地 PG 物化基础模板）、`assembler.py`（模板 COPY 与安全路径校验）、`blueprint_contracts.py`（`generate_java_facade_stubs()`）、`agent_files.py`（`PROTECTED_PREFIXES`）
  - **新增模块：** `factory_base_builder.py`（`build_backend_base(run_dir, blueprint, lock, materializer) -> BaseBuildResult`，产出只读底座目录 `run_dir/backend_base/`、共享 `pom.xml`/`common/`/`security/`/`V001__base.sql`、以及所有部门的只读 `modules/<dept_id>/api/*Facade.java` 与公共 DTO 骨架）
- **Goal：** 在各后端部门开工前，由 Python 程序确定性搭建好完整的只读后端骨架和跨模块 Java 门面接口存根（Stub），使所有部门（哪怕订单部依赖商品部）都能在第 0 秒基于同一份编译期接口同时并行开工。
- **Context：**
  - 底座代码来自本地 PG 物化的已认证 Spring Boot 模板，保证 `pom.xml` 依赖锁、统一响应体、全局异常处理、JWT 安全基座和 Flyway 配置 100% 一致。
  - `generate_java_facade_stubs()` 根据冻结蓝图的 `frozen_contracts` 生成各模块 `api/` 下的 Java 接口与参数/返回 DTO，所有部门对 `backend_base/` 与他部门 `api/` 仅拥有只读视图。
- **Constraints：**
  - 全程确定性执行，**0 模型调用**；
  - 生成的底座目录与 `api/*Facade.java` 必须写入只读清单并登记 SHA-256，总装时若发现任何部门篡改了底座或他部门 `api/` 签名，直接拦截报错；
  - 不修改原有 `assembler.py` 对旧 Demo 的装配逻辑；完成后独立提交 git（`feat(fp03): ...`）。
- **WhenDone：**
  - 针对 5 部门电商蓝图，能确定性生成包含只读底座 + 5 份 `modules/<dept>/api/*Facade.java` 的完整骨架目录并产出文件哈希清单；
  - 在本机存在 Maven/JDK 环境下，纯骨架目录可直接通过 `mvn test-compile`（验证生成的 Stub 语法与包名 100% 合法；无 Maven 环境标 `NOT_RUN`）；
  - 新增 `tests/test_fp03_backend_base_builder.py` 全绿；`./scripts/ci_gate.sh` 全绿；记录写入 `docs/FP03_后端只读底座与Facade骨架记录.md` 并独立 git commit。

---

### FP04｜部门三角色最小职责引擎（`DeptLead` + `DeptCoder` + `DeptTester`）与 `DefectTicket`

- **接口选用：**
  - **复用：** `patch_engine.py`（`PatchEngine`, `StagingBackend`, `LedgerChatModel`, `audit_tool_table`）、`agent_files.py`（`FileBackend`, `FileChangeSet`）、`agent_asset_tools.py`（`search_assets`, `read_asset_file`, `apply_asset_to_staging`）、`module_verifier.py`（`ModuleVerifier`: 轻量本地进程或 Runner `/verify-module`）、`production_routing.py`（`dept_lead` / `dept_coder` / `dept_tester` 角色路由）
  - **新增模块：**
    - `factory_tickets.py`（`DepartmentPlan`, `FixDirective`, `DefectTicket`, `TestEvidence`, `DepartmentDeliverable` 严格 Pydantic 契约）
    - `factory_department.py`（`DepartmentRunner`：串联 `DeptLead` 规划/修复拆解 → `DeptCoder` 在 `staging/<phase>/<dept_id>/` 写业务代码与 `api-fragment.json` → `DeptTester` 在 `test_paths` 写单测/Mock 契约测试并触发 `ModuleVerifier` 真跑编译与测试 → 失败生成脱敏 `DefectTicket` 打回 `DeptLead`）
- **Goal：** 实现单个功能部门内部完整的最小职责执行引擎：副总管定方案与接缺陷单、编码子 Agent 自主选择查 PG 资产库复用或直接编写、测试 Agent 编写测试并驱动真实工具校验，严防职责越界。
- **Context：**
  - **严格读写隔离**：
    - `DeptLead`：只读，产出 `DepartmentPlan`（首次）或 `FixDirective`（返工）；
    - `DeptCoder`：仅可写本部门 `owned_paths`（如 `modules/order/{controller,service,mapper,entity,dto}`、`V400__*.sql`）与 `api-fragment.json`；**禁止写 `test_paths`、底座、`api/*Facade.java` 及他部门路径**；可用 `search_assets` / `read_asset_file` / `apply_asset_to_staging` 自由决定是否复用模板；
    - `DeptTester`：仅可写本部门 `test_paths`（如 `src/test/.../modules/order/**`），**禁止修改业务源码**；必须调用 `ModuleVerifier` 实际执行编译与单测，测试是否 PASS 完全由工具退出码和解析后的测试报告决定。
  - 单模块测试时，业务代码依赖的外部 `Facade`（如订单部依赖 `ProductFacade`）由 `DeptTester` 使用 Mockito 等方式按冻结契约 Mock，从而保证本部门完全独立完成编译和单元测试。
- **Constraints：**
  - 启动 `DeptCoder` 与 `DeptTester` 前均强制调用 `audit_tool_table()`，确认无 `execute`（shell）与 `task`（无控子代理）；
  - 传递给 `DeptLead` 的 `DefectTicket.log_excerpt` 必须经过脱敏与长度截断（≤ 4 KB）；
  - 模型写出的代码严禁自动写入 PG 公共资产表 `asset_release`；
  - 完成后独立提交 git（`feat(fp04): ...`）。
- **WhenDone：**
  - 用脚本化引擎跑通单个部门（如 `order` 部门）两类场景：① 首次通过并产出 `FileChangeSet` + `api-fragment.json` + `TestEvidence`；② 第 1 轮单测故意失败 → 生成 `DefectTicket` → 打回 `DeptLead` 出 `FixDirective` → `DeptCoder` 局部修正 → 第 2 轮通过；
  - 负例全部拦截：`DeptCoder` 企图修改 `test_paths` 或他部门目录报 `403 FORBIDDEN`；`DeptTester` 企图修改业务源码报 `403 FORBIDDEN`；测试工具未真正执行或返回非零退出码时拒绝标 `PASSED`；
  - 新增 `tests/test_fp04_department_engine.py` 全绿；`./scripts/ci_gate.sh` 全绿；记录写入 `docs/FP04_部门三角色引擎记录.md` 并独立 git commit。

---

### FP05｜后端多部门异步并行调度池（`asyncio` + 双信号量 + 小循环自愈 + 契约变更升级）

- **接口选用：**
  - **复用：** `factory_department.py`（`DepartmentRunner`）、`factory_state.py`（`FactoryStateStore`）、`budget_pg.py`（`PgBudgetLedger`）、`module_parallel.py`（`ModuleStaging` 冲突校验理念）、`module_coordinator.py`（`ChangeRequest` 契约变更升级协议）
  - **新增模块：** `factory_scheduler.py`（`AsyncDepartmentPool`: 基于 `asyncio.TaskGroup` / `asyncio.gather` + 两个独立 `asyncio.Semaphore`——`llm_dept_semaphore` 默认 3、`verifier_semaphore` 默认 2；管理各部门小循环 `max_dept_rounds=3`、第 3 轮可选模型升级、单部门失败隔离、契约变更请求 `ContractChangeRequest` 暂停与局部重跑）
- **Goal：** 让所有后端部门（如电商的 `auth / product / cart / order / payment` 5 个部门）真正**同时启动异步并行工作**，各跑各的独立暂存区与部门内小循环，互不干扰、不爆本机内存、不双扣共享预算，并支持遇到契约缺口时走正规升级流程。
- **Context：**
  - 因为 FP03 已在开工前生成了所有只读 `Facade Stub`，所有后端部门无需串行等待上游实现完毕即可同时异步开工。
  - 为防止本地机器同时拉起过多 Maven 编译进程卡死，使用**双信号量**：LLM 规划/编码允许默认 3 路并发（可配 `AGENTFORGE_FACTORY_MAX_CONCURRENT_DEPTS`），沙箱单模块测试默认 2 路并发（可配 `AGENTFORGE_FACTORY_MAX_CONCURRENT_VERIFIERS`）。
  - 契约变更升级（每 run ≤ 2 次）：若某部门副总管确需在某外部 `Facade` 上新增一个字段/方法，不能私改，而是发出 `ContractChangeRequest` → 调度器交总管审批并升级 `blueprint.version` → 重新生成 Stub → 仅触发提供方与受影响消费方部门局部重跑。
- **Constraints：**
  - 单部门小循环硬上限 `max_dept_rounds = 3`（可配），超限即标 `FAILED`；
  - 任一部门抛出未预期异常不能导致其他正在运行的部门产生未释放的预算预留或脏暂存文件（必须 `try/finally` 清理与释放预留）；
  - 预算耗尽（`BUDGET_EXCEEDED`）或收到取消信号（`cancel_requested=True`）时，平滑停止派发新一轮任务并记录已完成的部门检查点，支持后续加预算恢复；
  - 完成后独立提交 git（`feat(fp05): ...`）。
- **WhenDone：**
  - 5 个模拟部门同时异步启动，日志时间戳证明真正并发执行且并发度严格不超过信号量上限；
  - 验证 4 类关键场景：① 某部门第 1 轮失败自愈、其余 4 部门不受阻；② 并发抢占预算零双扣；③ 取消信号发出后不再启动新一轮并保留已完成部门检查点；④ 契约变更请求仅触发相关 2 个部门重跑，其余 3 个部门复用已有成果；
  - 新增 `tests/test_fp05_async_pool.py` 全绿；`./scripts/ci_gate.sh` 全绿；记录写入 `docs/FP05_后端异步并发调度池记录.md` 并独立 git commit。

---

### FP06｜后端总装、权威 `openapi.json` 聚合与总验收归因打回大循环（强制 Docker Runner）

- **接口选用：**
  - **复用：** `factory_chief.py`（补全 `triage_integration_failures()`）、`factory_scheduler.py`（定向重跑被点名返工的部门）、`module_integration.py`（跨模块静态一致性门禁）、`runner_client.py`（`HttpRunnerClient.verify()` & `accept()` —— **强制 Docker Runner**）、`packaging.py`（`verification_passed()`）
  - **新增模块：**
    - `factory_assembler.py`（`merge_department_deliverables(base_dir, deliverables, output_dir)`：校验零路径冲突、零底座篡改、SQL 号段合规、自动生成 `V900__relations.sql` 与合并后的权威 `backend/openapi.json`）
    - `factory_backend_stage.py`（串联 `总装 → 强制 Docker Runner 全量构建/MySQL 迁移/跨模块集成验收 → 失败则总管根因归因生成 DefectTicket → 仅定向打回责任部门返工 → 重装复验`，大循环硬上限 `max_integration_rounds = 2`）
- **Goal：** 实现后端工厂的最终合龙——把所有通过小循环的部门代码确定性组装成完整后端工程、汇总生成权威接口文档 `openapi.json`，经**独立 Docker Runner 强制总验收**；不通过时由总管精准归因并只打回出问题的部门修改，直到产出可运行、全绿的完整后端。
- **Context：**
  - 后端总验收必须强制走 Docker Runner（严禁退化为 `LocalProcessModuleVerifier`），覆盖：`mvn package`、全新 `mysql:8.0` 执行 `V001..V899..V900` 全量迁移、应用启动健康检查、跨模块核心业务链路 HTTP 验收与支付沙箱回调测试。
  - 若总验收发现的是 `INFRASTRUCTURE` 错误（Docker 不可达、镜像缺失、超时），立即停止并报 `INFRASTRUCTURE`，**绝不触发大循环打回部门改业务代码**。
  - 总管归因（`triage_integration_failures`）把编译报错/迁移报错/HTTP 失败堆栈精确映射到一个或多个责任 `dept_id`，仅重新激活这些部门的 `REWORK` 状态，未出错的部门产物原地保留。
- **Constraints：**
  - 大循环上限硬编码默认 `max_integration_rounds = 2`（可配收紧），超限即落 `FAILED` 并保留完整失败产物与失败报告；
  - 合并前发现任何两个部门写了同一文件路径、或任一部门改动了底座/`api/*Facade.java` 签名，直接拒绝合并并生成 `CONTRACT_MISMATCH` 缺陷单；
  - 权威 `backend/openapi.json` 必须在后端总验收全 `PASS` 后才可冻结并传给前端流水线；
  - 完成后独立提交 git（`feat(fp06): ...`）。
- **WhenDone：**
  - 故意制造跨模块集成缺陷（如 `cart` 部门 SQL 外键类型与 `product` 部门主键类型不匹配导致全量 MySQL 迁移失败）：总验收拦截 → 总管精准归因仅打回 `cart` 部门（其余 4 部门不重跑）→ 第 2 轮合并后全绿并冻结权威 `openapi.json`；
  - 基础设施失败负例验证：Docker Runner 返回 `INFRASTRUCTURE` 时大循环返工次数为 0；
  - 新增 `tests/test_fp06_backend_assembly_and_triage.py` 全绿；`./scripts/ci_gate.sh` 全绿；记录写入 `docs/FP06_后端总装与归因大循环记录.md` 并独立 git commit。

---

### FP07｜前端项目总管、只读前端底座与基于权威 `openapi.json` 的 TS 契约类型生成

- **接口选用：**
  - **复用：** `factory_chief.py`（扩展 `plan_frontend_blueprint(spec, openapi_doc, profile, run_id)`）、`asset_cache.py`（物化前端基础模板）、`blueprint_contracts.py`、`openapi_client.py`（已有 E06 OpenAPI 解析与校验逻辑）
  - **新增模块：** `factory_frontend_base.py`（`generate_ts_api_types(openapi_doc) -> str` 确定性生成只读 `frontend/src/generated/api-types.ts` 与 `api-endpoints.ts`；`build_frontend_base(run_dir, fe_blueprint, openapi_doc, materializer)` 搭建 Vue 3 + TS 只读底座：全局布局、Axios 拦截器、Auth Store、公共样式与路由聚合入口）
- **Goal：** 以后端阶段已冻结、已验证的权威 `openapi.json` 和用户需求为唯一输入，由前端总管拆分前端业务部门、冻结路由挂载点与公共请求契约，并由程序确定性生成前端只读底座与全部后端接口的 TypeScript 类型定义。
- **Context：**
  - 前端绝对不能“凭想象猜后端字段名和 URL”，所有前端部门必须消费程序从权威 `openapi.json` 生成的只读 `src/generated/api-types.ts` 和 `src/generated/api-endpoints.ts`。
  - 前端项目总管按业务领域拆出与后端对应的前端部门（如 `auth-ui`、`home-ui`、`cart-ui`、`order-ui`、`payment-ui`），每个前端部门独占 `frontend/src/modules/<dept_id>/{api,views,components,store,routes.ts}` 和 `frontend/test/<dept_id>/`，互不重叠。
- **Constraints：**
  - 前端流水线**必须**校验后端阶段状态为 `PASSED` 且存在有效 `openapi.json` 哈希，否则拒绝启动；
  - `src/generated/api-types.ts`、`src/core/`、`package.json`、`package-lock.json` 均标记为只读受保护路径，禁止任何前端部门修改；
  - 蓝图同样须通过 Python 硬校验（前端部门数 ≤ 8、目录零交集、路由前缀无冲突、全部后端页面需求已覆盖）；
  - 完成后独立提交 git（`feat(fp07): ...`）。
- **WhenDone：**
  - 从电商权威 `openapi.json` 确定性生成无语法错误的 `api-types.ts` 与 `api-endpoints.ts`，覆盖所有路径、请求体和响应 DTO；
  - 前端总管输出 5 部门蓝图并过硬校验；生成的只读前端底座在有 Node 环境下可直接通过 `npm run type-check`（无 Node 环境标 `NOT_RUN`）；
  - 新增 `tests/test_fp07_frontend_chief_and_base.py` 全绿；`./scripts/ci_gate.sh` 全绿；记录写入 `docs/FP07_前端总管与契约底座记录.md` 并独立 git commit。

---

### FP08｜前端多部门异步并行流水线与声明式路由自动聚合

- **接口选用：**
  - **复用：** `factory_department.py`（`DepartmentRunner`）、`factory_scheduler.py`（`AsyncDepartmentPool`）、`agent_asset_tools.py`（检索前端 Vue 能力包与组件模板）、`module_verifier.py`（`stack="frontend"`，执行 `vue-tsc --noEmit` + 定向 `vitest run`）、`factory_state.py`
  - **新增模块：** `factory_frontend_stage.py`（`aggregate_frontend_routes(deliverables) -> str` 确定性扫描全部 `src/modules/*/routes.ts` 生成 `src/router/generated-routes.ts`；`run_frontend_factory_stage(...)` 驱动前端多部门异步并发小循环 + 前端总装 + 全量构建/契约核对大循环）
- **Goal：** 复用与后端完全同构的异步多部门流水线，并发完成所有前端业务部门的页面、状态、接口调用代码与组件单测，自动聚合总路由，确保每个前端调用都严格对齐后端权威 OpenAPI。
- **Context：**
  - 前端每个部门内部仍是最小职责小循环：`DeptLead`（页面与交互拆分）→ `DeptCoder`（可查 PG 前端能力包复用或直接编写 Vue 页面与 `routes.ts`）→ `DeptTester`（编写组件/API 契约测试 + 真实执行 `vue-tsc` 和 `vitest`）。
  - 为防止前端部门写出与后端不符的 URL 或漏掉权限守卫，总装前增加确定性契约核对：扫描前端部门调用的端点是否全部存在于权威 `openapi.json` 中，以及是否存在路由路径冲突。
- **Constraints：**
  - 前端部门同样受 `max_dept_rounds = 3`、`max_integration_rounds = 2` 与双信号量限流约束；
  - 前端禁止写入任何硬编码密钥、禁止使用 `v-html` 渲染未转义用户输入（基础 XSS 静态检查）；
  - 任一前端部门编译/类型检查/单测未真跑通过不得标 `PASSED`；
  - 完成后独立提交 git（`feat(fp08): ...`）。
- **WhenDone：**
  - 5 个前端部门异步并行完成各自页面、组件单测与 `routes.ts`，自动聚合生成 `generated-routes.ts` 且无路由冲突；
  - 故意制造类型不匹配（如页面引用后端 `openapi.json` 中不存在的字段）→ `vue-tsc` 失败 → 生成 `DefectTicket` 打回对应前端部门修复并通过；
  - 新增 `tests/test_fp08_frontend_factory_stage.py` 全绿；`./scripts/ci_gate.sh` 全绿；记录写入 `docs/FP08_前端异步流水线与路由聚合记录.md` 并独立 git commit。

---

### FP09｜全栈总装、Docker Runner 端到端验收与工厂交付包打包

- **接口选用：**
  - **复用：** `factory_backend_stage.py`、`factory_frontend_stage.py`、`runner_client.py`（`HttpRunnerClient.verify()` & `accept()` —— **强制 Docker Runner**）、`packaging.py`（`package_run()`, `collect_files()`, `build_verification()`, `build_cost_report()`）、`budget_pg.py`（`cost_by_department()`）、`generation_graph.py` & `worker.py`
  - **新增/扩展接口：** `factory_pipeline.py` 完整接入 `worker.py`（当 `AGENTFORGE_FACTORY_PIPELINE_ENABLED=1` 且项目/run 选择工厂模式时路由到 `run_factory_pipeline`）；`packaging.py` 新增工厂模式交付清单项：`backend/openapi.json`、`factory-blueprint.json`、`department-cost-report.json`、`EXTERNAL_SERVICES.md`（明确标注支付为本地沙箱模拟及切换指引）
- **Goal：** 把后端流水线与前端流水线合拢为完整的端到端交付闭环：合并全栈源码 + `compose.yaml` + `README.md`，交由**独立 Docker Runner 强制执行全栈构建与端到端验收**，通过后打包生成可下载、可一键启动的全栈 `source.zip`。
- **Context：**
  - 最终交付的 `cost-report.json` 需同时给出总计费用与**按阶段/按部门拆分的成本明细**，方便用户和开源开发者评估每个部门小循环的 Token 消耗。
  - 若第三方能力为本地沙箱模拟（如电商支付），`verification.json` 与 `external_readiness` 必须明确标为 `MOCK_ONLY / SANDBOX_VERIFIED`，**绝不伪标 `LIVE_VERIFIED`**，同时在 `EXTERNAL_SERVICES.md` 写明沙箱演示账号/模拟支付触发方式。
- **Constraints：**
  - 全栈最终验收**强制走独立 Docker Runner**，缺少 Docker Runner 时明确标 `NOT_RUN`、run 不可标 `SUCCEEDED`；
  - 打包前强制执行全量密钥扫描，排除 `node_modules/`、`target/`、`staging/`、`.cache/`；
  - `AGENTFORGE_FACTORY_PIPELINE_ENABLED=false` 时原有串行路径与全部既有测试零改动；
  - 完成后独立提交 git（`feat(fp09): ...`）。
- **WhenDone：**
  - 开启工厂开关后，`POST /api/projects/{id}/runs` 经 `worker` 完整跑通 `后端总管 → 后端并行部门 → 后端总装验收 → 前端总管 → 前端并行部门 → 全栈总装验收 → ZIP 打包`；
  - 下载的 ZIP 包含完整前后端源码、SQL、权威 `openapi.json`、`factory-blueprint.json`、按部门拆分的 `cost-report.json`、`verification.json`、`EXTERNAL_SERVICES.md`，且密钥扫描 0 命中；
  - 新增 `tests/test_fp09_fullstack_factory_delivery.py` 全绿；`./scripts/ci_gate.sh` 全绿；记录写入 `docs/FP09_全栈总装与工厂打包记录.md` 并独立 git commit。

---

### FP10｜平台 UI 多部门泳道进度页与「电商沙箱」开源端到端标杆验收

- **接口选用：**
  - **后端新增只读接口（`routes_runs.py`）：**
    - `GET /api/runs/{run_id}/factory`（校验项目归属，返回：当前阶段 `backend/frontend`、冻结蓝图摘要、各部门泳道状态/当前小循环轮次/耗时/测试结果、大循环轮次、未解决与已解决 `DefectTicket` 摘要、按部门成本明细、以及自定义模型路由脱敏快照）
  - **前端扩展（`platform-ui/src/`）：**
    - `views/RunDetailView.vue` 新增“工厂流水线泳道视图”（区分后端阶段与前端阶段，实时展示每个部门的 `规划 → 编码 → 测试 → 返工/通过` 状态卡片、缺陷单摘要与按部门费用，仍沿用每 2 秒增量轮询与退避，不展示虚假进度百分比）
    - `views/ProjectDetailView.vue` 新增运行模式选择（标准装配模式 / 异步工厂模式，展示模块软上限提示）
- **Goal：** 让开源用户在浏览器 UI 上直观看到“总管派活 → 多部门并发运转 → 缺陷打回自愈 → 后端交棒前端 → 下载完整全栈项目”的全过程，并用真实的 **「电商沙箱项目（登录注册 + 首页展示 + 加入购物车 + 创建订单 + 模拟支付）」** 完成全套端到端标杆验收。
- **Context：**
  - 需兼顾两种用户：使用标准模式跑小项目的用户界面保持简洁；切换到异步工厂模式的用户能清晰追踪每个部门的并行状态、返工原因与真实 Token 成本。
  - 开源验收报告需固定沉淀为 `docs/FACTORY_PIPELINE_ACCEPTANCE.md`，如实记录使用的自定义模型配置、本机 PG 版本、Docker Runner 版本、各部门耗时/轮次/Token/费用、ZIP 解压冷启动验证结果与所有 `NOT_RUN` 项。
- **Constraints：**
  - 前端所有展示的缺陷日志与模型文本必须纯文本/受控 Markdown 渲染，严禁 `v-html`；不向前端暴露任何 API Key 或数据库凭据；
  - 端到端标杆验收若在无真实模型 Key 或无 Docker Runner 环境下执行，必须如实区分“确定性/脚本化验收 PASS”与“真实模型+真实 Docker Runner 验收 NOT_RUN”，待真实 Key 与 Docker 就绪后补跑更新报告；
  - 完成后独立提交 git（`feat(fp10): ...`）。
- **WhenDone：**
  - `GET /api/runs/{run_id}/factory` 具备归属权校验、脱敏审计与完整单测；
  - `platform-ui` 通过 `npm run type-check`、`npm run build` 与新增 `test/factory-run.test.ts`；
  - 产出 `docs/FACTORY_PIPELINE_ACCEPTANCE.md`（含电商 5 部门沙箱完整验收证据、ZIP 解压按 README 冷启动核对、以及原有 MVP/Demo P0 冷启动零回归证明）；
  - `./scripts/ci_gate.sh` 全绿并独立 git commit。

---

## 2. 工厂流水线总验收门槛（开源发布标准）

在公开发布到 GitHub 前，核对以下 6 条总验收门槛：
1. **向后兼容零回归：** 关闭工厂开关时，原有 772 后端 / 41 前端 / 30 Runner 测试与 7 份旧 `asset-lock` 重放全部 PASS。
2. **本地 PostgreSQL 闭环：** 新用户克隆仓库后，按 `README.md` 启动本地 PG → `alembic upgrade head` → `seed_assets` → 配置任意 OpenAI 兼容模型 `config/models.json` 即可跑通。
3. **真实并行与安全隔离：** 多部门异步并发运行时，目录/SQL 零冲突、底座与 Facade Stub 不可被单部门篡改、预算行锁零双扣、单部门小循环与总装大循环均有界收敛。
4. **真实沙箱底线：** 后端总验收与全栈总验收强制由独立 Docker Runner 给出 `PASS`，绝不凭模型口头审查标 `SUCCEEDED`。
5. **支付与第三方安全：** 电商示例的支付能力完整运行在本地沙箱/Mock 模式，交付包零真实密钥、零生产支付凭据，明确标 `MOCK_ONLY / SANDBOX_VERIFIED`。
6. **开源隐私审计：** 全仓库与 Git 历史无任何 `.env`、真实 API Key、账号密码哈希泄漏。
