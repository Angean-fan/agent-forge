# M20｜实现单 worker 任务领取与幂等运行 API

日期：2026-10-11
状态：完成

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：实现 `POST /api/projects/{id}/runs` 与后台**独立 worker** 的事务领取、租约、运行幂等键及 `GET /api/runs/{id}`。
- **Context**：依赖 M07/M19；单 worker、租约、Idempotency-Key、`202`；不得用 FastAPI BackgroundTasks 承载长期构建。
- **Constraints**：仅确认规格可运行；重复点击返回同一 run；worker 不与 API 进程绑定；统一 UTC（会话过期、租约、账本）；租约比较以数据库时间为准、进程内超时用单调时钟；多机需主机时钟同步并写明容许偏差；初期不承诺完整恢复，异常落 `FAILED/UNKNOWN` 不伪造成功。
- **WhenDone**：API 提交并轮询；同幂等键不新建 run；关闭 API 不使独立 worker 丢失已持久化任务；worker 上报可查询心跳，停滞判不健康（不自动接管/重放，交 R23）；时间边界与心跳停滞有集成测试。

## 新增/变更文件

```
platform-api/
├── src/agentforge_platform/models.py       # Run += idempotency_key/lease_owner/lease_until；+ Worker
├── src/agentforge_platform/runs.py         # find_by_idempotency / claim_run（原子租约）/ heartbeat / 健康判定
├── src/agentforge_platform/worker.py       # 独立 worker 循环 + 占位处理
├── src/agentforge_platform/routes_runs.py  # POST runs / GET run / GET workers
├── src/agentforge_platform/main.py         # 挂载 runs 路由
├── migrations/versions/0005_*.py           # run 租约/幂等列 + worker 表
└── tests/test_runs_api.py                  # 12 例
```

## API

| 方法 | 行为 |
|---|---|
| `POST /api/projects/{id}/runs`（登录+CSRF+归属；头 `Idempotency-Key`） | 仅当项目有 `active_spec_id`（已确认规格）可运行，否则 409；缺幂等键 400；`spec_id` 与冻结版本不一致 409；**同 (project, key) 返回同一 run**；否则新建 QUEUED，`202` |
| `GET /api/runs/{id}`（归属） | 返回状态/`error_code`/`lease_owner`/时间戳（缺 404/跨用户 403/未登录 401） |
| `GET /api/workers`（登录） | worker 列表 + `last_heartbeat` + `healthy` |

## worker（独立进程）

`python -m agentforge_platform.worker`：循环 { 心跳（UTC）→ `claim_run`（事务 + 原子 UPDATE，`lease_until` 以**数据库时间**比较）→ 处理 → 等待（`time.monotonic`）}。
**默认处理为占位**：进入 `PLANNING` 后**如实判 `FAILED/GENERATION_NOT_IMPLEMENTED`**（真实生成图在 M23 接入，不伪造成功）。

**时钟约定**：会话/租约/时间戳统一 UTC（朴素）；租约过期比较用 DB `now()`；进程内等待用单调时钟。**多机部署**须 NTP 同步，容许偏差建议 < 1s（租约 TTL 60s），详见运行手册。

## 验证（实测）

1. `uv run --locked pytest` → **140 passed**（含 M20 12 例）；`ruff` 全绿。
2. **独立 worker 进程实测**：迁移后种一个 QUEUED run，后台启动 `python -m agentforge_platform.worker`（3s 后停止）→ run 变为 `FAILED/GENERATION_NOT_IMPLEMENTED`，`lease_owner=worker-…`，`worker` 表写入心跳。
3. 覆盖：未确认规格 409；创建 202 且 QUEUED；同幂等键同 run；缺幂等键 400；`spec_id` 不一致 409；未登录 401/缺 CSRF 403；跨用户读取 403/缺失 404；领取置租约、被占不再领、过期可再领；worker 领取并失败 + 心跳健康；心跳停滞判不健康；worker 直接据同一 DB 处理已持久化 run（不依赖 API）。

## 剩余风险 / 阻塞

- 领取仅限 `QUEUED`（`PLANNING` 的崩溃接管/重放交 R23）；`task.deps` DAG 与真实生成图在 M23。
- worker 心跳健康仅为**观测**，不自动接管（符合文档）。
- 无阻塞。

## WhenDone 逐项判定

| 条目 | 判定 | 依据 |
|---|---|---|
| API 可提交任务并轮询状态 | ✅ 满足 | POST 202 / GET run（测试 + 独立进程实测） |
| 同一幂等键重复请求不新建 run | ✅ 满足 | `test_idempotency_key_returns_same_run` |
| 关闭 API 不使独立 worker 丢失已持久化任务 | ✅ 满足 | `test_worker_processes_persisted_run_without_api` + 独立进程实测 |
| worker 上报可查询心跳，停滞判不健康、阻止误报成功 | ✅ 满足 | `/api/workers` + `test_stalled_worker_is_unhealthy`；默认处理不伪造成功 |
| 时间边界与心跳停滞有集成测试 | ✅ 满足 | 租约过期再领 + 心跳停滞测试 |
