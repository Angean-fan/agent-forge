# M06｜定义版本化 Software Specification Schema

日期：2026-10-11
状态：完成

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：用 Pydantic 定义 `SoftwareSpecification` 及 `Entity/Field/Permission/BusinessRule/AcceptanceCase` 的最小可扩展结构。
- **Context**：依赖 M05；技术文档第 4 节要求全部契约字段；后续按此 schema 生成。
- **Constraints**：MVP 可让暂不使用的字段为空数组，但不得删去契约字段；来源标注 `user_explicit/user_confirmed/pending`；只支持 1 个业务实体、固定任务管理 Profile；不接受超范围输入后静默裁剪。
- **WhenDone**：已有可通过校验的 `task-management.spec.json` 样例；缺实体/权限/关键规则或非法 Profile 不能被误判为可确认；schema 单元测试通过。

## 新增/变更文件

```
platform-api/
├── src/agentforge_platform/spec.py     # schema 模型 + 可确认性校验
└── tests/test_spec.py                  # 12 例
schemas/
└── task-management.spec.json           # 可校验、可确认的样例
```

## Schema 设计

- **契约字段（16 个，均在 `SoftwareSpecification` 中，未删减）**：`schema_version / project_type / goal / actors / entities / relations / permissions / business_rules / acceptance_cases / technical_profile / constraints / exclusions / capability_requests / recipe_requests / integrations / external_requirements`。
- **命名子模型（Goal 指定）**：`Entity`、`Field`、`Permission`、`BusinessRule`、`AcceptanceCase`；另有 `TechnicalProfile`。其余字段（relations、capability/recipe/integrations/external_requirements）MVP 暂不定义内部结构，用 `list[dict]` 占位，避免过早固化形状。
- **来源标注**：`Source` 枚举 `user_explicit / user_confirmed / pending`，默认 `pending`（保守）；用于 `Entity / Permission / BusinessRule / AcceptanceCase`。
- **严格性**：全部模型 `extra="forbid"`，未知字段被拒绝而非静默丢弃；`schema_version` 校验为受支持版本（MVP 仅 `0.1`）。
- **固定 Profile 常量**：`MVP_PROFILE_ID="demo-ts-mp"`、`MVP_PROFILE_VERSION="0.1"`（取自技术文档第 4 节示例；M08 落地 Profile Registry 后改由其提供）。
- **单实体上限**：`MVP_ENTITY_LIMIT=1`。

## 可确认性校验

`confirmation_issues(spec)` 返回阻止确认的问题（空 = 可确认），覆盖：

1. 实体数为 0 → `缺少业务实体`；实体数 > 1 → `MVP 仅支持 1 个业务实体…`（超范围，不裁剪只拒绝）。
2. 权限为空 → `缺少角色权限定义`。
3. 业务规则为空 → `缺少关键业务规则`。
4. `technical_profile` 非固定 Profile → `不支持的技术 Profile…`。
5. 关键 `pending` 非零（实体/权限/规则/验收场景）→ `存在未确认的 pending 项…`。

`is_confirmable(spec)` = 无问题项。

## 实际执行的测试与结果

1. `uv run pytest -v` → **48 passed**（M01–M05 36 + M06 12）。
2. `uv run ruff check .` → **All checks passed**。
3. 覆盖点：样例通过校验且可确认、含全部 16 契约字段；缺实体/权限/规则/非法 Profile/多实体/pending 均**不可确认**且问题指向对应项；非法 `source` 值、未知顶层字段、非支持 `schema_version`、缺必需契约字段均抛 `ValidationError`。
4. 未运行项：无。

## 剩余风险 / 阻塞

1. **Profile 常量待 M08 对齐**：`demo-ts-mp@0.1` 目前为代码常量；M08 建立 Profile Registry 后应改为读取注册表，避免两处定义漂移。
2. **`relations` 等字段用 `list[dict]` 占位**：MVP 未定义其内部结构；如需结构化，待对应步骤再定，避免过早固化。
3. 无阻塞。

## WhenDone 逐项判定

| 条目 | 判定 | 依据 |
|---|---|---|
| 有可通过校验的 `task-management.spec.json` 样例 | 满足 | `schemas/task-management.spec.json` 校验通过且 `is_confirmable` 为真 |
| 缺实体/权限/关键规则/非法 Profile 不可被判为可确认 | 满足 | 对应测试均断言 `not is_confirmable` 且指向具体问题 |
| schema 单元测试通过 | 满足 | `tests/test_spec.py` 12 例实跑通过 |
