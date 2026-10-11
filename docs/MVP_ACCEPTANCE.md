# AgentForge MVP 验收与交接（M28）

日期：2026-10-11
状态：见文末“已完成 / 未完成 / 下一阶段入口”

本文件是 MVP 阶段的**总验收与交接入口**：列出平台部署/恢复证据、镜像 digest、模型路由、
Provider/型号/模式、实际调用与成本，并明确边界。分步实施记录见 `docs/M01..M27_*.md`，
部署手册见 `docs/MVP_DEPLOY.md`。

## 1. 交付物与判据（对照 M28 WhenDone）

| # | 判据 | 状态 | 证据位置 |
|---|---|---|---|
| 1 | 平台部署编排 + 运行说明（API/worker/UI/SQLite 卷/独立 Runner 连通性） | ✅ | §2、`deploy/`、`docs/MVP_DEPLOY.md` |
| 2 | 冷启动→迁移→健康检查→重启留存；时钟同步与心跳告警；平台 compose 与生成项目 compose 区分 | ✅ | §2、§3 |
| 3 | SQLite 元数据与产物目录备份/恢复演练（范围/频率/保留/加密/权限/结果） | ✅ | §4 |
| 4 | 运行目录/ZIP 配额、剩余空间告警、保留/清理；达阈值拒绝新任务或安全清理 | ✅ | §5 |
| 5 | 过期会话定期批量清理；过期令牌 401；valid 保留；有测试 | ✅ | §6 |
| 6 | 登录接口后端限流与边界测试，已启用 | ✅ | §6 |
| 7 | 真实链路：DeepSeek `deepseek-flash` → 草稿 → 用户确认冻结 → 模板装配（不调 Qwen）→ Runner 验证 | ✅ | §7 |
| 8 | 源码可解压并按 README 启动；一例故意失败样例及真实报告 | ✅ | §8 |
| 9 | CI 基础门禁实际通过；本文件列出部署/恢复证据等 | ⚠️ 见 §9 | §9 |

## 2. 平台部署（`deploy/compose.yaml`，本机实测）

拓扑：`ui(nginx:8080)` → `/api` → `api(uvicorn:8000)` ＋ `worker` ＋ `maintenance`；命名卷 `platform-db`(`/db`)
持久化 SQLite，`./var/runs` 绑定挂载为运行目录。独立 Runner 在受控主机（见 `docs/MVP_DEPLOY.md`）。

实测（Docker 29.8.2）：

```
$ docker compose up -d --build
Container agentforge-platform-api-1  Started … Healthy
Container agentforge-platform-worker-1 Started
Container agentforge-platform-maintenance-1 Started
Container agentforge-platform-ui-1 Started

$ curl -s http://127.0.0.1:8000/health
{"data":{"status":"ok","version":"0.1.0"}}

$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/        → 200
$ curl -s -i http://127.0.0.1:8080/api/auth/me | head -1               → HTTP/1.1 401（经 UI 反代到 API）
```

- **迁移**：`api` 容器启动即 `alembic upgrade head`；`POST /api/projects` 于卷内库成功建表并落库。
- **worker 心跳**：`GET /api/workers` 返回 `{"worker_id":"worker-…","healthy":true}`。
- **重启留存**：经 UI 代理登录并创建项目后 `docker compose restart`，重启后 `GET /api/projects` **仍返回该项目**。
- **发现的缺陷并已修复**：nginx 静态 `proxy_pass` 会缓存 API 容器 IP，重启后 502；
  已改为 Docker 内嵌 DNS（`resolver 127.0.0.11`）+ 变量式 `proxy_pass`（`deploy/nginx.conf`）。

## 3. 时钟同步 / 心跳告警 / 两套 compose

- 心跳与租约使用 **UTC**；`is_worker_healthy` 以平台侧时间为基准（阈值
  `AGENTFORGE_WORKER_HEARTBEAT_TIMEOUT`，默认 30s）。多机部署须启用 NTP，避免时钟漂移误判。
- 心跳停滞告警探针：`python -m agentforge_platform.maintenance workers check`
  （存在不健康 worker 时退出码非 0，可接入监控/cron）。
