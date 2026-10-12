# R02｜建立访谈题目与答案 Schema

日期：2026-10-12
状态：完成（纯 schema + 确定性校验；不调用模型）
前置：R01（`bbf6b59`）；迭代文档第一部分 R02。

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：定义 `question_id/type/prompt/options/allow_custom/required/affects/version` 及回答协议，支持单选、多选、文本、自定义与"不确定"。
- **Context**：依赖 R01 与 MVP M06/M07；需求文档 FR02、技术文档第 5 节问答契约；R03（模型生成问题）与 R05（前端卡片）依赖本 schema。注：M22 现有 `questions_json` 仅为**字符串列表**，本步建立结构化规范。
- **Constraints**：答案必须绑定题目版本；`affects` 明确本问题改变哪项业务决定；题面使用业务语言（不出现 ORM/框架术语）；**本步不调用模型**。
- **WhenDone**：题型与非法输入均有 Pydantic 校验；旧版题目答案无法写入新版本；schema 测试通过。

## 输入 / 输出契约

- **输入**：结构化 `Question`（含 `question_id`/`version`/`type`/`prompt`/`options`/`allow_custom`/`required`/`affects`）与 `Answer`（`question_id`/`question_version`/`selected`/`custom`/`unsure`）。
- **输出**：
  - `validate_answer(question, answer) -> list[str]`：单条合法性（空 = 合法）。**版本不匹配即拒绝**。
  - `validate_submission(questions, submission) -> {"issues", "resolved", "pending"}`：整份提交的分类结果；"不确定"与未作答的必填题留在 `pending`。
  - Pydantic 校验：题型与选项一致性、`affects` 白名单、题面长度/业务语言、`question_id` 安全、`version ≥ 1`、每轮 ≤3 题、轮次 ≤4。

## 变更文件

```
platform-api/
├── src/agentforge_platform/interview.py   # 新增：QuestionType/Question/QuestionBatch/Answer/AnswerSubmission + validate_answer/validate_submission
└── tests/test_interview.py                # 新增：15 例
```

未改动 M22 提取、spec、API 或迁移；无新表（持久化/thread 归 R04）。

## 设计

- **题型**：`single_choice`/`multi_choice`/`text`；文本题 `options` 必须为空，选择题必须给出**去重非空**选项。
- **affects**：取值限定为 spec 业务区块键 `goal/actors/entities/fields/relations/pages/permissions/business_rules/acceptance_cases/constraints/exclusions`，非空、去重；未知键即拒。
- **业务语言**：题面校验拒绝框架/ORM/技术选型术语（`MyBatis/Hibernate/ORM/JPA/Spring/Vue/React/Angular/JWT/Redis/主键/外键/框架`）。
- **答案协议**：`selected`（选项）+ `custom`（自定义/文本）+ `unsure`（不确定）；单选 ≤1 项且不得同时给自定义；自定义需 `allow_custom`；`unsure` 不得与选择/自定义并存。
- **版本绑定**：`Answer.question_version` 必须等于当前 `Question.version`，否则该答案判为过期、不入 `resolved`——旧版题目答案无法写入新版本。
- **批次上限**：`QuestionBatch` 一轮 ≤3 题（`questions` 长度约束）且 `round ∈ [1,4]`，同轮 `question_id` 唯一。

## 验证（实测）

`./scripts/ci_gate.sh` → `==> gate passed`

| 目标 | 命令 | 结果 |
|---|---|---|
| 平台后端 | `uv run --locked pytest` | **234 passed**（R01 后 219，本步 +15） |
| 平台后端静态 | `uv run --locked ruff check .` | All checks passed |
| 平台前端 | `npm ci + type-check + build + test` | **28 passed** |
| 独立 Runner | `pytest / ruff` | 26 passed, 2 skipped；All checks passed |

`test_interview.py` 覆盖：单选/多选/文本合法构造；文本带选项、选择题无选项、选项重复/空串 → 拒绝；`affects` 空/未知 → 拒绝、去重；题面空/超长/含技术术语 → 拒绝；`question_id` 不安全、`version<1` → 拒绝；轮次 5、每轮 4 题、同轮重复 id → 拒绝；单选多选/越界选项/不允许自定义/文本带选项/不确定带答案 → 拒绝；**版本不匹配 → 拒绝且不 resolved**；未知题目 → issue；必填未答/不确定 → `pending`。真实模型：**未调用**（本步无模型）。

## WhenDone 逐项判定

| 判据 | 结果 |
|---|---|
| 题型与非法输入均有 Pydantic 校验 | ✅ PASS |
| 旧版题目答案无法写入新版本 | ✅ PASS（`validate_answer` 版本不匹配拒绝；`test_answer_binding_version_mismatch_is_rejected`） |
| schema 测试通过 | ✅ PASS（15 例） |
| 答案绑定题目版本 / affects 明确 / 业务语言 / 不调用模型 | ✅ 均满足 |

## 风险 / 下一步

- **R03 衔接**：R03 的 DeepSeek 输出须落到本 `Question`；模型可能给出技术术语题面，届时由 `Question` 校验拒绝并触发结构纠正（复用 M22 的重试范式）。
- **持久化**：本步未加 `interview` 表；R04（LangGraph interrupt/checkpointer）负责轮次与答案的持久化与 thread 绑定。
- **下一步 R03**：依赖本 schema；需真实 DeepSeek 调用（`user_interaction → deepseek-flash`，计费），按既有约定需开发者确认后再实跑，或先以离线探针/模拟响应验证结构。
