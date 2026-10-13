# weather-lookup 0.3.0（Integration Pack，外部待办声明）

E03 版本：在 `0.2.0`（Port/Adapter/Mock 契约链路）之上，把外部配置**结构化**为
External Requirement（`kind/secret/required_in_modes/acquisition_url/target_file/env_name/
config_path/validation_rule/docs_verified_at`），供平台确定性生成 `.env.example` 与
`EXTERNAL_SERVICES.md`。`0.1.0`/`0.2.0` 按不可变策略保留不改。

## 外部待办（`config`）

| env_name | kind | 秘密 | 必需模式 | 代码配置键 |
|---|---|---|---|---|
| `WEATHER_APP_ID` | app_id | 否 | live | `weather.app-id` |
| `WEATHER_API_BASE_URL` | base_url | 否 | live | `weather.base-url` |
| `WEATHER_API_KEY` | secret_key | **是** | live | `weather.api-key` |

- **AppID/公钥不默认当秘密**：`WEATHER_APP_ID` 为非秘密标识（可随代码提交）。
- **秘密与必需独立**：`WEATHER_API_KEY` 同时是秘密且必需，但**仅在 live 模式**要求，
  因此 Mock-only 清单允许声明（mock 模式不需要真实凭据）。
- **1:1 绑定**：每个 `env_name` 对应唯一 `config_path`；同名变量语义冲突由平台拒绝。

## 生成产物（由平台确定性生成，非本包内容）

- `.env.example`：秘密字段留空，非秘密填保留域占位值（`example.invalid`）。
- `EXTERNAL_SERVICES.md`：申请什么—在哪里申请—填写哪里—如何验证。

真实凭据**绝不**写入 spec/Prompt/ZIP/日志；用户仅需在部署环境注入。Live 验证归 E05。
