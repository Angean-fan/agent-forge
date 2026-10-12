# task-crud-backend-mb

Spring Boot + **MyBatis（非 MyBatis-Plus）** 的单实体 CRUD 后端能力包（对应 `demo-ts-mb` Profile）。

- SQL 为**显式** MyBatis XML（`src/main/resources/mapper/*Mapper.xml`），Mapper 接口 + `@Param`；**不使用任何 Plus 专属 API**。
- 权限（按契约 `allowed_roles`）与数据归属（`owner_id`）判定结果与 MyBatis-Plus 版**一致**：不存在 → 404，越权 → 403。
- 分页用 `LIMIT/OFFSET` + `COUNT(*)`，搜索用 `LIKE`（无 Plus 分页插件/`jsqlparser` 依赖）。
- 依赖：无额外 Maven 依赖（`mybatis-spring-boot-starter` 由模板提供）。
