# R08｜补充 Spring Boot + MyBatis（非 Plus）分支

日期：2026-10-12
状态：完成（MyBatis 后端模板 + 能力包 + 认证清单 + `mvn test` 构建认证；真实 HTTP 记 NOT_RUN）
前置：R07（`c14bdab`）；迭代文档 D2 的 R08。

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：制作 MyBatis（非 Plus）的后端模板/映射能力包，**复用同一实体及 REST 业务契约**。
  （本次确认：**XML Mapper 显式 SQL**；验收：**后端构建认证 + 无网回归**，真实 HTTP 记 NOT_RUN。）
- **Context**：依赖 MVP M08—M11 与 R07 的版本思路；需求文档 FR04 要求两种 ORM 可选择。
- **Constraints**：**不改写已认证的 MyBatis-Plus 资产**；MyBatis Profile/模板/能力包以**新 ID 或新版本**发布，经构建认证后才加入允许集；
  普通 CRUD 可多一些显式 SQL，但**权限/分页验收结果必须一致**；保留适配层边界；Finder 必须按 ORM/Profile 过滤，不复用只针对 Plus 的实现。
- **WhenDone**：MyBatis Task 工程可构建启动并通过同一业务测试；**不使用 Plus 专属 API**；新 Profile 经规格确认与 Resolver 锁定后可生成；旧 Plus 资产/锁回归通过。

## 输入 / 输出契约

- **新模板** `templates/demo-0.2-mb`（`version=demo-0.2-mb`, `compatible_profiles=[demo-ts-mb]`）：
  前端同 `demo-0.2`（TS）；后端 ORM 换为 MyBatis：`pom` 用 `mybatis-spring-boot-starter`（去 `mybatis-plus`）、
  `AppUser` 为普通 POJO、`AppUserMapper` 为接口 + `resources/mapper/AppUserMapper.xml`、`UserService`/`AdminUserInitializer` 改写、
  `application*.yml` 增 `mybatis.mapper-locations` 与下划线转驼峰。`provided_capabilities` 以 `mybatis` 取代 `mybatis-plus`。
- **新后端能力包** `capability-packs/task-crud-backend-mb@0.1.0`（`compatible_profiles=[demo-ts-mb]`，`requires template:mybatis`）：
  渲染 `entity`（普通 POJO）、`mapper`（接口 + `@Param`）、`mapper_xml`（**显式 SQL**：`SELECT/INSERT/UPDATE/DELETE` + `LIMIT/OFFSET` + `LIKE`）、
  `service`（`findByIdAndOwner` 归属校验；不存在 404 / 越权 403）、`controller`/`dto`/`vo`/`migration`/`test_api`（同契约）。
- **前端包新版本** `capability-packs/task-crud-frontend@0.1.1`（仅 `compatible_profiles=[demo-ts-mb]`）：使 TS 前端支持 MB 组合；
  **仅兼容 MB**，故 TS(MV P) 组合仍选 0.1.0（旧锁不变）。
- **新 Profile** `profiles/demo-ts-mb-0.1.json`（backend `orm=mybatis`）；加入 **认证清单** `profiles/certified.json`。
- **MB 锁样例** `schemas/asset-lock.mb.sample.json`。
- **派生参数**：`capability_packs.build_params` 增设 `entity_columns_sql/writable_columns_sql/writable_values_sql/writable_set_sql/id_ref/owner_ref/search_column` 等（确定性派生，对 Plus 模板无影响）。

## 变更文件

```
templates/demo-0.2-mb/                                  # 新增 MyBatis 后端模板（前端同 demo-0.2）
capability-packs/task-crud-backend-mb/0.1.0/            # 新增 MyBatis 后端能力包（XML Mapper）
capability-packs/task-crud-frontend/0.1.1/              # 新增：仅兼容 demo-ts-mb 的前端包版本
profiles/demo-ts-mb-0.1.json                            # 新增 MyBatis Profile
profiles/certified.json                                 # + demo-ts-mb
schemas/asset-lock.mb.sample.json                       # 新增 MB 锁样例
platform-api/
├── src/agentforge_platform/capability_packs.py         # build_params 增 MyBatis XML 派生参数
└── tests/test_r08_mybatis_profile.py                   # 新增：7 例
```