- **平台 compose ≠ 生成项目 compose**：前者编排平台自身（API/worker/UI/SQLite）；后者在被生成的
  项目 ZIP 内，编排生成应用的 MySQL+后端+前端。二者无关，勿混用（`docs/MVP_DEPLOY.md` 首节）。

## 4. 备份 / 恢复演练（实测）

- **一致性快照**：`VACUUM INTO`（WAL 写入期也一致），不直接复制主库文件。
- **加密/权限**：`openssl enc -aes-256-cbc -pbkdf2 -md sha256`；备份与 manifest 均 `0600`；
  密钥文件（`0600`）与备份分离保存。
- **演练结果**（对真实 `platform-api/agentforge.db` + 产物目录）：

```
project      original=4  restored=4   OK
spec_version original=2  restored=2   OK
run          original=2  restored=2   OK
event        original=7  restored=7   OK
artifact     original=1  restored=1   OK
projects identical: True
```
  另验证：目标已存在且未 `--force` → 拒绝覆盖；被盗改（sha256 不符）→ 拒绝恢复。

- **范围/频率/保留**：范围为平台 SQLite 元数据 + 运行产物目录；建议每日 1 次、保留 7–30 天；
  副本加密、限权、与密钥分离。

## 5. 保留 / 配额（`retention.py`）

- 仅清理**已终结**（SUCCEEDED/FAILED）且**超过保留期**（`AGENTFORGE_RUN_RETENTION_DAYS`，默认 14）的 run 目录；
  进行中与保留期内产物不动。
- 达阈值（`AGENTFORGE_RUN_QUOTA_BYTES` / `AGENTFORGE_MIN_FREE_BYTES`）→ `POST …/runs` 返回
  `507 STORAGE_QUOTA_EXCEEDED`，拒绝新运行；`runs cleanup` 回收空间。
- 测试：`tests/test_retention.py`（仅删过期终态、dry-run、配额超限拒绝、剩余空间不足拒绝、阈值内放行）。

## 6. 会话清理与登录限流（均含测试）

- **会话批量清理**：`python -m agentforge_platform.maintenance sessions purge`（支持 `--loop`），
  由 compose `maintenance` 服务每小时执行；过期会话被删除且过期令牌仍返回 401，有效会话不受影响
  （`tests/test_auth.py`）。
- **登录限流**：`POST /api/auth/login` 按客户端 IP 滑动窗口限流
  （`AGENTFORGE_LOGIN_MAX_ATTEMPTS`=10 / `AGENTFORGE_LOGIN_WINDOW_SECONDS`=60），超限 →
  `429 RATE_LIMITED`（`retryable=true`）。边界测试覆盖：阈值内放行、超阈拒绝（即使凭据正确）、
  窗口滑过复位、按 IP 隔离（`tests/test_auth.py`）。

## 7. 真实链路（DeepSeek → 草稿 → 确认 → 装配 → Runner 验证）

- **模型路由**：`user_interaction → deepseek_official / deepseek-flash`（唯一启用；其余角色为占位，
  调用即拒且不发外部请求）。Provider=DeepSeek 官方 API；型号=`deepseek-flash`；
  模式=`thinking_mode=disabled` + `json_mode=true`；结构纠正最多重试 1 次（最多 2 次计费请求）。
