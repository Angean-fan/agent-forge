# 生成项目基础模板（base template · MyBatis 后端）

可独立启动的全栈基础工程：Spring Boot + **MyBatis（非 Plus）** + MySQL（后端）与 Vue 3 + TypeScript + Vite（前端）。

- 模板版本：`demo-0.2-mb`
- 对应 Platform Profile：`demo-ts-mb@0.1`（见 `../profiles/`）
- 与 `demo-0.2` 的差异：后端 ORM 由 MyBatis-Plus 换为 **MyBatis（XML 显式 SQL）**；前端与 REST 契约保持一致。

## 提供的能力（provided_capabilities）

认证、统一异常、健康检查、Flyway 迁移、前端路由/登录页/基础首页/请求封装/Pinia、Element Plus；
后端 ORM 能力登记为 `mybatis`。

## 目录

```
templates/demo-0.2-mb/
├── compose.yaml            # MySQL(锁 digest) + backend + frontend
├── .env.example
├── backend/                # Spring Boot 3.5.16 / Java 17 / MyBatis 3.x（XML Mapper）
└── frontend/               # Vue 3.5 / TS 5.9 / Vite 8 / Element Plus（同 demo-0.2）
```
