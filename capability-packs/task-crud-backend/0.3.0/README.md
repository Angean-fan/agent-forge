# 能力包：task-crud-backend

可参数化的**后端单实体 CRUD 能力包**：为生成的 Spring Boot 工程提供 CRUD、分页、
简单搜索与**用户数据归属**，复用模板的 JWT 鉴权与统一异常体系（不重复实现认证）。

- 引入步骤：M11
- 兼容 Profile：`demo-ts-mp`
- 提供能力（provides）：`crud`、`pagination`、`search`、`data_ownership`
- 依赖注入：`com.baomidou:mybatis-plus-jsqlparser:3.5.17`（MyBatis-Plus 分页拦截器所在模块）

## 参数来源（严格来自契约）

参数只从**经校验的 `BusinessContract`** 派生（`capability_packs.build_params`），
字段名、表名、路由、DTO 均不可由模型或任意字符串指定：

- 实体名 / 表名 / 复数路径 ← `contract`（`Task` / `task` / `/api/tasks`）
- 列与 Java 类型 ← 白名单映射（`BIGINT→Long`、`VARCHAR(255)→String`、`DATETIME→LocalDateTime`…）
- 归属列固定为 `owner_id`（缺失即拒绝，保证数据归属）
- 搜索列 = 第一个非 `id/owner_id/created_at` 的 VARCHAR 列（此处 `title`）

## 渲染

```bash
cd platform-api
uv run python -m agentforge_platform.capability_packs \
  ../schemas/task-management.contract.json /tmp/out
```

把基础模板复制到输出目录后渲染能力包文件，并把依赖注入模板 `pom.xml` 的
`<!-- CAPABILITY_PACK_DEPS -->` 标记处；**不改动原模板**。

## 生成内容与会话归属

- `entity/{Entity}.java`、`mapper/{Entity}Mapper.java`
- `dto/{Entity}CreateRequest|UpdateRequest.java`（**不含** `id/created_at/owner_id`）、`vo/{Entity}View.java`、`vo/PageResult.java`
- `service/{Entity}Service.java`：分页（`PageResult.total`）、`LIKE` 搜索、`requireOwned` 归属校验（非本人 → `403`）
- `controller/{Entity}Controller.java`：`GET/POST /api/{plural}`、`GET/PUT/DELETE /api/{plural}/{id}`；归属用户由登录态解析
- `config/MybatisPlusConfig.java`：分页拦截器
- `db/migration/V2__create_{table}.sql`
- `test/{Entity}ApiTest.java`：分页总数、搜索、跨用户拒绝

## M11 验证（实测）

用固定 Task 契约渲染到临时工程后：

| 检查 | 结果 |
|---|---|
| `mvn clean package` | ✅ BUILD SUCCESS |
| `Tests run` | ✅ 7（模板 4 + 能力包 3） |
| 分页总数 `$.total` | ✅ `TaskApiTest.paginationReturnsCorrectTotal` |
| 搜索过滤 | ✅ `TaskApiTest.searchFiltersByField` |
| 成员访问他人记录 | ✅ `TaskApiTest.memberCannotAccessOthersRecord`（读/改/删均 403） |