## 设计

- **不原地覆盖**：新模板版本、新能力包（`-mb`）、前端包新版本**仅兼容 `demo-ts-mb`** → 既有 TS/JS 组合的锁与哈希不变。
- **显式 SQL 且行为一致**：权限判定（`allowed_roles`）与数据归属（`owner_id`）在 Controller/Service 层实现，与 Plus 版**同结果**；
  分页 `LIMIT/OFFSET` + `COUNT(*)`，搜索 `LIKE`；无 Plus 分页插件/`jsqlparser` 依赖。
- **去 Plus 严格性**：MB 模板与能力包中**无任何** `com.baomidou`/`BaseMapper`/`QueryWrapper`/`@TableName`/`@TableId`。
- **认证**：`profiles/certified.json` 为唯一允许集；`demo-ts-mb` 经 `mvn test` 通过后加入。

## 验证（实测）

`./scripts/ci_gate.sh` → `==> gate passed`

| 目标 | 命令 | 结果 |
|---|---|---|
| MB 后端构建认证 | 装配 `demo-ts-mb` Task 工程 → `mvn -B test` | **Tests run: 8, Failures: 0, Errors: 0；BUILD SUCCESS** |
| 平台后端 | `uv run --locked pytest` | **332 passed**（R07 后 325，本步 +7） |
| 平台后端静态 | `ruff check .` | All checks passed |
| 平台前端 | `npm ci + type-check + build + test` | 33 passed |
| 独立 Runner | `pytest / ruff` | 26 passed, 2 skipped；All checks passed |

> 装配产物 `/tmp/mb-out` 含 `TaskMapper.xml`/`TaskService`（MB）/`TaskApiTest`；`grep` 确认无 Plus 泄漏。
> **真实 Runner/MySQL HTTP 验收 = NOT_RUN**（本轮约定），已在 `profiles/demo-ts-mb-0.1.json` 标注。

## WhenDone 逐项判定

| 判据 | 结果 | 依据 |
|---|---|---|
| MyBatis Task 工程可构建并通过同一业务测试 | ✅ PASS | `mvn test` 8 passed（含 `TaskApiTest` 分页/搜索/越权用例） |
| 不使用 Plus 专属 API | ✅ PASS | `test_mybatis_pack_renders_explicit_xml_without_plus`（无 `com.baomidou`/`BaseMapper`/`@TableName`…） |
| 新 Profile 经确认与 Resolver 锁定后可生成 | ✅ PASS | `test_mybatis_spec_confirmable_and_resolves`、`test_mybatis_lock_matches_committed_sample` |
| Plus/MB 组合按 Profile 严格隔离 | ✅ PASS | `test_plus_and_mybatis_backends_are_not_mixed` |
| 旧 Plus 资产/锁回归通过 | ✅ PASS | `test_legacy_plus_lock_replay_unchanged`（同哈希，后端仍 0.1.2、前端仍 0.1.0） |
| 真实 HTTP 验收 | ⚠️ **NOT_RUN** | 按约定仅构建认证 + 无网回归（如实记录） |

## 风险 / 下一步

- **真实 HTTP 验收未跑**：MB 后端 `mvn test`（H2）已覆盖分页/搜索/归属，但真实 Runner/MySQL HTTP 验收仍 **NOT_RUN**；
  与 R07 一并留待具备 Docker/MySQL 的环境或 R12/R26 矩阵回归补跑。
- **前端包/后端包的“兼容版本复制”模式**：为新 Profile 发布仅兼容该 Profile 的版本，避免改变既有锁；R09 四组合时会进一步统一。
- **下一步 R09**：完成 JS/TS × MyBatis/MyBatis-Plus **四组合**的独立锁、兼容矩阵、构建及前端高级选择；
  仅在需要面向用户的技术通俗解释时调用 DeepSeek `deepseek-flash`。
