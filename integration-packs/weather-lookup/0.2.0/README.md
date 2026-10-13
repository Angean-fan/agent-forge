# weather-lookup 0.2.0（Integration Pack，Port/Adapter/Mock 契约链路）

E02 版本：在 E01（`0.1.0`）的清单之上加入**契约链路**——业务 Service 只依赖内部 Port，
Adapter 实现第三方协议，Mock 提供确定性测试行为，并随包发布 **Port 契约测试**。
非支付、Mock-only、无真实凭据；`0.1.0` 按不可变策略保留不改。

## 分层（`integration/port`｜`integration/adapter`｜`integration/mock`）

| 层 | 资产 | 说明 |
|---|---|---|
| Port | `port/WeatherPort`、`port/WeatherForecast`、`port/WeatherUnavailableException`、`port/WeatherTimeoutException` | 业务侧只依赖此处；方法签名**只引用内部类型**。 |
| Adapter | `adapter/HttpWeatherAdapter` + `adapter/WeatherTransport` | 实现第三方协议，把**不可信原始响应**归一化为内部类型；传输层可注入。 |
| Mock | `mock/MockWeatherAdapter` | 确定性；按 `Scenario` 演示 `success`/`failure`/`timeout`。 |

契约测试：`tests/java/com/agentforge/integration/weather/WeatherPortContractTest.java`——
对**任何** `WeatherPort` 实现断言接口约束（成功返回内部类型、失败/超时为明确类型、空城市拒绝），
Mock 与真实 Adapter 共用同一套断言。

## 契约要点

- **Port 只出内部类型**：`WeatherForecast` 等列于清单 `internal_types`；供应商 DTO/原始文本不得出现在 Port 上。
- **Adapter 才依赖协议**：`HttpWeatherAdapter` 解析供应商响应、区分超时与业务失败，解析失败明确报错，不吞异常。
- **Mock 覆盖三场景**：`mock.scenarios = [success, failure, timeout]`；`MockWeatherAdapter` 可确定性地演示三者。
- **Mock ≠ Live**：Mock 通过只证明接口与错误语义，不证明真实鉴权/网络/厂商审批；Live 归 E04/E05。

## 版本策略

`0.2.0` 为**新增版本**（不改 `0.1.0`）；内容变更须再新增 `<version>` 并回填 `checksum`。
默认取同 `id` 最高版本，可用 `id@version`/`pins` 固定复现。清单声明 `contract_tests` 后，
`resolve_integration_lock` 在锁定时会执行确定性契约校验（不通过即 `ASSET_CONFLICT`）。
