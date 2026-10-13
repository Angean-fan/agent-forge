# E02｜Port/Adapter/Mock 契约链路

日期：2026-10-13
状态：完成（清单契约声明 + 确定性契约校验 + 已发布契约包；免费/确定性测试）
前置：E01（`da0841c`）；迭代文档第二部分 E1 的 E02；技术文档 v0.3 §6。

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：让生成业务 Service **仅依赖内部 Port**，Adapter 实现第三方协议，Mock 实现确定性测试行为。
- **Context**：依赖 E01 的 Integration Pack 清单/锁定；技术文档 §6 约定生成后端隔离为
  `integration/port`、`integration/adapter`、`integration/mock`，且 Integration Pack 需携带
  **契约测试**；并指出"生成 Client 不等于完成幂等与业务语义"。
- **Constraints**：外部响应属于**不可信数据**；**不得把供应商 DTO 直接传播到核心业务规则**；
  Mock 测试成功不能宣称 Live 成功。
- **WhenDone**：同一 Port 可在 Mock 下演示 **成功 / 失败 / 超时**；至少一套**契约测试**对真实 Adapter
  的接口约束有明确断言。

## 输入 / 输出契约

- **清单契约字段（`IntegrationPackManifest`，向后兼容 E01 清单）**：
  - `adapters[]` 增 `implements`（绑定 Port）与 `kind ∈ {mock, stub}`——
    `mock`=确定性测试行为，`stub`=实现真实第三方协议、经注入传输层确定性运行、**不持有真实凭据**
    （`live`/SDK 类仍拒，留待 E04/E05）。
  - `internal_types: list[str]`（Port 契约可引用的**内部类型**白名单）。
  - `contract_tests: list[str]`（包内相对路径；越界/绝对路径拒绝）。
- **`integration_contract.py`（新）**：
  - `port_contract_cases(manifest) -> list[ContractCase]`：按 `Port × {success,failure,timeout}` 确定性派生
    用例（`expectation ∈ {ok, typed_error, typed_timeout}`），供审计。
  - `integration_contract_issues(manifest, *, pack_dir=None) -> list[str]`：确定性拒绝理由，覆盖
    ①每个 Port 有实现 Adapter；②恰好一个 Mock；③至少一个**协议 Adapter**；④Adapter 不得绑定未声明 Port；
    ⑤Mock `scenarios ⊇ {success,failure,timeout}`；⑥**Port 签名只引用内部类型**（供应商 DTO 一律拒）；
    ⑦`contract_tests` 非空、文件存在、且文本断言了 Port 与三种场景。
  - `require_valid_integration_contract(...)`：不通过抛 `ASSET_CONFLICT`(422)。
- **锁定集成**：`resolve_integration_lock` 对**声明了 `contract_tests`** 的清单在锁定时执行契约校验
  （不通过即 `ASSET_CONFLICT`）；未声明契约的旧式清单（如 `weather-lookup@0.1.0`）不做该校验，仍可重放。

## 变更文件

```
integration-packs/weather-lookup/0.2.0/                 # 新增版本（0.1.0 按不可变策略保留不改）
├── manifest.json                                       # ports/adapters(implements)/mock(scenarios)/internal_types/contract_tests
├── README.md
├── src/main/java/com/agentforge/integration/
│   ├── port/WeatherPort.java + WeatherForecast.java    # 内部 Port 与内部 DTO（+ 失败/超时类型）
│   ├── mock/MockWeatherAdapter.java                    # Scenario SUCCESS/FAILURE/TIMEOUT（确定性）
│   └── adapter/HttpWeatherAdapter.java + WeatherTransport.java  # 协议 Adapter + 可注入传输层
└── tests/java/com/agentforge/integration/weather/WeatherPortContractTest.java   # Port 契约测试
platform-api/
├── src/agentforge_platform/
│   ├── asset_registry.py        # adapters.implements + internal_types + contract_tests；adapter kind 放宽为 mock/stub
│   ├── integration_contract.py  # 新增：契约校验 + 用例派生 + CLI
│   └── integration_lock.py      # 声明契约的清单在锁定时执行契约校验
└── tests/test_e02_integration_contract.py   # 新增：18 例
schemas/asset-lock.integration.sample.json     # 刷新：默认最新版升为 0.2.0
```

## 设计

- **Port 只出内部类型**：`internal_types` 声明内部 DTO；Java 方法签名的返回/参数类型 token 必须落在
  `内部类型 ∪ JDK 基础类型`，否则判为"供应商 DTO 进入 Port"并拒绝——确定性实现"不把供应商 DTO 传播到核心业务规则"。
