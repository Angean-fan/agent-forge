# M25｜平台 UI：创建项目与自然语言输入

日期：2026-10-11
状态：完成（免费链路实测通过；真实 DeepSeek 调用按用户决定记 NOT_RUN）

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：在 `platform-ui/` 做最小登录、项目列表、项目创建和需求输入页面，接入 M04/M05/M22。
- **Context**：依赖 M04/M05/M22；技术文档第 1、5 节建议 Vue 3 + TS + Vite + Element Plus；需求文档 FR01 对非技术用户的业务语言要求。
- **Constraints**：此步只做项目/需求输入，不同时搭完整工作台；所有模型文本按普通文本或安全 Markdown 展示；处理 401/403/422 和加载失败；平台 UI 的依赖独立于生成项目 Profile 管理，不复用 M08 的版本决定。
- **WhenDone**：登录后可创建 Task 项目并经 `user_interaction` 路由触发 **DeepSeek V4.1 Flash** 结构化草稿生成；UI 不出现来自其他模型的用户交互文本；刷新后项目仍在；平台 UI 的 Vue/Vite/TypeScript/Element Plus 等直接依赖记录经兼容性构建验证的精确版本，提交 `package-lock.json`，在干净环境执行 `npm ci`、类型检查、构建与最少一条界面测试；CI 同步覆盖平台 UI 安装、类型检查、构建和测试。

## 新增/变更文件

```
platform-ui/
├── package.json            # 精确版本依赖 + dev/build/type-check/test 脚本
├── package-lock.json       # 提交锁定（npm ci 复现）
├── tsconfig.json           # Vue 3 + TS 严格模式
├── vite.config.ts          # /api 代理到平台 API
├── vitest.config.ts        # jsdom + setup
├── env.d.ts, index.html
├── src/
│   ├── main.ts, App.vue, styles.css
│   ├── api/client.ts       # 统一请求、CSRF 双提交、错误映射
│   ├── api/types.ts        # API 响应类型
│   ├── router/index.ts     # 路由 + 登录守卫
│   ├── stores/auth.ts      # 会话状态（reactive 单例）
│   ├── components/SafeText.vue
│   ├── utils/errors.ts, utils/format.ts
│   └── views/{LoginView,ProjectListView,ProjectCreateView,ProjectDetailView}.vue
└── test/{client,views,package}.test.ts, helpers.ts, setup.ts   # 删除旧 smoke.test.mjs（node --test → vitest）
scripts/ci_gate.sh          # UI 步骤改为 npm ci + 类型检查 + 构建 + 测试
.workflow/master-pipeline.yml
README.md
docs/M25_平台UI记录.md
```

## 技术选型与版本锁定（独立于 Profile）

平台 UI 自己维护 `package.json` + `package-lock.json`，**不引用** `profiles/demo-ts-mp-0.3` 的 `frontend_pinned_versions`，也不与其共享锁文件。选定后经本机兼容性构建实测（见下方验证）。直接依赖（精确版本，无 `^`/`~`）：

| 包 | 版本 | 用途 |
|---|---|---|
| vue | 3.5.43 | 运行时 |
| vue-router | 5.4.0 | 前端路由 |
| element-plus | 2.14.7 | UI 组件库 |
| vite | 8.3.4 | 构建/开发服务器 |
| @vitejs/plugin-vue | 6.0.10 | SFC 编译 |
| typescript | 5.9.3 | 类型系统（匹配 vue-tsc 3.3.13 的 volar 工具链） |
| vue-tsc | 3.3.13 | `--noEmit` 类型检查 |
| vitest | 5.0.3 | 测试运行器 |
| @vue/test-utils | 2.5.1 | 组件测试 |
| jsdom | 30.1.2 | 测试 DOM 环境 |
| @types/node | 26.6.5 | 配置文件类型 |

> 未引入 pinia：本步状态量极小，用 `reactive` 单例即可，减少依赖面。

## 页面与交互

- **登录（`/login`）**：预置账号用户名/密码，元素表单校验；成功后按 `redirect` 回到原页面或项目列表；失败显示后端消息（401 统一提示）。
- **项目列表（`/`）**：仅当前用户项目；列名/规格状态标签/创建时间；行点击或“查看”进入详情；“新建项目”进入创建页；加载失败显示告警；会话失效跳登录。
- **项目创建（`/projects/new`）**：名称 1–60、需求 20–5000（FR01，前后端都校验）；提交 `POST /api/projects`，成功进入详情。
- **需求输入/草稿（`/projects/{id}`）**：展示需求原文与固定技术方案（标注“MVP 暂不可切换”）；“生成/重新生成需求草稿”调用 `POST /api/projects/{id}/extract`（`version` 取当前草稿版本，无草稿为 0），展示模型返回的结构化草稿（摘要、目标、角色、实体字段、权限、业务规则、验收场景、约束、排除项）与 `missing_fields`/`questions`/`pending`/`issues`。本步只展示，不提供编辑与确认（M26）。调用前弹出费用确认。

## 安全与错误处理

