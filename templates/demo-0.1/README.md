# 生成项目基础模板（base template）

可独立启动的全栈基础工程：Spring Boot + MyBatis-Plus + MySQL（后端）与 Vue 3 + TypeScript + Vite + Element Plus（前端）。

- 模板版本：`demo-0.1`
- 对应 Platform Profile：`demo-ts-mp@0.2`（见 `../profiles/`）
- 引入步骤：M09

## 提供的能力（provided_capabilities）

模板只提供**公共能力**，不含具体业务实体 CRUD（由能力包在 M11/M12 提供）：

- 认证：`POST /api/auth/login`（JWT 签发）、`GET /api/auth/me`（Bearer 鉴权）；Spring Security 无状态过滤链。
- 异常处理：统一 `GlobalExceptionHandler`，对外仅 `{code, message}`，不外泄堆栈。
- 健康检查：`GET /api/health`（公开）。
- 数据库迁移：Flyway（`db/migration`）。
- 前端：路由（含登录守卫）、登录页、基础首页、`fetch` 请求封装、Pinia auth store、Element Plus。
- 基础 CRUD 分页**不在此**模板内（属能力包能力）。

## 与平台账号的关系

**完全独立。** 生成项目的登录用户来自本项目 `app_user` 表；平台账号是另一套体系，两者不复用。

## 目录

```
templates/
├── compose.yaml            # MySQL(锁 digest) + backend + frontend
├── .env.example            # 部署环境变量样例（无真实值）
├── backend/                # Spring Boot 3.5.16 / Java 17 / MyBatis-Plus 3.5.17
└── frontend/               # Vue 3.5 / TS 5.9 / Vite 8 / Element Plus
```

## 运行

### 快速本地（无需 MySQL，使用 H2 内存库）

```bash
cd backend
JWT_SECRET=local-dev-secret-local-dev-secret-1234 mvn spring-boot:run -Dspring-boot.run.profiles=local
# 或运行已打包 jar：java -jar target/backend-0.1.0.jar --spring.profiles.active=local
curl http://localhost:8080/api/health
curl -X POST http://localhost:8080/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}'
```

`local` profile 的演示凭据（`admin/admin123`）与开发密钥仅用于本地演示，不是真实秘密。

### 生产 / compose（MySQL）

复制 `.env.example` 为 `.env` 填好必填项，然后 `docker compose up -d`。MySQL 镜像锁定为
`mysql:8.0.43@sha256:3e646bcda0d9448ffa3d2024eef04e1bca95528ec19b9e8b76749da9d97d4a10`。

### 前端

```bash
cd frontend
npm ci
npm run type-check && npm run build && npm test
npm run dev    # 开发服务器（/api 代理到 :8080）
```

## M09 验证状态（实测）

| 检查 | 结果 |
|---|---|
| 后端 `mvn clean package` | ✅ BUILD SUCCESS（4 tests：健康、登录成功/失败、鉴权） |
| 前端 `npm ci` + `type-check` + `build` + `test` | ✅ 通过（4 tests） |
| 启动 + `/api/health` + 模板登录 | ✅ 本地 H2 profile 实测通过 |

### 未验证项

- **Docker/compose 未执行**：本机未安装 Docker，`compose.yaml`、Dockerfile、MySQL 容器启动未实跑。
- **基础镜像 digest**：仅 MySQL 锁定了 digest；maven/node/nginx/temurin 仍为 tag，需在 M16/M24 前锁定。
- **MySQL 实跑**：本地启动用 H2（MySQL 兼容模式）；对照真实 MySQL 8.0.43 的运行在 M18（Runner）验证。
