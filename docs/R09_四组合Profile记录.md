# R09｜四组合 Profile 认证与高级选择

日期：2026-10-12
状态：完成（第 4 组合 js-mb + 兼容矩阵 + 选型通俗解释接口 + 最小前端控件；构建认证；真实 HTTP 记 NOT_RUN）
前置：R08（`430f7b4`）；迭代文档 D2 的 R09。

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：完成 JS/TS × MyBatis/MyBatis-Plus **四种 Profile** 的独立锁、兼容矩阵、构建及前端高级选择；
  **仅在需要面向用户的技术通俗解释时调用 DeepSeek `deepseek-flash`**。
- **Constraints**：选型与 Profile 兼容判断由 R07 建立的**确定性规则**执行，仅启用经认证的精确版本/模板/资产矩阵；
  旧 `demo-ts-mp@0.3` 保持可读可用，不原地改已发布资产；DeepSeek 只解释**经验证的差异**；用户不能指定任意依赖版本。
- **WhenDone**：四组合均有可追溯 `profile_id/version`、构建报告及验收记录；四组合分别走通规格确认 → Resolver → 锁 → 构建/Task 回归；
  非法组合或未认证版本被拒；默认继续推荐 TS + MyBatis-Plus；四组合各至少验证一次 Method Unit 候选的兼容/不兼容筛选；旧锁重放不受索引刷新影响。

## 输入 / 输出契约

- **第 4 组合 `demo-js-mb@0.1`**：新模板 `demo-0.2-js-mb`（JS 前端 + MyBatis 后端）；
  新包版本 `task-crud-backend-mb@0.1.1`、`task-crud-frontend-js@0.1.1`（**仅兼容 `demo-js-mb`**，故其余组合锁不变）。
- **兼容矩阵** `profiles_matrix.list_profile_options()` → 每个已认证组合的
  `{profile_id, profile_version, template_version, frontend_language, orm, recommended, notes}`；
  `recommended` 固定为 `demo-ts-mp@0.3`；`notes` 为**确定性**特点（前端/ORM 说明），不含模型输出。
- **`GET /api/profiles`**（需登录）：返回四组合选项与确定性特点（**不调用模型**）。
- **`POST /api/projects/{id}/profile-explanation`** body `{profile_id}`（属主 + CSRF）：
  `user_interaction → deepseek-flash` 生成面向用户的通俗解释（**只基于已认证事实**；路由不命中 Qwen），
  返回 `{profile_id, explanation, thinking_mode, reasoning_effort}`；未知组合 404 且**不调用模型**。
- **锁样例**：`schemas/asset-lock.js-mb.sample.json`（第 4 组合确定性锁）。

## 变更文件

```
templates/demo-0.2-js-mb/                       # 新增第 4 组合模板（JS 前端 + MyBatis 后端）
capability-packs/task-crud-backend-mb/0.1.1/    # 新增：仅兼容 demo-js-mb
capability-packs/task-crud-frontend-js/0.1.1/   # 新增：仅兼容 demo-js-mb
profiles/demo-js-mb-0.1.json + certified.json   # 第 4 认证组合
schemas/asset-lock.js-mb.sample.json            # 第 4 组合锁样例
platform-api/
├── src/agentforge_platform/profiles_matrix.py  # 新增：确定性四组合矩阵
├── src/agentforge_platform/profile_explain.py  # 新增：通俗解释（deepseek-flash，仅已验证事实）
├── src/agentforge_platform/routes_profiles.py  # 新增：GET /profiles、POST …/profile-explanation
├── src/agentforge_platform/main.py             # 注册 profiles 路由
└── tests/test_r09_four_combinations.py         # 新增：9 例
platform-ui/
├── src/api/types.ts / src/api/client.ts        # + ProfileOption/Explain + profilesApi
├── src/views/ProfilesView.vue                  # 新增：最小选择控件（列表 + 特点 + AI 解释）
├── src/router/index.ts / test/helpers.ts       # + /profiles 路由
└── test/profiles.test.ts                       # 新增：3 例
```

## 设计