- **Adapter 分层**：`stub` 适配器实现真实协议（解析/映射/错误分类），传输层可注入，测试用确定性 stub，
  **无网络、无凭据**；把供应商原始响应挡在 Port 之内。
- **Mock = 确定性行为**：`MockWeatherAdapter` 按 `Scenario` 演示成功/失败/超时；`port_contract_cases`
  把三条路径固化为可审计用例。
- **契约测试即资产**：随包发布的 `WeatherPortContractTest` 对**任何** `WeatherPort` 实现断言同一接口约束
  （成功返回内部类型、失败/超时为明确类型、空城市拒绝），Mock 与真实 Adapter 共用断言；Mock 通过**不**标 Live。
- **Mock-only 不越界**：`mode` 仍锁 `mock-only`；`live`/SDK adapter kind 在 parse 阶段即拒。
- **版本不变式**：`0.2.0` 为新增版本，`0.1.0` 字节不变；默认解析取最高版本，`id@version`/`pins` 固定复现。

## 验证（实测，免费路径）

`./scripts/ci_gate.sh` → `==> gate passed`

| 目标 | 命令 | 结果 |
|---|---|---|
| 平台后端 | `uv run --locked pytest` | **542 passed**（本步 +18） |
| 平台后端静态 | `ruff check .` | All checks passed |
| 平台前端 | `npm ci + type-check + build + test` | 38 passed |
| 独立 Runner | `pytest / ruff` | 30 passed, 2 skipped；All checks passed |

真实调用：**无**（契约校验全程确定性，不含 LLM；不编译/运行 Java）。

## 费用 / 安全影响

- 费用：无模型调用；契约校验为纯确定性文件/清单检查。
- 安全：Port 不得引用供应商类型（结构性阻断 DTO 外溢）；Adapter 仅 `mock/stub`（无 live/SDK、无真实凭据）；
  `contract_tests` 路径拒绝越界/绝对路径；契约测试要求文件存在且覆盖三场景，避免"空壳断言"。

## WhenDone 逐项判定

| 判据 | 结果 | 依据 |
|---|---|---|
| 同一 Port 可在 Mock 下演示成功/失败/超时 | ✅ PASS | `MockWeatherAdapter` 三 `Scenario` + `WeatherPortContractTest` 三用例；`test_port_contract_cases_cover_success_failure_timeout` |
| 契约测试对真实 Adapter 的接口约束有明确断言 | ✅ PASS | `WeatherPortContractTest` 对 Mock 与 `HttpWeatherAdapter`（stub 传输）共用断言；`integration_contract_issues` 校验其存在、断言 Port 且覆盖三场景 |
| 生成 Service 仅依赖内部 Port | ✅ PASS（契约层） | Port 签名内部类型白名单校验：`test_vendor_type_in_port_signature_is_rejected`；`internal_types` 声明 |
| 供应商 DTO 不传播到核心业务规则 | ✅ PASS | 同上；Adapter 归一化为内部 `WeatherForecast`（`realAdapterParsesUntrustedPayloadIntoInternalType` 断言） |
| Mock 成功 ≠ Live 成功 | ✅ PASS（策略） | `mode=mock-only`、adapter 仅 mock/stub（`test_live_or_sdk_adapter_kind_is_rejected`）；报告级 Mock/Live 区分归 E04 |
| 契约不通过阻断锁定 | ✅ PASS | `test_resolver_rejects_pack_with_invalid_contract`（`ASSET_CONFLICT`） |
| 旧版本可重放 | ✅ PASS | `test_resolver_locks_contract_pack_and_reports_old_version_still_replays`（`weather-lookup@0.1.0` 仍锁定） |

## 剩余风险 / 下一步

- **未接入生成装配链路**：本步只提供契约清单 + 校验 + 资产；把集成包文件 COPY 进生成工程、让生成 Service
  注入 `WeatherPort`、由 Runner 执行 `mvn test` 运行契约测试，**尚未接线**——属"装配 + Runner 集成"能力，
  仍未纳入本步（E01/E02 一致地保持生成默认路径零漂移）。
- **契约测试未在平台侧执行**：平台以"文本断言 + 文件存在"确定性校验契约测试，不编译/运行 Java；
  真正执行需生成工程 + Runner（E04/E05 的 Mock 验证链路）。
- **场景集为最小集**（success/failure/timeout）；限流/重复回调/验签失败/补偿等随具体集成在后续扩展。
- **下一步 E03**：从 pack manifest 生成 `External Requirement` 结构化声明与
  `.env.example`/`EXTERNAL_SERVICES.md` 配置指南（含秘密扫描、变量 1:1 绑定）。
