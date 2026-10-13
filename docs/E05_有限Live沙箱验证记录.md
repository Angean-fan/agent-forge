# E05｜有限 Live 沙箱验证与受控秘密注入（安全层）

日期：2026-10-13
状态：**部分完成**——安全前置/注入计划/失败分类/脱敏结果已实现并测试；**真实 Live 执行 NOT_RUN**（需开发者确认）
前置：E04（`3930c54`）与既有 Runner；迭代文档第二部分 E1 的 E05；技术文档 v0.3 §6/§7。

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：在明确用户已配置第三方测试凭据后，增加**独立 Live 验证工作方式**，输出**脱敏**结果。
- **Context**：依赖 E04 和既有 Runner；技术文档 §6/§7 要求**受控网络、最小凭据注入、提供商测试环境**。
- **Constraints**：平台**不收集**真实第三方密钥给模型；Live 专用 Runner **仅注入该 Adapter 必需秘密**并
  允许**精确域名/端口**；可能扣款/发短信等**真实副作用需额外人工批准**，不与普通测试混用。
- **WhenDone**：未配置时**明确阻止 Live 启动**；配置正确的供应商测试环境可执行**规定契约用例**；
  **鉴权失败和网络不通能区分**且**不泄漏凭据**。

## 本次交付（确定性安全层，`live_validation.py`）

| 能力 | 契约 |
|---|---|
| **前置门禁** | `live_preflight(services, env)` / `require_live_preflight`：缺配置、未声明可 Live、缺/非法允许域名端口、副作用未批准 → 结构化 `issues`，并抛 **`LIVE_NOT_READY`(422)** 阻止启动（不静默回退 Mock） |
| **最小注入计划** | `injection_plan(services)`：只列该 Adapter **必需**变量**名**（秘密/非秘密分列）与副作用，**绝不含值** |
| **规定契约用例** | 复用 E02 的成功/失败/超时契约用例语义；Live 只对**声明可 Live**且**前置通过**的服务执行 |
| **失败分类** | `classify_live_outcome`：`AUTH`(401/403) / `NETWORK`(连接/DNS) / `TIMEOUT`(408/504/超时) / `PROVIDER`(5xx) / `BUSINESS`(其它 4xx) / `OK` |
| **脱敏结果** | `build_live_record`：按服务记 `mode/required/status(脱敏 reason)/outcome/环境/包版本/耗时/证据(redacted)`；`redact_secrets` 覆盖已知值 + PEM/JWT/AWS/秘密赋值 |
| **并入就绪** | `merge_live_records` → 写入 `verification.json.external_services`，由 E04 派生 `LIVE_VERIFIED/LIVE_FAILED` |

## 变更文件

```
platform-api/
├── src/agentforge_platform/
│   ├── live_validation.py   # 新增：前置/注入计划/分类/脱敏结果/合并 + CLI（只读）
│   └── errors.py            # +LIVE_NOT_READY
└── tests/test_e05_live_validation.py   # 新增：11 例
```

## 设计

- **不可猜测安全配置**：允许域名须为**精确主机名**（拒绝通配），端口须显式声明；缺失即阻止——
  不"为兼容陌生 API 而开放任意网络"。
- **密钥不流向模型**：平台不收集真实密钥；注入计划仅含变量名，供 Live 执行环境在**本地/Secret Store**
  注入；证据一律脱敏后记录。
- **Mock≠Live 不混用**：Live 前置独立于普通 Mock 测试；未满足即阻止，不静默降级。
- **副作用需人工批准**：声明 `side_effects`（如扣款/短信）且 `requires_approval` 时，未 `approved` 即阻止。

## 验证（实测，免费路径）

`./scripts/ci_gate.sh` → `==> gate passed`

| 目标 | 命令 | 结果 |
|---|---|---|
| 平台后端 | `uv run --locked pytest` | **579 passed**（本步 +11） |
| 平台后端静态 | `ruff check .` | All checks passed |
| 平台前端 | `npm ci + type-check + build + test` | 41 passed |
| 独立 Runner | `pytest / ruff` | 30 passed, 2 skipped；All checks passed |

真实调用：**无**（不发起任何网络请求；不注入任何真实凭据）。

## WhenDone 逐项判定

| 判据 | 结果 | 依据 |
|---|---|---|
| 未配置时明确阻止 Live 启动 | ✅ PASS | `test_preflight_blocks_when_config_missing`（`LIVE_NOT_READY`）、`_mock_only_integration`、`_missing_or_wildcard_allowlist`、`_side_effects_without_approval` |
| 配置正确的供应商测试环境可执行规定契约用例 | ⚠️ **部分 / NOT_RUN** | 安全前置 + 注入计划 + 契约用例语义已就绪（`test_preflight_allows_when_fully_configured`）；**真实执行需待确认项（见下）** |
| 鉴权失败和网络不通能区分 | ✅ PASS | `test_classify_live_outcome_matrix`（`AUTH` vs `NETWORK` vs `TIMEOUT` vs `PROVIDER` vs `BUSINESS`） |
| 不泄漏凭据 | ✅ PASS | `test_build_live_record_redacts_secrets` + `test_redact_patterns_cover_pem_jwt_aws` + 注入计划不含值 |

## 需开发者确认（阻塞真实 Live 执行，**不从网络文档猜测**）

按执行要求"遇到 API Key、Runner 权限、真实供应商协议、运行环境不确定时暂停并请求确认"：

1. **供应商测试环境与协议**：使用哪个提供商沙箱、允许域名/端口白名单的确切取值、鉴权方式（Key/签名/OAuth）、
   限流与超时阈值、契约用例的**期望响应**；均由你确认后写入受审核来源，不猜测。
2. **Runner 权限**：Live 专用 Runner 的独立执行主机/容器与网络策略（仅放行上列精确域名端口，隔离宿主/内网/云元数据）；
   **平台 API 不持有该权限**。当前仓库 Runner 为 `container_network=none`，未具备 Live 放行能力。
3. **受控秘密注入**：由 live 执行环境在本地/Secret Store 注入，平台与模型均不经手真实值；需确认注入通道与审计方式。
4. **副作用人工批准机制**：扣款/短信等副作用需额外人工批准——需确认批准载体（人工工单/双人复核）与记录格式，
   本次仅提供 `approved` 门禁位，未接入实际批准流。
5. **费用**：真实 Live 可能产生第三方费用/扣款；需确认是否允许、预算与批准范围。

在上述确认前，本步**不含任何真实 Live 调用**（标记 **NOT_RUN**），不得声称 Live 已验证。

## 剩余风险 / 下一步

- **真实 Live 未执行**：`LIVE_VERIFIED/LIVE_FAILED` 仅在提供脱敏结果记录时可派生（E04），真实网络验证待确认后接入。
- **注入计划未接线**：`injection_plan` 尚未传入任何 Runner；live 执行环境与网络策略未实现。
- **下一步 E06**：基于**经过校验 OpenAPI** 的 Client 生成（校验格式/来源/允许域名；规范正文视为不可信数据；
  生成 Client 后仍需人工明确签名/鉴权/幂等/回调语义；已有审核 Port/Adapter 优先检索复用）。
