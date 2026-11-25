# 三家 Provider 接入与路由冒烟记录

本文记录「把 OpenLux / DeepSeek / 4Router 的 Base_URL 与 API_Key 接入 AgentForge」的最终配置、
本地启动方式、以及 2026-11-24 的真实冒烟证据。密钥只以**环境变量名**出现在配置里，明文仅在
`platform-api/.env`（已 gitignore）。

## 1. 角色映射

| 角色 | 用途 | Provider | Base_URL | Model |
| --- | --- | --- | --- | --- |
| `user_interaction` | 用户对话 / 需求分析 | openlux | `https://api.openlux.ai/v1` | `gpt-6.2-sol` |
| `chief_architect` | 项目总管 | openlux | `https://api.openlux.ai/v1` | `gpt-6.2-sol` |
| `technical_planning` | 技术方案 | openlux | `https://api.openlux.ai/v1` | `gpt-6.2-sol` |
| `dept_coder` | 部门开发（代码成员） | deepseek_official | `https://api.deepseek.com/v1` | `deepseek-flash` |
| `coding` | 开发（非部门） | deepseek_official | `https://api.deepseek.com/v1` | `deepseek-flash` |
| `dept_lead` | 部门副总管 | four_router | `https://4router.net/v1` | `gpt-6.1-sol` |
| `dept_tester` | 部门测试工程师 | four_router | `https://4router.net/v1` | `gpt-6.1-sol` |
| `complex_repair` | 复杂修复 | four_router | `https://4router.net/v1` | `gpt-6.1-sol` |

依据：`docs/AgentForge_03_...` 第 0.2 节「项目总管与部门副总管不直接写业务源码」，故
`dept_lead` 即「副总管」。

对应环境变量（`platform-api/.env`，已 gitignore）：

- `OPENLUX_API_KEY`
- `DEEPSEEK_API_KEY`
- `FOUR_ROUTER_API_KEY`
- `AGENTFORGE_MODELS_CONFIG=.../platform-api/config/models.json`

配置校验规则由 `model_gateway.load_custom_routes()` 强制：未知角色、缺 `provider/model/base_url`、
URL 非法、配置里出现明文密钥字段、`api_key_env` 被写成密钥本体，都会 422。

## 2. 路由配置文件

只有一份：`platform-api/config/models.json`（`version=local-custom-2`）。切换配置只需改启动命令里的
`AGENTFORGE_MODELS_CONFIG`：

```bash
AGENTFORGE_MODELS_CONFIG=$PWD/config/models.json .venv/bin/uvicorn agentforge_platform.main:app --port 8000
```

> 2026-11-24 4Router 上游故障期间曾临时使用 `config/models.smoke.json`（三个角色指向 OpenLux）；
> 4Router 恢复后该文件已删除，正式配置回归单一来源。

## 3. 本地启动

```bash
# 1) PostgreSQL（凭据与 platform-api/.env 中的 15432 对齐）
cd deploy
POSTGRES_USER=angean POSTGRES_PASSWORD=<见 .env> POSTGRES_DB=agentforge POSTGRES_PORT=15432 \
  docker compose -f compose.local-pg.yaml up -d

# 2) 迁移
cd ../platform-api && set -a; source .env; set +a
.venv/bin/alembic upgrade head          # 当前 head = 0018

# 3) 后端（8000）与前端（5173）
.venv/bin/uvicorn agentforge_platform.main:app --host 127.0.0.1 --port 8000
cd ../platform-ui && npm run dev
```

- 健康检查：`GET http://127.0.0.1:8000/health`（注意不是 `/api/health`）
- UI：`http://localhost:5173/`，`/api` 由 Vite 代理到 8000
- 平台账号见 `platform-api/config/accounts.json`（gitignore，`demo_admin`），密码由维护者设置

## 4. 冒烟脚本

`scripts/provider_routing_smoke.py`：对每个角色走**与生产相同的客户端构造路径**
（`production_routing.build_client_for_role()`），各做一次真实调用，并核对账本入账。

```bash
cd platform-api && set -a; source .env; set +a
.venv/bin/python ../scripts/provider_routing_smoke.py --config config/models.smoke.json \
  --roles user_interaction,dept_coder,dept_tester
```

参数：`--roles a,b,c|all`、`--tokens N`（默认 512）、`--config PATH`。
退出码：0 = 所有调用成功（即使账本因缺价记 `UNKNOWN`，也只打 `[warn]` 不改退出码）/ 1 = 有调用失败 / 2 = 缺密钥（不发起请求）。

## 5. 实测证据（2026-11-24，PG 16.15 + alembic 0018 + Vite 8.3.4 / Node 26.13.1）

三家全部通过（每次调用均为真实网络请求）：

```
[call] user_interaction   PASS chars=23 in=26  out=13   msg='我是 ChatGPT，由 OpenAI 开发。'
[call] dept_coder         PASS chars=29 in=47  out=199  msg='当前模型名称是 ChatGPT，所在厂商是 OpenAI。'
[call] dept_lead          PASS chars=36 in=26  out=65   msg='我是 OpenAI 旗下的 ChatGPT，当前具体模型版本未向我披露。'
[call] dept_tester        PASS chars=27 in=26  out=118  msg='当前具体模型名称未向我披露，所属厂商为 OpenAI。'
[call] complex_repair     PASS chars=28 in=26  out=112  msg='当前模型的具体名称未向我提供，所属厂商是 OpenAI。'
```

工具调用（`dept_tester` / `complex_repair` 的 `supports_tools=true` 前提）单独验证过：
4Router `gpt-6.1-sol` 返回 `finish_reason=tool_calls` 且 `tool_calls[0].function` 参数正确。

账本（`model_call`）各行均为 `UNKNOWN / reserved=1000000 / cost=NULL`：路由未配 `price_micro`，
按既定原则**不伪算为 0**，预留 1 元继续占预算。项目默认预算 10 元，因此一个项目约 10 次调用后
会触发 `BUDGET_EXCEEDED`。要跑完整工厂流水线前，需二选一：

- 填 `price_micro`（外部 `AGENTFORGE_PRICE_FILE`，每百万 token 微价），或
- 临时调高 `AGENTFORGE_PROJECT_BUDGET_MICRO`。

模型自报厂商不可信（DeepSeek 也自称 ChatGPT），路由正确性以 `role_status().endpoint/model` 与请求
实际打到的端点为准。

## 6. 4Router 现状（2026-11-24 已恢复）

- 故障期证据：`POST /v1/chat/completions` 与 `/v1/messages`（x-api-key 与 Bearer 两种鉴权）均 502，
  错误为 `upstream API error: 所有凭据均已禁用（0/7）`——网关鉴权已放行，是上游号池被封。
- 恢复后：模型列表**只剩 `gpt-6.1-sol`**（`supported_endpoint_types: ["openai"]`），原
  `claude-opus-5-5` 已下架；因此对应角色改用 `gpt-6.1-sol`。
- `base_url` 保持 `https://4router.net/v1`（客户端拼 `/chat/completions`）。实测裸域名
  `https://4router.net/chat/completions` 也能 200，但仍按标准写法保留 `/v1`。
- 若日后要接 Anthropic 原生协议（`/v1/messages`、`x-api-key`、顶层 `max_tokens`/`system`），
  平台目前只有 OpenAI 兼容客户端，需要新增 `AnthropicMessagesClient` 并在 `_effective_route()` 分支，
  且 base_url 语义要改成 Anthropic 根（`https://4router.net`），不能与 `/v1` 写法混用。
