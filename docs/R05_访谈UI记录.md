# R05｜平台 UI 动态问答卡片

日期：2026-10-12
状态：完成（后端问答端点 + 前端问答卡片；免费测试）
前置：R04（`dafca51`）；迭代文档第一部分 R05。

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：按 R02 协议展示题目/选项/自定义回答，支持保存与继续访谈的页面。
- **Context**：依赖 M25/M26 与 R02—R04；需求文档 FR02 要求业务语言；技术文档第 5 节要求安全渲染、每次 ≤3 题、可离开后接续。
- **Constraints**：一次最多 3 题；只展示 R03 经 schema 校验的题面（非 Qwen 原始输出）；**前端不猜测题型**（按 `type` 渲染）；网络中断不丢已确认答案；待输入与生成失败分别展示。
- **WhenDone**：至少两轮问答、离开并回来接续；过期题目有清晰提示；构建与交互测试通过。

## 输入 / 输出契约

- **`GET /api/projects/{id}/interview`** → `InterviewView{project_id, draft_id, round, status, waiting, questions[], pending[], issues[], error_code}`；`status ∈ {NEW, CLARIFYING, READY_TO_CONFIRM, NEEDS_INPUT}`。无草稿 → 404；非属主/未登录 → 403/401。
- **`POST /api/projects/{id}/interview`** body `{answers: Answer[]}`：空 answers = 开始（已开始则原样返回、不重复生成/计费）；带 answers = 续答（不在等待中 → 409）。
- 题面经 R02 `Question` 校验（业务语言、≤3 题）；答案经 R02 版本绑定校验，过期 → `issues` 且不推进。

## 变更文件

```
platform-api/
├── src/agentforge_platform/routes_interview.py   # 新增：GET/POST 访谈端点
├── src/agentforge_platform/main.py               # 注册路由
├── src/agentforge_platform/interview_graph.py    # 问题序列化 mode=json；新增 read_interview（读状态不 invoke）
└── tests/test_interview_api.py                   # 新增：6 例
platform-ui/
├── src/api/types.ts            # + Interview* 类型；SoftwareSpecification + pages
├── src/api/client.ts           # + interviewApi
├── src/views/InterviewView.vue # 新增：问答卡片页
├── src/views/ProjectDetailView.vue  # “需求访谈”入口；blankSpec + pages
├── src/router/index.ts         # /projects/:id/interview
├── test/{helpers,project-detail,client}.ts  # 路由桩 / + pages
└── test/interview.test.ts      # 新增：5 例
```

## 设计

- **后端**：复用 R04 图；`read_interview` 用 `get_state` 只读持久化状态（支持“离开再回来”）；POST 无 answers 且已在等待时**原样返回**，不重新生成。`_context` 复用请求所用数据库（含测试覆盖），`client=None` → 生产走 DeepSeek（测试 `monkeypatch DeepSeekClient.complete`）。
- **前端**：题型由后端给定；单选/多选/文本 + 自定义（`allow_custom`）+“不确定”按 `type` 条件渲染；题面走 `SafeText`（纯文本，永不 `v-html`）。答案本地按 `question_id` 保留，**提交失败/过期不清空**，由后端 `issues` 给出清晰过期提示。`READY_TO_CONFIRM` 与 `NEEDS_INPUT`（含 `error_code` 生成失败）分别展示。

## 验证（实测）

`./scripts/ci_gate.sh` → `==> gate passed`

| 目标 | 命令 | 结果 |
|---|---|---|
| 平台后端 | `uv run --locked pytest` | **258 passed**（R04 后 252，本步 +6） |
| 平台后端静态 | `ruff check .` | All checks passed |
| 平台前端 | `npm ci + type-check + build + test` | **33 passed**（+5），type-check/build 通过 |
| 独立 Runner | `pytest / ruff` | 26 passed, 2 skipped；All checks passed |

后端覆盖：两轮问答 → `READY_TO_CONFIRM` 且状态持久化（离开再回来一致）；空 answers 幂等（仅一次生成）；过期答案 → `issues` 且停在当前轮；未开始就提交 → 409；属主/CSRF/无草稿 → 401/403/404。
前端覆盖：NEW → 开始；单选/多选/文本/自定义/不确定 → 正确构造 answers；过期 → 提示且保留作答；生成失败 → 独立提示。真实调用：**无**（测试全用假客户端）。

## WhenDone 逐项判定

| 判据 | 结果 |
|---|---|
| 可完成至少两轮问答、离开并回来接续 | ✅ PASS（后端持久化 + 前端重载） |
| 过期题目回答有清晰提示 | ✅ PASS（`stale-alert` + 后端 issues） |
| 构建与交互测试通过 | ✅ PASS（ui 33 passed；type-check/build 通过） |
| ≤3 题 / 业务语言 / 前端不猜题型 / 分别展示失败 | ✅ 均满足 |

## 风险 / 下一步

- **前端未做真实浏览器端到端**：交互由组件测试覆盖；如需可在 R26 矩阵回归时补浏览器验证。
- **下一步 R06**：冻结验收场景与规格修改版本（`pending` 清零、按用户确认冻结、篡改验收断言被拒）。