- **模型/用户文本**：一律通过 Vue 文本插值渲染（`SafeText.vue`，永不 `v-html`），模型返回的 HTML/脚本不会执行；有专门测试覆盖。
- **会话与 CSRF**：请求带 `credentials: 'include'`；状态变更方法从可读 Cookie `af_csrf` 读令牌并写入 `X-CSRF-Token`（与 M04 双提交一致）。
- **错误分类**：401→清除会话并跳登录；403→权限提示；422→校验提示；`SPEC_CONFLICT`→重新拉取最新草稿；`SPEC_NOT_FOUND`(404)→视为“尚未生成草稿”的正常状态；网络失败→可重试提示。
- **不越界**：本步不请求 `technical_planning`/`coding` 等未启用角色；UI 不出现其他模型的用户交互文本（`user_interaction` 是唯一启用的业务路由）。

## 验证（实测）

1. **干净环境安装 + 类型检查 + 构建 + 测试**（`platform-ui/`）：
   - `npm ci` → `added 179 packages`（按锁文件复现）。
   - `npm run type-check`（`vue-tsc --noEmit`）→ 无输出（通过）。
   - `npm run build`（`vue-tsc` + `vite build`）→ `✓ built`，产出 `dist/`。
   - `npm test`（`vitest run`）→ **3 个文件 / 16 个用例全部通过**。
2. **界面测试覆盖**：`SafeText` 原样输出且不生成 `<img>`（禁止执行 HTML）；`ProjectListView` 渲染两行项目与“需求草稿/已冻结规格”标签、加载失败显示告警、401 跳登录并清除会话；`LoginView` 成功提交后路由到列表、失败停留并显示错误；`package.test.ts` 断言 `private`/`module` 且所有直接依赖为精确版本。
3. **HTTP 契约实测**（真实后端 `uvicorn` + Vite 开发服务器，用户选择“只跑免费用例”）：
   - 开发服务器 `localhost:5173` 返回应用 HTML；`/src/main.ts`、`App.vue`、`router`、各视图与 `api/client.ts` 均 200（运行期 SFC/TS 编译通过）。
   - 经 `:5173` 代理请求 `/api/auth/me` 得到后端 401 统一错误体（证明代理与同源 Cookie 链路成立）。
   - 按 UI 相同的请求方式（Cookie 会话 + `X-CSRF-Token`）驱动：登录 200（下发 `af_session`/`af_csrf`，前者 HttpOnly）→ 无 CSRF 建项目 403 `CSRF_INVALID` → 带 CSRF 建项目 201 → 非法名称 422 `VALIDATION_ERROR` → 列表 200 含新项目、详情 200 需求一致（**刷新持久化**）→ 未生成草稿时 `GET .../spec` 404 `SPEC_NOT_FOUND` → 访问他人/不存在项目 404。
4. **真实 DeepSeek 提取（NOT_RUN）**：`POST /api/projects/{id}/extract` 会经 `user_interaction → deepseek_official/deepseek-flash` 发生真实计费调用。当前无预置账号（已临时生成仅本机、gitignored 的 `platform-api/config/accounts.json` 用于上述免费链路），且用户选择不产生真实模型费用，因此**未执行**该调用；其请求/响应契约由 `test_requirement_extraction.py` 的 mock 用例与前端 `client`/`views` 单测覆盖。

## 剩余风险 / 阻塞

- **真实模型端到端未跑**：`extract` 的真实链路待具备预置账号与获准计费后，在 M26/M27 或 M28 冷启动验收中补齐。
- **构建体积告警**：Element Plus 全量引入使主包 >500kB（gzip 276kB），属提示级；如需可改为按需引入或代码分割。
- **生产同源**：本步用 Vite 开发代理；构建产物的同源托管（由 API 提供静态资源）未在本步实现，属部署编排范围（M28）。
- 临时测试账号 `config/accounts.json`（仅哈希）为本机 gitignored 文件，如需长期使用请自行轮换或替换。

## WhenDone 逐项判定

| 条目 | 判定 | 依据 |
|---|---|---|
| 登录后可创建 Task 项目并经 `user_interaction` 触发 DeepSeek 草稿 | ⚠️ 部分 | 登录→建项目→列表/详情已实测；`extract` 经 `user_interaction→deepseek-flash` 的**真实调用**按用户决定记 NOT_RUN（mock 覆盖契约） |
| UI 不出现其他模型交互文本 | ✅ 满足 | 仅调用 `user_interaction` 路由；无其他角色请求 |
| 刷新后项目仍在 | ✅ 满足 | 服务端持久化；HTTP 实测列表/详情重新读取一致 |
| 精确版本 + `package-lock.json` + 干净环境 `npm ci`/类型检查/构建/界面测试 | ✅ 满足 | 精确版本表；`npm ci`+`vue-tsc`+`vite build`+`vitest`（16 用例）实测 |
| CI 覆盖平台 UI 安装、类型检查、构建、测试 | ✅ 满足（本地脚本） | `scripts/ci_gate.sh` 与 `.workflow` 已更新；Gitee Go 执行环境未启用，管道记为 NOT_RUN（同 M08A） |
