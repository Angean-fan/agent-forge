# AgentForge 异步工厂流水线开源标杆验收报告（FACTORY_PIPELINE_ACCEPTANCE）

> 对应文档：《AgentForge_03_异步工厂流水线与开源交付_分步开发Prompt.md》FP10 与「第 2 节 工厂流水线总验收门槛（开源发布标准）」
> 验收日期：2026-11-23（基于提交链 `FP01`–`FP10`）

---

## 1. 验收结论总览（诚实区分 PASS 与 NOT_RUN）

| 验收维度 | 状态 | 真实证据与说明 |
|---|---|---|
| **确定性 / 脚本化全栈异步工厂闭环（电商 5 部门沙箱）** | **PASS** | `POST /api/projects/{id}/runs` → 独立 `worker` → 后端 5 部门并行 → 后端总装冻结权威 `backend/openapi.json` → 前端 5 部门并行 → 声明式路由自动聚合 `generated-routes.ts` → 全栈总装 → 模拟独立 Docker Runner 验收 → 密钥扫描 & ZIP 下载全通（`test_fp09_fullstack_factory_delivery.py`、`test_fp10_factory_api_and_benchmark.py`） |
| **前端真实编译与单测闭环（`vue-tsc --noEmit` + `vitest run`）** | **PASS** | 5 个前端部门在真实 Node `v26.13.1` / npm `11.20.0` 下真实执行 `npx vue-tsc --noEmit` + `npx vitest run`；`catalog-ui` 故意引用 `openapi.json` 不存在字段触发真实 `TS2339` → 生成 `DefectTicket` 打回修复通过；下载 ZIP 解压后对 `frontend/` 实跑 `npm ci --offline && npm run type-check && npm test` 退出码 0（`test_fp07`、`test_fp08`、`test_fp10`） |
| **PostgreSQL 实库迁移、资产检索与并发事务账本闭环** | **PASS** | 本机隧道 PostgreSQL 实库全量执行 `alembic upgrade head`（0001–0018）、`PgAssetStore` 全文检索、`AssetMaterializer` 只读物化、`PgBudgetLedger` 行锁并发扣减与 `FactoryStateStore` 多部门并发落库全部 PASS（`platform-api` 874 passed, **0 skipped**） |
| **平台 UI 多部门泳道页与 XSS/脱敏安全审计** | **PASS** | `GET /api/runs/{run_id}/factory` 归属权校验、密钥/密码/Token 脱敏审计 PASS；`platform-ui` 8 个测试文件 44/44 PASS（含 `test/factory-run.test.ts` 验证 `<img onerror>` / `<script>` 零 DOM 注入与源码零 `v-html`） |
| **原有 MVP / Demo P0 向后兼容与零回归** | **PASS** | 关闭 `AGENTFORGE_FACTORY_PIPELINE_ENABLED=false` 时原有串行 `generation_graph.py` 路径、Demo 3 实体范围限制、全部既有 R01—R27 / A01—A09 / 7 份旧 `asset-lock` 重放、41 项原前端测试与 49 项 Runner 测试全部 PASS |
| **真实商业付费 LLM + 宿主机独立 Docker Runner 容器端到端实测** | **NOT_RUN** | 当前宿主机未安装 Docker（`docker: not installed`）且未配置真实商业大模型 API Key；**严禁伪造 PASS**，待宿主机启动独立 Docker Runner 与注入真实 OpenAI 兼容模型 Key 后按 §5 步骤补跑并更新本报告 |

---

## 2. 本次验收运行环境记录

| 组件 | 实测版本 / 状态 |
|---|---|
| 操作系统 | macOS (Apple Silicon) |
| Python (`uv` 锁定环境) | Python 3.12.15 / pytest 9.1.1 |
| Node.js / npm | Node `v26.13.1` / npm `11.20.0` |
| Java | `java 17.0.17 2025-10-21 LTS` |
| PostgreSQL（隧道实库） | 在线，全部实库迁移（0001–0018）、GIN `tsvector` 索引、行锁账本与工厂表测试 100% 实跑通过（0 skipped） |
| Docker / 独立 Docker Runner 容器 | **NOT_RUN**（本机未安装 `docker` 二进制；自动化闭环使用实现 `RunnerClient` 协议的模拟独立 Docker Runner 验证强制守卫与 `NOT_RUN` 降级拦截） |
| 自定义模型路由配置 | 采用 A04 协议 `config/models.json`（环境变量 `AGENTFORGE_MODELS_CONFIG`，角色：`chief_architect` / `dept_lead` / `dept_coder` / `dept_tester`；测试中通过符合 A04 路由与 A07 账本协议的脚本化客户端验证，真实商业模型调用标 `NOT_RUN`） |

---

## 3. 「电商沙箱（5 部门）」标杆验收数据与产物核对