- **不调用 Qwen**：生成期无任何 LLM；装配为确定性模板装配（M15/M23）。
- **端到端**（本机 Docker Desktop 共享 VM；真实 DeepSeek + 真实 Runner 容器；按运行顺序记录）：

  1. **真实提取**：`POST /api/projects/{id}/extract` 经 `user_interaction → deepseek-flash` 返回结构化草稿
     （实测 **2.2s**；所有推断项标 `pending`，符合“模型推断非用户确认”）。
  2. **真实装配 + Runner 验证（首轮，spec 1）**：装配确定性模板工程后交真实 Runner：
     `/verify` **PASS**（`static_manifest`/`backend_test`/`backend_build`/`frontend_build` 全 PASS），
     `/accept` **PASS**（16 项 HTTP 用例全 PASS，含 `cross_user_denied`/`pagination_*`；
     `mysql_digest` 对账一致 `sha256:3e64…`）。
  3. **发现的真实缺陷并已修复（打包）**：M24 `collect_files` 在**排除判断之前**就对符号链接报错，
     使 `node_modules`（本不打包）内的 npm 符号链接令整个 run 以 `UNSAFE_PACKAGE_PATH` 失败
     （run 1/run 3 因此失败）。已改为**先跳过排除项再判符号链接**，并补回归测试
     `test_symlink_inside_excluded_dir_is_skipped`。
  4. **交付包验收发现模型坏推断（spec 3）**：修复后 run 4 打包成功并产出 `source.zip`，
     但固定验收 `create_task` **FAIL** → run 4 `VERIFICATION_FAILED`。根因：真实 DeepSeek 草稿多出
     一个必填字段 `owner`（与 `owner_id` 重复），生成的 `TaskCreateRequest` 因此要求 `owner`，
     而平台固定用例不传该字段 → 400。**这正是固定验收的价值**：编译全 PASS 也挡不住坏推断。
     真实报告存 `docs/samples/acceptance-failure-real-extraction.json`。
  5. **用户编辑 → 重新确认 → 生成（spec 4）**：按 M26 流程，用户在草稿中**删除 `owner` 字段**并逐项确认，
     `PUT` 生成新草稿版本后 `POST /confirm` 冻结（`spec_id=4`，hash `c2d620ab…`），再生成 run 5。
     ✅ **run 5 = `SUCCEEDED`**：真实 Runner `/verify`+`/accept` 全 PASS → 打包 ZIP（`verified=true`）。
  6. **不调用 Qwen**：生成期无任何 LLM；装配为确定性模板装配。
  7. **实际调用与成本**（`cost-report.json`，如实记录、未伪算）：
     `draft_phase = {calls: 1, input_tokens: 1333, output_tokens: 443, cost_micro: 0, unknown_calls: 1}`；
     `generation_phase = {calls: 0}`。即**真实发生 1 次 `deepseek-flash` 调用**（token 已记录）；
     因未配置经核对的同币种价目表（`AGENTFORGE_PRICE_FILE`），成本**记为 UNKNOWN 并保留预留**，不以他币种价格伪算。

> **执行环境说明**：真实链路在**同机裸进程**（API + worker + 独立 Runner 共享运行目录）上跑通；
> 平台自身的 **compose 部署**已在 §2 独立实测（冷启动/迁移/健康/重启留存）。两者组件一致，共用同一套代码与配置。



## 8. 交付物与失败样例

### 8.1 失败样例（真实报告，两例）

**样例 A（故意失败）**：移除生成工程中的数据归属校验（`TaskService.requireOwned` 的 FORBIDDEN 分支），
交真实 Runner 跑 `/verify` + `/accept`。报告：`docs/samples/failure-sample-report.json`。

```
verify_overall = FAIL   checks: static_manifest PASS / backend_test FAIL / backend_build PASS / frontend_build PASS
accept_overall = FAIL   cases : 16 项，cross_user_denied FAIL（其余 PASS）
```

**样例 B（真实模型坏推断，非人为改码）**：真实 DeepSeek 草稿多出必填字段 `owner`，
直接生成的工程在固定验收 `create_task` 上失败。报告：
`docs/samples/acceptance-failure-real-extraction.json`。

```
verify_overall = PASS   checks: static_manifest / backend_test / backend_build / frontend_build 全 PASS
accept_overall = FAIL   cases : create_task FAIL（其余 PASS）
```

**要点**：两例中后端都**编译成功**（`backend_build PASS`），但固定验收与生成工程自带测试仍**如实失败**
→ “**编译成功 ≠ 功能通过**”。失败包在平台侧呈现为 `FAILED` + `VERIFICATION_FAILED/…`，并标注 `未验证`；
样例 B 进一步说明平台为何必须**逐项固定验收**（不能只信编译与模型）。

### 8.2 成功交付包（ZIP，run 5）

由**真实 DeepSeek 提取、用户编辑后重新确认的冻结 spec（spec 4）**装配，经真实 Runner `/verify`+`/accept`
全 PASS 后打包。下载核对（`GET /api/runs/5/artifact`）：

```
X-Artifact-Verified: true        字节数 61586        条目数 73
包含报告：verification.json / cost-report.json / spec.json / asset-lock.json / EXTERNAL_SERVICES.md / API.md
包含 .dockerignore：backend/.dockerignore、frontend/.dockerignore
verification.json：validation_scope=MVP, verified=true, label=已验证
```

