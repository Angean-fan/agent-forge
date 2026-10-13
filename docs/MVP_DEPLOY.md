# AgentForge MVP 部署与运行手册

面向把 MVP 跑起来并做日常维护的人。覆盖：平台编排（API/worker/维护/UI + 持久化 SQLite 卷）、
独立 Runner 的部署与连通性、备份/恢复、保留/配额、时钟与心跳告警。

> **两套 compose 不是一回事**：本文件部署的是**平台自身**（`deploy/compose.yaml`）。
> 生成项目 ZIP 内的 `compose.yaml` 是**被生成应用**的编排（MySQL + 后端 + 前端），与平台无关。

## 1. 拓扑

```
[浏览器] ── http://<host>:8080 ──► platform-ui (nginx)  ── /api ──► platform-api (8000) ──┐
                                          │                                                  │
                                   静态 SPA(dist)                                    SQLite 卷(persist)
                                                                                             │
                                                     platform-worker  ◄──── 同一 DB/卷 ──────┘
                                                             │
                                                             │ 内部令牌(X-Runner-Token)
                                                             ▼
                                                    独立 Runner (受控主机:9000)
                                                             │ docker run（受限容器）
                                                             ▼
                                                    执行镜像：JDK17+Maven+Node22
```

- **平台 API 不持有 Docker 权限**；只有 Runner 调用 Docker。
- **Runner 不在平台 compose 内**：它运行在受控主机上，且**不得**向其挂载宿主 docker socket、
  平台数据库或 Provider 密钥。平台仅通过内部令牌访问其 HTTP API。
- **共享运行目录**：worker 写 `run-<id>/`、Runner 读同一目录并挂载进执行容器。
  MVP 要求二者**同机共享**该目录（见 §4）。跨机需自备共享存储（后续项）。

## 2. 前置

- Docker Engine + `docker compose`（本机实测 Docker 29.8.2）。
- 平台账号文件（预置哈希，无注册）：

  ```bash
  cd platform-api
  uv run python -m agentforge_platform.passwords   # 生成 scrypt 哈希
  # 写入 platform-api/config/accounts.json（已被 Git 忽略）
  ```
- 唯一启用的业务模型路由：`user_interaction → DeepSeek 官方 deepseek-flash` 的 API Key。

## 3. 部署平台（deploy/）

```bash
cd deploy
cp .env.example .env      # 填 DEEPSEEK_API_KEY、RUNNER_BASE_URL、RUNNER_TOKEN
docker compose up -d --build
```

- **迁移**：`api` 服务启动命令为 `alembic upgrade head && uvicorn ...`，容器启动即应用迁移。
- **持久化**：命名卷 `platform-db` 挂载到 `/db`（`AGENTFORGE_DATABASE_URL` 指向其中的 `agentforge.db`）；
  `./var/runs` 绑定挂载为运行目录。
- **账号**：挂载 `platform-api/config` 目录到 `/config`（只读），`AGENTFORGE_ACCOUNTS_FILE=/config/accounts.json`；
  把生成的 `accounts.json` 放进该目录即可（**未放置时服务仍能启动**，只是无法登录）。
- **`deploy/.env` 可选**：compose 以 `required: false` 读取，缺失时使用代码内默认值；`cp .env.example .env` 后按需覆盖。
- **密钥边界**：`DEEPSEEK_API_KEY` 只注入 `api`；`worker` 不调用模型，Runner 绝不接收。
- **UI**：`http://<host>:8080`（nginx 提供 SPA，并将 `/api` 反代到 `api:8000`，会话/CSRF 按同源处理）。

**健康检查**：

```bash
curl -s http://127.0.0.1:8000/health      # 平台 API
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/     # UI
```

**冷启动/重启留存（本机实测）**：`up` 后 api 达到 healthy、worker 注册心跳、migration 生效；
经 UI 代理登录并创建项目后 `docker compose restart`，重启后 `GET /api/projects` 仍返回该项目
（数据在 `platform-db` 卷内，重启不丢失）。

**维护服务** `maintenance`：每小时执行

```bash
python -m agentforge_platform.maintenance sessions purge     # 批量清理过期会话
python -m agentforge_platform.maintenance runs cleanup        # 清理过期终态 run 目录
```

### 3.1 nginx 上游必须重新解析

`deploy/nginx.conf` 用 Docker 内嵌 DNS（`resolver 127.0.0.11`）+ 变量式 `proxy_pass`，
以便容器重启后 IP 变化时重新解析。若改为静态 `proxy_pass http://api:8000`，
`docker compose restart` 后会出现 **502**（nginx 仍用旧 IP）。

## 4. 部署独立 Runner（受控主机）

Runner 需 Docker Engine 且与平台共享运行目录。**MVP 采用同机部署**：

```bash
# 1) 从仓库根目录构建执行镜像；构建阶段需能访问 Maven Central 与 npm registry。
#    镜像预热固定模板和能力包依赖，运行阶段不再访问依赖仓库。
docker build -f runner/executor/Dockerfile -t agentforge/runner-executor:demo-0.2 .

# 2) 启动 Runner（在受控主机；RUNNER_WORK_ROOT 必须与平台 worker 指向同一目录）
cd runner && uv sync --dev
export RUNNER_TOKEN=<与平台 .env 中一致的令牌>
export RUNNER_WORK_ROOT=/绝对路径/到/platform-api/var/runs
export RUNNER_EXECUTOR_IMAGE=agentforge/runner-executor:demo-0.2
export RUNNER_CONTAINER_NETWORK=none
uv run uvicorn agentforge_runner.main:app --host 0.0.0.0 --port 9000
```