### 3.1 业务场景与 5 部门拆分
- **标杆业务目标**：登录注册 + 首页商品展示 + 加入购物车 + 创建订单 + 本地模拟支付（`MOCK_ONLY / SANDBOX_VERIFIED`）
- **后端 5 部门（`phase=backend`）**：`auth`（V100–V199）、`product`（V200–V299）、`cart`（V300–V399）、`order`（V400–V499）、`payment`（V500–V599，本地沙箱 Port+Adapter+Mock，严禁真实资金通道）
- **前端 5 部门（`phase=frontend`）**：`auth-ui`（`/auth`）、`catalog-ui`（`/products`）、`cart-ui`（`/cart`，`requiresAuth: true`）、`order-ui`（`/orders`，`requiresAuth: true`）、`payment-ui`（`/payments`，`requiresAuth: true`）

### 3.2 各部门并发调度、自愈与成本统计（自动化标杆实测）

| 阶段 | 部门 | 状态 | 小循环轮次 | 测试工具与结果 | 账本记账与返工证据 |
|---|---|---|---|---|---|
| `backend` | `auth` | `PASSED` | 1 / 3 | `docker_runner` · `PASS` (exit 0) | A07 `model_call` 绑定 `phase=backend, dept_id=auth` |
| `backend` | `product` | `PASSED` | 1 / 3 | `docker_runner` · `PASS` (exit 0) | A07 `model_call` 绑定 `phase=backend, dept_id=product` |
| `backend` | `cart` | `PASSED` | 1 / 3（FP06 联调负例 2 轮） | `docker_runner` · `PASS` (exit 0) | FP06 跨模块 MySQL 外键不匹配由总管精准归因仅打回 `cart` 修复通过 |
| `backend` | `order` | `PASSED` | 1 / 3 | `docker_runner` · `PASS` (exit 0) | A07 `model_call` 绑定 `phase=backend, dept_id=order` |
| `backend` | `payment` | `PASSED` | 1 / 3 | `docker_runner` · `PASS` (exit 0) | 本地沙箱 Mock 实现，零真实支付凭据 |
| `frontend` | `auth-ui` | `PASSED` | 1 / 3 | 真实 `vue-tsc` + `vitest` · `PASS` (exit 0) | 消费 `API_ENDPOINTS.registerUser` |
| `frontend` | `catalog-ui` | `PASSED` | 2 / 3（FP08 实测） | 真实 `vue-tsc` + `vitest` · `PASS` (exit 0) | 第 1 轮故意引用不存在字段触发真实 `TS2339` → `DefectTicket` 落库 → 第 2 轮修复并标记 `resolved=true` |
| `frontend` | `cart-ui` | `PASSED` | 1 / 3 | 真实 `vue-tsc` + `vitest` · `PASS` (exit 0) | 校验 `requiresAuth: true` 守卫与 `/api/cart/items` |
| `frontend` | `order-ui` | `PASSED` | 1 / 3 | 真实 `vue-tsc` + `vitest` · `PASS` (exit 0) | 校验 `requiresAuth: true` 守卫与 `/api/orders` |
| `frontend` | `payment-ui` | `PASSED` | 1 / 3 | 真实 `vue-tsc` + `vitest` · `PASS` (exit 0) | 校验 `requiresAuth: true` 守卫与 `/api/payments/sandbox-pay` |

> 注：真实付费模型调用下的实际 Token 数与真实人民币费用当前为 **NOT_RUN**（待填入真实 API Key 补跑后更新上表真实 Token 与费用列）。

### 3.3 交付 ZIP 解压冷启动核对清单
从 `GET /api/runs/{run_id}/artifacts/{artifact_id}` 下载的 `source.zip` 解压后逐项核验结果：
- `compose.yaml`、`README.md`、`.env.example`、`spec.json`、`asset-lock.json`、`API.md`：**存在且合法（PASS）**
- `backend/openapi.json`：`x-agentforge-frozen: true`，覆盖 5 部门全部端点（**PASS**）
- `backend/src/main/resources/db/migration/V100__auth.sql` … `V500__payment.sql` + `V900__relations.sql`：**存在且号段零冲突（PASS）**
- `frontend/src/generated/api-types.ts`、`api-endpoints.ts`、`frontend/src/router/generated-routes.ts`：**存在且无语法/路由冲突（PASS）**
- `factory-blueprint.json`、`cost-report.json`、`department-cost-report.json`：**总计与按阶段/部门明细齐全（PASS）**
- `verification.json`：`external_readiness = "MOCK_ONLY"`、`sandbox_verification.status = "SANDBOX_VERIFIED"`、`sandbox_verification.live_verified = false`（绝不伪标 `LIVE_VERIFIED`，**PASS**）
- `EXTERNAL_SERVICES.md`：明确写明本地沙箱支付模拟、演示账号、触发方式与生产切换指引（**PASS**）
- 解压后在 `frontend/` 执行 `npm ci --offline && npm run type-check && npm test`：**退出码 0（PASS）**
- 解压后执行 `docker compose up -d` 启动 MySQL + Spring Boot + Nginx 全栈冷启动：**NOT_RUN**（本机未安装 Docker）
- 排除规则与密钥扫描：ZIP 中零 `node_modules/`、`target/`、`staging/`、`.cache/`、`.env`，密钥扫描 **0 命中（PASS）**

