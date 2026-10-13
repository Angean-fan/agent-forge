# weather-lookup（Integration Pack，Mock-only）

E01 的代表性第三方适配清单样例：**无真实凭据、非支付**的模拟天气查询服务。用于验证
Integration Pack 能被 Resolver 按 `kind/id/version` 锁定、通过 Profile 兼容筛选，并在
重复/冲突/缺失依赖时被结构化拒绝。

## 分层

| 层 | 资产 | 说明 |
|---|---|---|
| Port | `WeatherPort` | 业务侧**只依赖**的内部接口；生成 Service 不感知第三方 DTO。 |
| Adapter | `MockWeatherAdapter` | 确定性 Mock 实现（mock-only）；外部响应视为**不可信数据**。 |
| Config | `WEATHER_API_BASE_URL` | 可选、**非秘密**；mock-only 下无需配置。真实秘密留待 E03/E05。 |

## 版本发布策略（复用既有资产寻址）

- 发布版本 = `kind=integration-pack` + `id` + `version` 三元组；目录 `integration-packs/<id>/<version>/`。
- **已发布版本不可原地修改**：内容变更必须新增 `<version>` 目录，并回填新的 `checksum`。
- 选择：默认取同 `id` 的**最高版本**；需要复现实验时可显式 `id@version` 或 `pins` 固定。
- 该清单**不进入生成默认注册表**（`DEFAULT_ROOTS`），只有显式请求时才由
  `resolve_integration_lock` 锁定，因此不改变 MVP 冷启动的 0.1 锁。

## 边界

- Agent 的一次性 Patch **不会**自动升格为本清单；入库需经 E10 的来源/许可证/独立测试/人工审核。
- Mock 通过**不**代表 Live 可用（见 E04/E05）。