R27 本机九次评估使用同一个绝对共享目录与 Runner 令牌，在另一终端执行：

```bash
cd platform-api
export RUNNER_BASE_URL=http://127.0.0.1:9000
export RUNNER_WORK_ROOT=/绝对路径/到/platform-api/var/runs
export RUNNER_TOKEN=<与 Runner 相同的令牌>
uv run --locked python ../scripts/demo_p0_eval.py
```

默认评估采用确定性装配，DeepSeek 和 DashScope 均不产生实际请求，因此不需要 API Key。
若另外启用模型角色，需分别配置 `DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`；
模型调用量和费用会按实际账本进入报告。镜像构建阶段需要能访问 Maven Central、npm registry，
Runner 执行阶段保持 `RUNNER_CONTAINER_NETWORK=none`。

**连通性检查**（在平台主机执行）：

```bash
curl -s $RUNNER_BASE_URL/health                     # {"status":"ok",...}
```

平台侧 `deploy/.env` 的 `RUNNER_BASE_URL` 同机 Docker Desktop 用 `http://host.docker.internal:9000`，
同机裸进程用 `http://127.0.0.1:9000`，跨机用受控主机地址。

> **重要约束**：`RUNNER_WORK_ROOT` 必须是 worker 与 Runner **共同可见的同一目录**。
> 平台 worker 在容器内见到的是 `/runs`，Runner 在宿主见到的是其绝对路径；二者须指向同一实体目录。
> 跨机部署需先接入共享存储（NFS/对象存储），否则 Runner 读不到产物——列为后续项。

> 镜像 digest 回填：本地构建的 digest 为本地 manifest 摘要；受控环境应推送 registry 并以
> registry manifest digest 更新 Profile（新增 `profile_version`，不原地覆盖）。

## 5. 时钟同步与心跳告警（多机）

- 所有心跳/租约使用 **UTC**；`is_worker_healthy` 以平台侧时间为基准，阈值
  `AGENTFORGE_WORKER_HEARTBEAT_TIMEOUT`（默认 30s）。多机部署须启用 **NTP**，避免时钟漂移
  造成 worker 被误判为不健康（或租约误过期）。
- **心跳停滞告警探针**（可接入监控/cron，异常退出码非 0）：

  ```bash
  python -m agentforge_platform.maintenance workers check
  ```
  输出 `{"workers":[...],"unhealthy":["..."]}`；存在不健康 worker 时退出码为 1。

## 6. 备份与恢复

- **一致性快照**：SQLite 用 `VACUUM INTO`（走 SQLite 事务，WAL 写入期也一致），
  **不**直接复制 `agentforge.db`（会漏 `-wal` 中已提交事务）。
- **备份**（元数据 + 运行产物，加密后落盘）：

  ```bash
  python3 scripts/backup.py --db platform-api/agentforge.db \
      --runs-root platform-api/var/runs \
      --out-dir backups/ --key-file /etc/agentforge/backup.key
  ```
  产物 `*.tar.gz.enc` 与 `*.manifest.json` 均为 **0600**；加密算法
  `openssl enc -aes-256-cbc -pbkdf2 -md sha256`。
- **密钥分离**：`backup.key`（0600）与备份文件**分机/分目录**存放；备份文件内含 sha256 供校验。
- **恢复**（先停 API/worker）：

  ```bash
  python3 scripts/restore.py --backup backups/<file>.tar.gz.enc \
      --key-file /etc/agentforge/backup.key \
      --db platform-api/agentforge.db --runs-root platform-api/var/runs --force
  ```
  覆盖数据库前会清除旧 `-wal`/`-shm`；manifest 存在时校验 sha256，不符即拒绝。
- **范围/频率/保留**：范围为平台 SQLite 元数据 + 运行产物目录；建议每日一次、保留 7–30 天；
  副本加密且限权；恢复演练见 `docs/MVP_ACCEPTANCE.md`。

## 7. 保留与配额（retention）

- 只清理**已终结**（SUCCEEDED/FAILED）且**超过保留期**的 run 目录；进行中与保留期内产物不动。
- 环境变量：`AGENTFORGE_RUN_QUOTA_BYTES`（0=不限）、`AGENTFORGE_MIN_FREE_BYTES`（0=不检查）、
  `AGENTFORGE_RUN_RETENTION_DAYS`（默认 14）。
- 达到阈值时**拒绝新运行**（`POST /api/projects/{id}/runs` → `507 STORAGE_QUOTA_EXCEEDED`）：

  ```bash
  python -m agentforge_platform.maintenance runs status     # 查看用量/阈值
  python -m agentforge_platform.maintenance runs cleanup    # 清理过期终态产物
  ```

## 8. 升级与回滚

- 迁移：镜像内 `alembic upgrade head`。回滚前先备份；`alembic downgrade` 会删除对应版本新增的数据。
- 配置：改 `deploy/.env` 后 `docker compose up -d` 使服务重建生效。
- Runner/执行镜像升级走**新增 `profile_version`**，不原地覆盖已发布 Profile。
