# R07｜补充 Vue 3 + JavaScript Profile 分支

日期：2026-10-12
状态：完成（JS 模板 + JS 前端能力包 + 认证清单 + 构建认证；真实 HTTP 记 NOT_RUN）
前置：R06D（`2150dc3`）；迭代文档 D2 的 R07。

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：从已通过的 TS 基线派生 JS 模板，**只替换真正不同的前端结构与构建配置**。
- **Context**：依赖 MVP M08—M12；需求文档 FR04 要求 JS/TS 可选择；技术文档第 2 节通过不可变 Platform Profile 管理兼容性。
- **Constraints**：`spec.py:is_supported_profile` 的单一 `demo-ts-mp@0.3` 常量判断改为**读取已发布、已认证 Profile 的确定性规则**，
  校验 `profile_id/profile_version/template_version` 一致性及资产兼容性；只有经 R07 构建认证的 JS 组合才加入允许集；
  MVP 固定 Task 字段规则按旧 spec/场景适用；**已发布 TS Profile/模板不原地覆盖**；**不得把 TS 类型文件无脑复制到 JS 模板**；保持相同接口与业务验收。
- **WhenDone**：JS 模板完成 `npm ci`、`npm run build` 及同一 Task HTTP 验收；已认证 TS/JS Profile 均能通过规格确认、Resolver 和锁定流程；
  未知版本/模板不匹配被拒；旧 TS Profile 与锁可重放。
  - 按本次确认：**构建认证 + 无网 HTTP 回归**；真实 Runner/MySQL HTTP 验收记为 **NOT_RUN**（环境依赖，已在记录与 Profile 中如实标注）。

## 输入 / 输出契约

- **新模板** `templates/demo-0.2-js`（`kind=template`, `version=demo-0.2-js`, `compatible_profiles=[demo-js-mp]`）：
  后端与 `demo-0.2` **逐字节一致**；前端改为 `.js`（`main.js`/`router/index.js`/`api/*.js`/`stores/auth.js`/`utils/validation.js`）、
  `.vue` 去掉 `lang="ts"` 与类型断言、删除 `tsconfig.json`/`env.d.ts`/`src/types/`、`package.json` 移除 `typescript`/`vue-tsc` 与 `type-check`、新增 `vite.config.js`/`vitest.config.js`。
- **新前端能力包** `capability-packs/task-crud-frontend-js@0.1.0`（`compatible_profiles=[demo-js-mp]`）：
  渲染 `src/api/*.js`、`src/views/*ListView.vue`（`<script setup>` 无类型）、`src/router/modules/*.js`、`test/*-api.spec.js`；接口路径与分页/搜索行为与 TS 版一致。
- **新后端能力包版本** `capability-packs/task-crud-backend@0.1.3`（`compatible_profiles=[demo-js-mp]`，内容与 0.1.2 相同）：
  使 JS 组合具备后端 CRUD；**仅兼容 JS**，故 TS 组合仍选中 0.1.2（旧锁不变）。
- **新 Profile** `profiles/demo-js-mp-0.1.json`（frontend=javascript/vue，backend 与 TS 相同）。
- **认证清单** `profiles/certified.json`：`(profile_id, profile_version, template_version)` 白名单；
  `spec.is_supported_profile` 改为查此清单（`AGENTFORGE_CERTIFIED_PROFILES` 可覆盖，测试用）。
- **JS 锁样例** `schemas/asset-lock.js.sample.json`：`resolve_assets(registry, "demo-js-mp")` 的确定性输出。

## 变更文件

```
templates/demo-0.2-js/                                  # 新增 JS 模板（后端同 demo-0.2）
capability-packs/task-crud-frontend-js/0.1.0/           # 新增 JS 前端能力包（templates/*.js.j2）
capability-packs/task-crud-backend/0.1.3/               # 新增：仅兼容 demo-js-mp 的后端包
profiles/demo-js-mp-0.1.json                            # 新增 JS Profile
profiles/certified.json                                 # 新增认证清单
schemas/asset-lock.js.sample.json                       # 新增 JS 锁样例
platform-api/
├── src/agentforge_platform/spec.py                     # is_supported_profile → certified_profiles()
└── tests/test_r07_js_profile.py                        # 新增：8 例
```

