# 外部服务与配置指南

> 由 AgentForge 从 integration pack manifest 确定性生成；真实凭据绝不写入本文件。

## weather-lookup@0.3.0

### WEATHER_API_BASE_URL — 天气服务基站地址
- 申请什么：天气服务基站地址 （base_url） — Live 模式下第三方天气服务基站地址；mock-only 下无需配置。
- 在哪里申请：https://docs.example.invalid/weather/quickstart
  - 查阅服务文档获取基站域名
- 填写哪里：.env.example 的 `WEATHER_API_BASE_URL`；代码配置键 `weather.base-url`
- 如何验证：以 https:// 开头且指向允许域名（文档核验：2026-10-13）
- 秘密：否　必需：是　必需模式：live

### WEATHER_API_KEY — 天气服务 API Key
- 申请什么：天气服务 API Key （secret_key） — Live 模式访问第三方天气服务的密钥；mock-only 下无需，绝不入库。
- 在哪里申请：https://console.example.invalid/weather/keys
  - 在控制台创建 API Key
  - 复制并保存（仅显示一次）
- 填写哪里：.env.example 的 `WEATHER_API_KEY`；代码配置键 `weather.api-key`
- 如何验证：非空字符串；不得出现在源码/日志/ZIP（文档核验：2026-10-13）
- 秘密：是　必需：是　必需模式：live

### WEATHER_APP_ID — 天气服务 AppID
- 申请什么：天气服务 AppID （app_id） — 第三方天气服务的应用标识；非秘密，可随代码提交。
- 在哪里申请：https://console.example.invalid/weather/apps
  - 注册开发者账号
  - 创建应用并复制 AppID
- 填写哪里：.env.example 的 `WEATHER_APP_ID`；代码配置键 `weather.app-id`
- 如何验证：非空字符串；与第三方控制台一致（文档核验：2026-10-13）
- 秘密：否　必需：是　必需模式：live

## 如何注入真实凭据

- 平台**不收集**真实第三方密钥；请在部署/本地环境的 Secret Store 注入上列变量。
- 秘密变量在 `.env.example` 中留空；真实 `.env` 加入忽略规则且不进入 ZIP。