**解压后按 README 复现构建**（干净目录，人工未改一行源码）：

```
unzip source.zip
cd backend  && mvn -B -DskipTests package   → target/backend-0.1.0.jar   ✅
cd frontend && npm ci && npm run build      → dist/index.html            ✅
```

> 启动/HTTP 验收由真实 Runner 的 `/accept` 覆盖（MySQL 隔离库 + 固定 16 项 HTTP 用例全 PASS，
> 含登录、CRUD、分页、搜索、跨用户拒绝），见 §7。

## 9. CI 基础门禁

- **本地门禁**（`scripts/ci_gate.sh`，与 CI 同口径）**实际通过**：

```
platform-api: uv run --locked pytest   → 209 passed
platform-api: uv run --locked ruff     → All checks passed
platform-ui : npm ci + type-check + build + test → 28 passed
runner      : pytest / ruff            → 26 passed, 2 skipped; All checks passed
==> gate passed
```

- **Gitee Go 流水线**：**`NOT_RUN`**——本仓库未启用 Gitee Go，无法在本环境触发/核对
  （同 M08A 记录的阻碍）。本地门禁已生效并通过，CI 仅为其托管入口。

## 10. 镜像 digest 与资产锁定

| 资产 | 引用 | digest |
|---|---|---|
| Runner 执行镜像 | `agentforge/runner-executor:demo-0.1` | `sha256:8a80c095c7e070e4a05da79c1b0002bdf41617886f936283fadc6ced7c49fc37`（本地 manifest 摘要，未推 registry） |
| 生成项目 MySQL | `mysql:8.0.43` | `sha256:3e646bcda0d9448ffa3d2024eef04e1bca95528ec19b9e8b76749da9d97d4a10`（与 asset-lock 对账一致） |

Profile：`demo-ts-mp@0.3`（template `demo-0.2`）。

## 11. 已知偏差 / 未完成项（如实记录）

| 项 | 说明 |
|---|---|
| **非专用受控主机** | 执行环境为 Docker Desktop 共享 Linux VM，**非**文档要求的“专用 Linux 主机/VM”；隔离强度弱于专用执行主机（公众上线前需加固）。 |
| **执行镜像未推送 registry** | digest 为本地 manifest 摘要；受控环境须推送并以 registry digest 替换（新增 `profile_version`）。 |
| **执行镜像基础镜像未锁 digest** | `maven:3.9-eclipse-temurin-17` 基座仍为可变 tag。 |
| **依赖来源** | 构建容器的 Maven/npm 依赖经 `RUNNER_CONTAINER_NETWORK=bridge` 临时放行；无受控代理/预热缓存（默认 `none`）。 |
| **Runner 与 worker 需同机共享运行目录** | 跨机需自备共享存储（NFS 等），列为后续项。 |
| **CI 流水线未启用** | Gitee Go `NOT_RUN`（本地门禁已通过）。 |
| **P1 未做** | 浏览器在线预览与自动回收（FR09）不在首版闭环内。 |

## 12. 已完成 / 未完成 / 下一阶段入口

- **已完成**：M01–M28 全部步骤；平台部署编排与冷启动/重启留存；备份恢复演练；保留配额；
  会话清理；登录限流；前端登录→项目→规格确认→生成→运行/下载闭环；CI 本地门禁；
  **真实链路 `deepseek-flash` → 草稿 → 编辑确认冻结 → 模板装配（无 Qwen）→ Runner 验证 → 下载 ZIP → 解压复现构建**。
- **未完成（外部依赖）**：Gitee Go 流水线（未启用，本地门禁已通过）；专用受控执行主机（当前为共享 VM）；
  执行镜像 registry 化；`AGENTFORGE_PRICE_FILE` 未配置导致成本记 UNKNOWN（需核定同币种价目表）。
- **下一阶段入口**：见 `AgentForge_02_迭代增强_分步开发Prompt.md`。优先项：接入受控依赖代理与
  镜像 registry；核定价目表以出真实成本；以新 `profile_version` 引入 Integration Pack / DeepAgent 局部补全；P1 在线预览与回收。
