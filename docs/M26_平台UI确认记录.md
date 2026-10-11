# M26｜平台 UI：规格编辑、显式确认与运行按钮

日期：2026-10-11
状态：完成（前端测试 + 真实后端 HTTP 免费链路实测通过；无真实模型调用）

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：展示并编辑结构化摘要、业务规则与权限，提供确认后才启用的“开始生成”按钮。
- **Context**：依赖 M07/M22/M25；FR03 要求用户看清功能、实体、角色、验收场景和排除项；M20 是运行提交接口。
- **Constraints**：DeepSeek 的任何推断不可直接视为用户确认；固定 Profile 必须明显标注且暂不可切换；重要 pending 未处理时禁用确认；后端仍要独立校验，确认后生成不可变 spec/hash；不能让后续 Qwen 根据原始对话覆盖其业务含义。
- **WhenDone**：未确认规格无法生成；编辑—确认—生成可走通；状态冲突/过期版本可以显示并重新读取；前端测试通过。

## 新增/变更文件

```
platform-ui/
├── src/api/client.ts              # +saveSpec(PUT)/confirm(POST)/runsApi.start(Idempotency-Key)；RequestOptions.headers
├── src/api/types.ts               # +SpecSource 联合类型、SpecConfirmView、RunView
├── src/components/ConfirmToggle.vue     # 显式确认开关（pending ↔ user_confirmed）
├── src/components/StringListEditor.vue  # 角色/约束/排除项的列表编辑
├── src/views/ProjectDetailView.vue      # 重构为可编辑规格 + 确认 + 生成
└── test/{client,project-detail}.test.ts # +client 用例、+M26 流程用例
README.md
docs/M26_平台UI确认记录.md
```

## 交互与语义

- **可编辑规格**：摘要/目标（`goal`）、角色、业务实体（字段名/类型/必填）、角色权限（角色 + 动作）、业务规则、验收场景、约束、明确不做。
- **显式确认**：每类项的 `source` 由 `ConfirmToggle` 控制，模型推断一律为 `pending`；只有用户点击“确认此项”才变为 `user_confirmed`。`pending`/`issues` 非空时“确认规格”禁用；有未保存改动时也禁用（服务端才是权威）。
- **固定技术方案**：以只读区块显示 `demo-ts-mp@0.3 / 模板 demo-0.2`，明确标注“MVP 暂不可切换”。
- **确认冻结**：`POST /confirm` 成功后显示“已确认并冻结”与 hash（只读）；如需调整，“创建新版本草稿以修改”会以当前版本 `PUT`，由后端新建下一版本草稿（已确认版本不可原地修改）。
- **开始生成**：仅当规格已确认且 `project.active_spec_id === spec.spec_id` 时启用；提交 `POST /api/projects/{id}/runs`（`Idempotency-Key` 头，失败重试复用同一键），成功后展示运行号与状态（运行详情/下载留 M27）。
- **冲突处理**：`PUT`/`confirm` 遇 `SPEC_CONFLICT` 显示消息并重新读取最新草稿；`confirm` 遇 `SPEC_NOT_READY` 显示后端逐项原因。

## 验证（实测）

1. **前端门禁**（`platform-ui/`）：`npm ci` → `npm run type-check`（vue-tsc 通过）→ `npm run build`（✓ built）→ `npm test` → **4 文件 / 23 用例全部通过**（M25 16 + M26 7）。
2. **M26 前端用例**：`pending` 非空时“确认规格”“开始生成”禁用；编辑 `goal` 后“保存修改”启用，`saveSpec` 收到 `(9, version, revision, body)` 且 body 含新 `goal`；保存后（服务端返回无 pending）确认启用；确认后“开始生成”启用，点击调用 `runsApi.start(9, spec_id, key)` 并展示运行号；`409 SPEC_CONFLICT` 显示消息并重新读取（`getSpec` 被再次调用）。客户端用例另覆盖 `saveSpec/confirm/runsApi.start` 的请求形状（CSRF、`Idempotency-Key`、body）。
3. **真实后端 HTTP 免费链路**（`uvicorn` + 预置账号；用 `schemas/task-management.spec.json` 作为合法草稿，**不调用任何模型**）：
   - 未确认即生成 → `409 SPEC_NOT_READY`；
   - `PUT /spec`（`version=0,draft_revision=0`）→ `200`，`issues=[]`、`pending=[]`；
   - 旧 `draft_revision` 再 `PUT` → `409 SPEC_CONFLICT`（前端据此重新读取）；
   - `POST /confirm` → `200`，返回 `spec_id/hash`，`GET /projects/{id}` 的 `active_spec_id` 指向该版本；
   - `POST /runs`（`spec_id` + `Idempotency-Key`）→ `202 QUEUED`；同键重试返回同一 `run id`；缺 `Idempotency-Key` → `400`；`spec_id` 不一致 → `409`。
4. **真实模型**：本步不触发任何 `user_interaction` 调用（编辑/确认/提交均为确定性接口），故无模型费用；`extract` 的真实链路仍按 M25 的 NOT_RUN 记录。

## 剩余风险 / 阻塞

- 运行详情/失败/下载页（含轮询）尚未实现，属 M27；本步生成后仅展示运行号与状态。
- 编辑表单未做字段级前端预校验（如空标题），依赖后端 `confirmation_issues`/422 反馈；MVP 可接受。
- Element Plus 全量引入的主包体积告警仍在（提示级）。

## WhenDone 逐项判定

| 条目 | 判定 | 依据 |
|---|---|---|
| 未确认规格无法生成 | ✅ 满足 | 前端未确认时禁用“开始生成”；后端 `POST /runs` 无 `active_spec_id` → `409 SPEC_NOT_READY`（HTTP 实测 + `test_runs_api.py`） |
| 编辑—确认—生成可走通 | ✅ 满足 | 前端流程用例通过；真实后端 `PUT→confirm→runs` 全链路 HTTP 实测通过 |
| 状态冲突/过期版本可显示并重新读取 | ✅ 满足 | 前端 `409 SPEC_CONFLICT` 显示并重读；后端旧 `draft_revision` 返回 409（HTTP 实测） |
| 前端测试通过 | ✅ 满足 | `npm ci`+`vue-tsc`+`vite build`+`vitest`（23 用例）实测通过 |