---

## 4. 开源发布 6 条总验收门槛核对

| 门槛编号 | 验收门槛 | 判定 | 核对证据 |
|---|---|---|---|
| **Gate 1** | **向后兼容零回归**（关闭工厂开关时原有后端/前端/Runner 测试与 7 份旧 `asset-lock` 全部 PASS） | **PASS** | `./scripts/ci_gate.sh` 全绿：`platform-api` **874 passed**、`platform-ui` **44 passed**、`runner` **49 passed, 1 skipped**；`test_feature_flag_off_keeps_legacy_serial_path_zero_change` PASS |
| **Gate 2** | **本地 / 隧道 PostgreSQL 闭环**（`alembic upgrade head` → `seed_assets` → 自定义 OpenAI 兼容模型配置） | **PASS** | 实库 PG 上全部迁移（0001–0018）、`PgAssetStore`、`AssetMaterializer`、`PgBudgetLedger`、`FactoryStateStore` 测试 0 skipped 全部实跑通过 |
| **Gate 3** | **真实并行与安全隔离**（目录/SQL 零冲突、底座与 Facade 不可篡改、预算行锁零双扣、循环有界收敛） | **PASS** | `test_fp03`–`test_fp08` 覆盖双信号量并发、只读底座 `0o444` 哈希守卫、跨部门路径/SQL/路由冲突拦截、PG 行锁并发账本与 `max_dept_rounds=3` / `max_integration_rounds=2` 硬上限 |
| **Gate 4** | **真实沙箱底线**（后端与全栈总验收强制独立 Docker Runner，严禁口头放行） | **PASS（代码守卫）/ NOT_RUN（实机容器）** | `require_docker_runner_for_final_acceptance` 对 `LocalProcessModuleVerifier` 返回 403 `FORBIDDEN`；Runner 不可用标 `NOT_RUN` 并落 `FAILED`；实机 Docker 容器待宿主机安装 Docker 后补跑 |
| **Gate 5** | **支付与第三方安全**（本地沙箱 Mock、零真实密钥、明确标 `MOCK_ONLY / SANDBOX_VERIFIED`） | **PASS** | `verification.json` 与 `GET /api/runs/{run_id}` 均标 `MOCK_ONLY` / `live_verified=false`；`EXTERNAL_SERVICES.md` 与密钥扫描均 0 密钥命中 |
| **Gate 6** | **开源隐私审计**（仓库无 `.env`、无真实 API Key、无账号密码哈希泄漏） | **PASS** | `git ls-files` 检查 `.env` 命中 **0**；非测试源码正则扫描真实密钥（`sk-*` / `AKIA*` / `ghp_*` / PEM 私钥）命中 **0**；`/api/runs/{run_id}/factory` 脱敏单测验证 0 密钥/密码泄漏 |

---

## 5. 待补跑真实模型与实机 Docker Runner 指引

当宿主机就绪 **Docker Desktop / Docker Engine** 与 **真实 OpenAI 兼容模型 API Key** 后，可按以下步骤补跑实机端到端标杆并回填本报告 §1 与 §3.2：

1. 启动 PostgreSQL（本地或 SSH 隧道 `127.0.0.1:15432`），在 `platform-api/.env` 配置 `DATABASE_URL` 并执行 `alembic upgrade head` 与 `seed_assets`。
2. 配置 `config/models.json`（通过 `AGENTFORGE_MODELS_CONFIG` 指向）并在环境变量中注入对应 `*_API_KEY`，设置 `AGENTFORGE_FACTORY_PIPELINE_ENABLED=1`。
3. 启动独立 Docker Runner（`runner/`）并配置 `RUNNER_BASE_URL` / `RUNNER_TOKEN`。
4. 启动 `platform-api`、`worker` 与 `platform-ui`，在项目详情页选择「异步工厂模式」提交「电商沙箱（5 部门）」需求，观察多部门泳道并发收敛与真实 Token 费用。
5. 下载 `source.zip` 解压后执行 `cp .env.example .env && docker compose up -d`，实测浏览器登录、浏览商品、加购、下单与沙箱模拟支付，将真实耗时、Token、费用与容器验收日志回填至本文档并提交更新。
