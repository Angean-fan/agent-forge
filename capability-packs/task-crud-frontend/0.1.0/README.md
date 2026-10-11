# 能力包：task-crud-frontend

可参数化的**前端单实体 CRUD 能力包**（Vue 3 + TypeScript）：生成列表/查询/表单页面、
API 请求模块、类型与路由模块，接入模板的登录状态与路由守卫。

- 引入步骤：M12
- 兼容 Profile：`demo-ts-mp`（使用选定 TS Profile，不生成 JS 变体）
- 提供能力（provides）：`crud-ui`、`list`、`form`、`search-ui`、`pagination-ui`

## 参数来源（严格来自契约）

参数仅从**经校验的 `BusinessContract`** 派生：实体名/复数路径、视图字段与可写字段、
TS 类型（`Long→number`、`String→string`、`LocalDateTime→string`…）。字段名与接口路径不可由模型或任意字符串指定。

## 渲染

```bash
cd platform-api
uv run python -c "from pathlib import Path; from agentforge_platform.capability_packs import materialize; from agentforge_platform.contract import BusinessContract; r=Path('..').resolve(); c=BusinessContract.model_validate_json((r/'schemas/task-management.contract.json').read_text()); materialize(r/'templates'/'frontend', r/'capability-packs'/'task-crud-frontend', c, '/tmp/fe')"
```

把基础前端模板复制到输出目录后渲染能力包文件，**不改动原模板**。

## 生成内容

- `src/types/{entity}.ts`：`{Entity}View` / `{Entity}Input`（非必填字段可空/可选）
- `src/api/{entity}.ts`：`list/create/get/update/remove`（复用模板 `request()`，自动带 Bearer）
- `src/views/{Entity}ListView.vue`：Element Plus 表格 + 搜索 + 分页 + 新增/编辑对话框；接口异常经 `ElMessage.error` 显式提示
- `src/router/modules/{entity}.ts`：业务路由（`requiresAuth`）；模板路由经 `import.meta.glob('./modules/*.ts')` 自动加载
- `test/{entity}-api.spec.ts`：请求路径/方法契约的 vitest 用例

> 模板侧前向改动（M12）：路由支持 `modules/*.ts` 自动加载；`request()` 支持空响应体（DELETE）。

## M12 验证（实测）

| 检查 | 结果 |
|---|---|
| `npm ci` + `type-check` | ✅ 通过 |
| `npm run build` | ✅ 通过（页面构建成功） |
| `npm test` | ✅ 7 passed（模板 4 + 能力包 3） |
| 经前端 dev 代理端到端（`登录→新增→查询`） | ✅ health/login/`POST /api/tasks`/`GET /api/tasks?keyword=` 均成功；无 token → 401 |

> 未在真实浏览器中点击操作（无头浏览器未接入）；已用 `type-check`/`build`/单测 + dev 代理端到端覆盖请求链路。UI 错误提示由 `ElMessage.error` 显式处理，不出现空白失败。