- **确定性矩阵**：四组合来自 `certified.json`；前端语言/ORM/推荐/特点全部由 Profile 文件派生，UI 显示的“事实”不经模型。
- **模型只在解释处**：DeepSeek 只把已验证事实改写成通俗中文；不读原始聊天、不变事实/权限/费用；不命中 Qwen。
- **隔离不破坏旧锁**：新包版本仅声明新组合兼容 → 既有三组合 `resolve` 结果与 `lock_hash` 均不变（见 `test_four_locks_match_committed_samples_and_replay`）。
- **Method Unit 按 Profile 过滤**：四组合各验证一次——仅兼容 `demo-ts-mp` 的方法单元在 ts-mp 被选中，其余三组合以 `profile_incompatible` 被拒。

## 验证（实测）

`./scripts/ci_gate.sh` → `==> gate passed`

| 目标 | 命令 | 结果 |
|---|---|---|
| 第 4 组合后端构建认证 | 装配 `demo-js-mb` Task 工程 → `mvn -B test` | **8 passed，BUILD SUCCESS** |
| 平台后端 | `uv run --locked pytest` | **341 passed**（R08 后 332，本步 +9） |
| 平台后端静态 | `ruff check .` | All checks passed |
| 平台前端 | `npm ci + type-check + build + test` | **36 passed**（+3），type-check 通过 |
| 独立 Runner | `pytest / ruff` | 26 passed, 2 skipped；All checks passed |

**四组合矩阵（实测解析）**：

| Profile | 模板 | 后端包 | 前端包 |
|---|---|---|---|
| `demo-ts-mp@0.3`（推荐） | demo-0.2 | task-crud-backend@0.1.2 | task-crud-frontend@0.1.0 |
| `demo-js-mp@0.1` | demo-0.2-js | task-crud-backend@0.1.3 | task-crud-frontend-js@0.1.0 |
| `demo-ts-mb@0.1` | demo-0.2-mb | task-crud-backend-mb@0.1.0 | task-crud-frontend@0.1.1 |
| `demo-js-mb@0.1` | demo-0.2-js-mb | task-crud-backend-mb@0.1.1 | task-crud-frontend-js@0.1.1 |

> **真实 Runner/MySQL HTTP 验收 = NOT_RUN**（本轮约定）；js-mb 后端与 ts-mb 后端字节一致、前端同 demo-0.2-js，
> 已由 `mvn test`（H2）与前端构建分别认证。

## WhenDone 逐项判定

| 判据 | 结果 | 依据 |
|---|---|---|
| 四组合均有可追溯 profile_id/version + 构建/验收记录 | ✅ PASS | `docs/R06*..R09*`；四组合 `resolve` + 锁样例 + `mvn test`/前端构建 |
| 四组合走通确认 → Resolver → 锁 → 构建/Task 回归 | ✅ PASS | `test_four_combinations_resolve_to_expected_assets`、`test_four_locks_match_committed_samples_and_replay`、各组合 `mvn test` |
| 非法组合/未认证版本被拒 | ✅ PASS | `test_fourth_combination_is_supported`（模板不匹配拒绝）；`is_supported_profile` 只认认证清单 |
| 默认推荐 TS + MyBatis-Plus | ✅ PASS | `test_matrix_options_are_deterministic`（唯一推荐为 demo-ts-mp） |
| 四组合各验证一次 Method Unit 兼容/不兼容筛选 | ✅ PASS | `test_method_unit_compat_filtered_per_combination` |
| 旧锁重放不受影响 | ✅ PASS | 三份旧锁样例哈希不变（`…match_committed_samples_and_replay`） |
| 通俗解释只用已验证差异、不命中 Qwen | ✅ PASS | `test_profile_explanation_uses_deepseek_flash_and_records_ledger`（假客户端，路由 `user_interaction`/`deepseek-flash`） |
| 真实 HTTP 验收 | ⚠️ **NOT_RUN** | 按约定仅构建认证 + 无网回归（如实记录） |

## 风险 / 下一步

- **真实 HTTP 未跑**：四组合真实 Runner/MySQL HTTP 验收均 **NOT_RUN**；建议在 R12/R26 矩阵回归时统一补跑。
- **前端控件为最小实现**：ProfilesView 仅列表 + 特点 + AI 解释（用首个项目计费）；未做完整的“选择并写入 spec”交互。
- **兼容版本复制模式累积**：为不破坏旧锁，新增组合通过“仅兼容新组合的包版本”实现；R10 之后可考虑统一版本与矩阵治理。
- **下一步 R10**：支持第 2—3 个业务实体与关系映射（扩展确定性契约编译、能力包、装配器与 Runner；`MVP_ENTITY_LIMIT=1` 与 `entities[0]` 限制解除后 R01 门禁方可解除）。
