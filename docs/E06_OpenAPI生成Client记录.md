# E06｜基于经过校验 OpenAPI 的 Client 生成

日期：2026-10-13
状态：完成（校验 + 锁定版本生成 + Port 适配 + 复用优先；免费/确定性）
前置：E01—E03（`da0841c`/`568743e`/`e4b2b1a`）；迭代文档第二部分 E2 的 E06；技术文档 v0.3 §6。

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：支持上传/引用**已审核 OpenAPI**，以**锁定版本**生成器生成 Client/DTO，再**适配既有 Port**。
- **Context**：依赖 E01—E03；技术文档 §6 的"Integration Pack → OpenAPI → 文档 → 受限 Agent"四级策略。
- **Constraints**：校验格式、来源与**允许域名**；规范正文是**不可信数据**，不能解释为执行指令；
  生成 Client 后**签名/鉴权/幂等/回调**语义仍需**人工确认**；已有审核 Port/Adapter **先检索复用**；
  OpenAPI 生成的新 Client **不因一次构建**成为公共 Method Unit。
- **WhenDone**：固定 OpenAPI 样例成功生成并通过 **Port 契约测试**；**恶意 URL、超大 schema 与
  未经批准 SDK 被拒**。

## 输入 / 输出契约（`openapi_client.py`）

- **`validate_openapi(text, *, source_url, allowed_hosts) -> list[str]`**：格式（OpenAPI 3.x / info / paths）、
  **来源与允许域名**（须 https、无内嵌凭据、非内网/回环/元数据、主机在允许清单）、规模（字节/路径/操作/schema 数/嵌套深度）、
  **未经批准 SDK**（`x-vendor-sdk`）。
- **`generate_client(text, *, pack_id, port_methods, source_url, allowed_hosts, generator, generator_version, sdk) -> ClientGeneration`**：
  任一校验不通过抛 **`OPENAPI_REJECTED`(422)**；产出：
  - `dtos`：每个 `components.schemas` → 内部 DTO（`com.agentforge.integration.dto`，**非厂商类型**）；
  - `client_interface`：`{Provider}ApiClient`（仅 `<JDK>` 依赖）；
  - `port_adapter`：`{Pack}PortAdapter` **实现既有 Port 方法**（映射到对应 `operationId`）；
  - `output_hash`（内容派生，可复现）、`pending_semantics = (signature, auth, idempotency, callback)`、
    `publishable_as_public_method_unit = False`。
  - **只允许 JDK + 本项目内部包**导入，任何厂商 SDK 一律拒绝。
- **`find_reusable_port(packs, *, port_methods) -> str | None`**：先检索复用——返回提供**同名方法集** Port 的
  已审核资产 `id@version#Port`（取最高版本）。
- `errors.py` +`OPENAPI_REJECTED`。

## 变更文件

```
platform-api/
├── src/agentforge_platform/
│   ├── openapi_client.py    # 新增：校验 + 生成 + Port 适配 + 复用检索
│   └── errors.py            # +OPENAPI_REJECTED
└── tests/test_e06_openapi_client.py   # 新增：19 例
schemas/openapi.exchange-rate.sample.json   # 固定 OpenAPI 样例（汇率服务）
```

## 设计

- **四级链路的第 2 级**：优先复用已审核 Port/Adapter；无审核资产时才以 OpenAPI 生成 Client/DTO 并适配 Port。
- **不可信正文**：规范只被**结构提取**（paths/operations/schemas/参数/响应），描述等文本不被执行；
  生成器**不把规范文本当指令**（样例含"忽略此前所有指令"仍被惰性丢弃）。
- **锁定版本可复现**：生成器 `id@version` 白名单 + `output_hash`；同输入同输出。
- **人工语义待办**：生成物显式带 `pending_semantics`，Client 骨架方法抛 `PENDING` 占位——**签名/鉴权/
  幂等/回调**由人工确认后补全，不臆测供应商协议。
- **不自动升格**：生成物落在项目 `integration/` 扩展点，`publishable_as_public_method_unit=False`；
  入库治理归 E10。

## 验证（实测，免费路径）

`./scripts/ci_gate.sh` → `==> gate passed`

| 目标 | 命令 | 结果 |
|---|---|---|
| 平台后端 | `uv run --locked pytest` | **598 passed**（本步 +19） |
| 平台后端静态 | `ruff check .` | All checks passed |
| 平台前端 | `npm ci + type-check + build + test` | 41 passed |
| 独立 Runner | `pytest / ruff` | 30 passed, 2 skipped；All checks passed |

真实调用：**无**（纯确定性；不发起网络）。

## 安全 / 费用影响

- 费用：无模型调用、无网络。
- 安全：来源域名白名单 + 内网/元数据地址拒绝 + 规模上限，避免 SSRF 式与资源耗尽；生成物仅 JDK/内部依赖，
  杜绝未批准 SDK；正文不可信、不执行；凭据无关（不涉及密钥）。

## WhenDone 逐项判定

| 判据 | 结果 | 依据 |
|---|---|---|
| 固定 OpenAPI 样例成功生成 | ✅ PASS | `test_generate_from_fixed_sample_adapts_port`（DTO/Client/Adapter） |
| 通过 Port 契约（结构）测试 | ✅ PASS | `_port_conformance` 适配既有 Port；`test_port_method_without_operation_is_rejected`（无法适配即拒） |
| 恶意 URL 被拒 | ✅ PASS | `test_malicious_source_url_is_rejected`（http/凭据/未允许/回环/元数据）、`test_server_url_not_allowlisted_is_rejected` |
| 超大 schema 被拒 | ✅ PASS | `test_oversized_spec_is_rejected`、`test_too_many_paths_is_rejected`、`test_deeply_nested_schema_is_rejected` |
| 未经批准 SDK 被拒 | ✅ PASS | `test_unapproved_sdk_is_rejected`、`test_unapproved_generator_is_rejected` |
| 已有审核 Port 先复用 | ✅ PASS | `test_reuses_audited_port_before_generating`（`weather-lookup@0.3.0#WeatherPort`） |
| 正文不可信、不执行 | ✅ PASS | `test_untrusted_body_instructions_are_inert` |
| 生成语义待人工确认 / 不自动升格 | ✅ PASS | `test_pending_semantics_and_not_public_method_unit` |

## 剩余风险 / 下一步

- **生成物未接入装配/编译**：Client/DTO/Adapter 为确定性源码文本，尚未写入项目并执行 `mvn test`；
  真正"通过 Port 契约测试"的编译级验证随装配接入补齐（E01—E06 一致保持生成默认路径零漂移）。
- **仅 JSON OpenAPI**：未引入 YAML 依赖；YAML 规范需转换后再校验。
- **语义映射为约定**：Port 方法与 `operationId` 按名匹配；签名/鉴权/幂等/回调留待人工确认。
- **下一步 E07**：只有第三方文档时生成**待确认 Integration Spec**（向用户提问/解释用 DeepSeek；缺 OpenAPI 时
  提取带出处与待确认项，确认后才由技术角色受限补 Adapter）。