## 设计

- **确定性认证规则**：`certified_profiles()` 只读 `profiles/certified.json`；未列入组合一律拒绝（不因新增 Profile 而放宽）。
  TS 0.1/0.2 仍为 `locked` 但**不在认证清单** → 继续拒绝，行为与 R06 前一致。
- **不原地覆盖**：新增独立模板版本与能力包版本；后端包 0.1.3 仅 `compatible_profiles=[demo-js-mp]`，
  因此 `resolve_assets(demo-ts-mp)` 仍选 0.1.2 → **旧 TS 锁哈希不变**。
- **去类型而非复制**：JS 模板与 JS 包为**重写**的 `.js`/无类型 `.vue`，不携带 `typescript`/`vue-tsc`/`tsconfig`。
- **接口与验收一致**：JS 前端渲染的 API 路径（`/api/{plural}`、分页/搜索参数）与 TS 版完全相同。

## 验证（实测）

`./scripts/ci_gate.sh` → `==> gate passed`

| 目标 | 命令 | 结果 |
|---|---|---|
| JS 前端构建认证 | `templates/demo-0.2-js/frontend`: `npm ci` + `npm run build` + `npm test` | 构建通过；**vitest 4 passed** |
| 平台后端 | `uv run --locked pytest` | **325 passed**（R06D 后 317，本步 +8） |
| 平台后端静态 | `ruff check .` | All checks passed |
| 平台前端 | `npm ci + type-check + build + test` | 33 passed |
| 独立 Runner | `pytest / ruff` | 26 passed, 2 skipped；All checks passed |

> 构建认证说明：本机 npm 镜像源在解析 vitest peer 依赖时触发 `edgesOut` 崩溃，改用 `npm install --legacy-peer-deps`
> 生成锁文件；随后 `npm ci`（生成项目/Dockerfile 使用的方式）**无需该标志即可复现安装**。
> **真实 Runner/MySQL HTTP 验收 = NOT_RUN**（本轮约定），已在 `profiles/demo-js-mp-0.1.json` 的 `verification.http_acceptance` 标注。

## WhenDone 逐项判定

| 判据 | 结果 | 依据 |
|---|---|---|
| JS 模板 `npm ci` + `npm run build` | ✅ PASS | 实测通过（见上表） |
| 同一 Task HTTP 验收 | ⚠️ **NOT_RUN** | 按约定仅做构建认证 + 无网 HTTP 回归；需真实 Runner/MySQL（如实记录，未伪装 PASS） |
| 已认证 TS/JS Profile 均能过确认/Resolver/锁定 | ✅ PASS | 无网回归：`test_js_spec_is_confirmable_and_resolves`、`test_js_lock_matches_committed_sample`、`test_certified_profiles_are_supported` |
| 未知版本/模板不匹配被拒 | ✅ PASS | `test_unknown_and_mismatched_profiles_are_rejected`、`test_js_profile_rejects_template_mismatch` |
| 旧 TS Profile 与锁可重放 | ✅ PASS | `test_legacy_ts_profile_and_lock_replay`（同哈希、后端仍 0.1.2） |
| 去类型（未复制 TS 类型文件） | ✅ PASS | `test_js_frontend_pack_renders_plain_javascript`（无 `lang="ts"`/`import type`/`as`） |

## 风险 / 下一步

- **真实 HTTP 验收未跑**：JS 组合的后端与 TS 组合字节一致、前端接口路径相同，但**真实 Runner HTTP 验收仍为 NOT_RUN**，
  需在具备 Docker/MySQL 的环境补跑（R12/R26 矩阵回归时一并覆盖）。
- **JS 前端未做浏览器端到端**：仅构建 + 单测；如需可后续补。
- **npm 镜像 peer 依赖崩溃**：仅影响生成锁文件的一次性操作；已提供 `--legacy-peer-deps` 说明。
- **下一步 R08**：补充 Spring Boot + MyBatis（非 Plus）分支（新 ID/版本发布，经构建认证后加入认证清单；
  Finder 按 ORM/Profile 过滤；不改写已认证的 MyBatis-Plus 资产）。
