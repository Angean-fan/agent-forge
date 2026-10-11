# M02｜搭建 FastAPI 与统一错误响应

日期：2026-10-11
状态：完成

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：实现平台 API 启动、`/health`、请求 ID、统一成功/失败响应结构。
- **Context**：依赖 M01 的 `platform-api/`；技术文档第 5 节约定 `request_id + data/error`，错误含 `code/message/retryable`。
- **Constraints**：只处理 API 骨架；不得执行生成代码、调用模型或绑定 Runner；不在错误堆栈中暴露环境变量。
- **WhenDone**：健康接口返回可解析数据；成功与错误响应均携带 request_id；至少有一例 HTTP 自动化测试且实际通过。

## 新增/变更文件

```
platform-api/
├── pyproject.toml                 # + fastapi, uvicorn；dev + httpx
├── uv.lock                        # 重新锁定
├── README.md                      # + 启动/模块说明
├── src/agentforge_platform/
│   ├── main.py                    # create_app() + app
│   ├── routes.py                  # GET /health
│   ├── middleware.py              # X-Request-ID 生成/沿用 + request.state
│   ├── responses.py               # ApiResponse / ApiErrorResponse / ApiError / HealthData
│   └── errors.py                  # ErrorCode / AppException / 异常处理器
└── tests/test_api_contract.py     # 7 例 HTTP 契约测试
```

## 锁定版本（本次新增）

| 包 | 版本 |
|---|---|
| fastapi | 0.143.0 |
| pydantic | 2.14.0 |
| starlette | 1.7.0 |
| uvicorn | 0.54.0 |
| httpx（dev） | 0.28.1 |

## 设计要点

1. **统一信封**：成功 `{"request_id", "data"}`；失败 `{"request_id", "error": {"code","message","retryable"}}`。`ApiResponse` 用 PEP 695 泛型（`class ApiResponse[T]`）。
2. **请求 ID**：中间件优先沿用合法 `X-Request-ID` 请求头，否则生成 uuid4；写入 `request.state` 并回写同值响应头，保证成功/失败响应一致。
3. **异常映射**：`AppException`→自定义状态码；`RequestValidationError`→422 `VALIDATION_ERROR`；`StarletteHTTPException`→404 `NOT_FOUND`/其余 `BAD_REQUEST`；未预期 `Exception`→500 `INTERNAL_ERROR`，对外仅返回通用消息，堆栈只写服务器日志（不进入响应体）。
4. **不越界**：仅 API 骨架，未执行任何生成代码、未调用模型、未绑定 Runner。

## 实际执行的测试与结果

1. `uv run pytest -v` → **9 passed**（含 M01 的 2 例 + 本步 7 例）。
2. `uv run ruff check .` → **All checks passed**。
3. 真实启动验证：`uv run uvicorn agentforge_platform.main:app --port 8123` 后 `curl`：
   - `GET /health` → `200`，`x-request-id` 头与响应体 `request_id` 一致，`data.status=ok`。
   - `GET /nope` → `404`，`{"error":{"code":"NOT_FOUND","message":"Not Found","retryable":false}}`，含 `request_id` 与同值头。
4. 未运行项：无。

## 已知告警（非失败）

- `fastapi.testclient` 触发 `StarletteDeprecationWarning`（建议安装 httpx2）。当前测试通过；是否切换 httpx2 留待后续依赖评审，未在本步改动。

## 剩余风险 / 阻塞

- 无阻塞。后续 M03 将在此骨架上加入 SQLite 数据访问与迁移。

## WhenDone 逐项判定

| 条目 | 判定 | 依据 |
|---|---|---|
| 健康接口返回可解析数据 | 满足 | `/health` 返回 `{"request_id","data":{"status","version"}}`，测试与 curl 均验证 |
| 成功与错误响应均携带 request_id | 满足 | 成功信封、404 错误信封、领域错误、校验错误、500 由测试与 curl 验证，均含 `request_id` 且与 `X-Request-ID` 头一致 |
| 至少一例 HTTP 自动化测试且实际通过 | 满足 | `tests/test_api_contract.py` 7 例，实跑通过 |
