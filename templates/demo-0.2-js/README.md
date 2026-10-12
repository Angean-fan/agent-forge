# 生成项目基础模板（base template · JavaScript 前端）

可独立启动的全栈基础工程：Spring Boot + MyBatis-Plus + MySQL（后端）与 **Vue 3 + JavaScript + Vite + Element Plus**（前端）。

- 模板版本：`demo-0.2-js`（由 `demo-0.2` 的 TS 基线派生，**只替换真正不同的前端结构与构建配置**）
- 对应 Platform Profile：`demo-js-mp@0.1`（见 `../profiles/`）
- 与 `demo-0.2` 的差异：前端源码为 `.js`、移除 `typescript`/`vue-tsc`/`tsconfig.json`、构建脚本不含 `type-check`；
  后端与接口契约、业务验收**保持一致**。

## 提供的能力（provided_capabilities）

模板只提供**公共能力**，不含具体业务实体 CRUD（由能力包提供）：认证、统一异常、健康检查、Flyway 迁移、
前端路由（含登录守卫）、登录页、基础首页、`fetch` 请求封装、Pinia auth store、Element Plus。

## 与平台账号的关系

**完全独立。** 生成项目的登录用户来自本项目 `app_user` 表；平台账号是另一套体系，两者不复用。

## 目录

```
templates/demo-0.2-js/
├── compose.yaml            # MySQL(锁 digest) + backend + frontend
├── .env.example            # 部署环境变量样例（无真实值）
├── backend/                # Spring Boot 3.5.16 / Java 17 / MyBatis-Plus 3.5.17（与 demo-0.2 相同）
└── frontend/               # Vue 3.5 / JavaScript / Vite 8 / Element Plus
```

## 运行

```bash
cd frontend && npm ci && npm run build   # 前端构建认证
cd backend  && mvn -B -DskipTests package # 后端构建
```
