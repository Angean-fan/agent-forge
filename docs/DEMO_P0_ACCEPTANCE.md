# Demo P0 验收报告（R27｜正式 9 次独立生成评估）

生成时间：2026-10-13T10:37:46.857855+00:00
运行环境说明：真实 Runner：http://127.0.0.1:9000；共享根：/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared；批次：r27-0d261eb30fee；Profile：demo-ts-mp@0.4；运行期断网；方法索引快照：16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341

## 发布判定

- 通过（全部必需检查 PASS）次数：**9 / 9**（门槛 ≥ 7）
- 校园活动成功：**是**
- 结论：**达到文档建议验收目标**
- 首次通过：9；修复后通过：0；未通过：0；NOT_RUN/暂停/取消：0

## 失败分布

| 失败码或状态 | 次数 |
|---|---:|
| 无 | 0 |

## 逐次结果

| 用例 | 第几次 | run | 判定 | 耗时 | 失败码 | 修复次数 | Agent 调用 | 说明 |
|---|---|---|---|---|---|---|---|---|
| campus | 1 | run #1 | PASS | 32.9s | — | 0 | 0 | — |
| campus | 2 | run #1 | PASS | 39.2s | — | 0 | 0 | — |
| campus | 3 | run #1 | PASS | 31.4s | — | 0 | 0 | — |
| task | 1 | run #1 | PASS | 25.9s | — | 0 | 0 | — |
| task | 2 | run #1 | PASS | 25.5s | — | 0 | 0 | — |
| task | 3 | run #1 | PASS | 27.4s | — | 0 | 0 | — |
| lending | 1 | run #1 | PASS | 36.2s | — | 0 | 0 | — |
| lending | 2 | run #1 | PASS | 33.2s | — | 0 | 0 | — |
| lending | 3 | run #1 | PASS | 30.1s | — | 0 | 0 | — |

## 费用与延迟

- 成功项目平均成本：0 微元（≈0.0000 元）
- 所有尝试总费用 / 成功次数：0 微元（≈0.0000 元）（总费用 0 微元（≈0.0000 元））
- 平均耗时：31.3s

## 分角色模型用量（DeepSeek 交互 / Qwen 规划·编码·升级）

| 角色 | 调用 | 输入 tokens | 输出 tokens | 成本 | 未知 |
|---|---|---|---|---|---|
| user_interaction | 0 | 0 | 0 | 0 | 0 |
| technical_planning | 0 | 0 | 0 | 0 | 0 |
| coding | 0 | 0 | 0 | 0 | 0 |
| complex_repair | 0 | 0 | 0 | 0 | 0 |

## 资产覆盖四层指标（关键词命中不算可复用覆盖）

- 检索命中 run 数：9
- 兼容通过 run 数：9
- 实际复用 run 数：9
- 验收通过 run 数：9

| 资产类型 | 检索命中 run | 兼容通过 run | 实际复用 run | 验收通过 run |
|---|---:|---:|---:|---:|
| Method Unit | 0 | 0 | 0 | 0 |
| Capability Pack | 9 | 9 | 9 | 9 |

> 检索与兼容层取自 asset-search 候选审计；实际复用取自资产锁；验收通过需同时满足复用和全部必需检查。

## 各用例汇总

| 用例 | 次数 | 通过 |
|---|---|---|
| campus | 3 | 3 |
| task | 3 | 3 |
| lending | 3 | 3 |

## 模型路由断言

| 角色 | 期望(provider, model) | 实际(provider, model) | 结果 |
|---|---|---|---|
| user_interaction | ('deepseek_official', 'deepseek-flash') | ('deepseek_official', 'deepseek-flash') | OK |
| technical_planning | ('dashscope', 'qwen3.7-plus') | ('dashscope', 'qwen3.7-plus') | OK |
| coding | ('dashscope', 'qwen3-coder-flash') | ('dashscope', 'qwen3-coder-flash') | OK |
| complex_repair | ('dashscope', 'qwen3.8-max') | ('dashscope', 'qwen3.8-max') | OK |

## 可追溯证据（每次 run）

| 用例 | 第几次 | 运行目录 | 索引快照 | 已锁方法 | 被拒资产数 | 复用:Patch |
|---|---|---|---|---|---|---|
| campus | 1 | `/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/campus-1/run-1` | 16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341 | 0 | 5 | 确定性装配 |
| campus | 2 | `/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/campus-2/run-1` | 16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341 | 0 | 5 | 确定性装配 |
| campus | 3 | `/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/campus-3/run-1` | 16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341 | 0 | 5 | 确定性装配 |
| task | 1 | `/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/task-1/run-1` | 16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341 | 0 | 5 | 确定性装配 |
| task | 2 | `/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/task-2/run-1` | 16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341 | 0 | 5 | 确定性装配 |
| task | 3 | `/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/task-3/run-1` | 16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341 | 0 | 5 | 确定性装配 |
| lending | 1 | `/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/lending-1/run-1` | 16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341 | 0 | 5 | 确定性装配 |
| lending | 2 | `/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/lending-2/run-1` | 16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341 | 0 | 5 | 确定性装配 |
| lending | 3 | `/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/lending-3/run-1` | 16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341 | 0 | 5 | 确定性装配 |

## 每次 run 的逐项证据

### campus 第 1 次

- 运行目录：`/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/campus-1/run-1`；run ID：1
- 状态：SUCCEEDED；全部必需项通过：是；首次通过：是；修复次数：0；Agent 调用：0
- 构建：PASS；HTTP 验收：PASS；耗时：32.9s；已结算成本：0 微元（≈0.0000 元）；未知调用：0
- 索引快照：`16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341`；已锁 Method Unit：无；已锁 Capability Pack：task-crud-backend@0.4.0, task-crud-frontend@0.2.0
- 检索候选未声明覆盖的验收 ID：AC01, AC02, AC03, AC04
- 候选 Method Unit：0；兼容通过：0；候选 Capability Pack：11；兼容通过：6；Method Unit 复用路径：否；Patch：否
- 首次失败快照：`无`
- 说明：—

| 必需构建检查 | 结果 | 失败码 |
|---|---|---|
| static_manifest | PASS | — |
| backend_test | PASS | — |
| backend_build | PASS | — |
| frontend_build | PASS | — |

| 固定业务验收 | 结果 | 失败码 |
|---|---|---|
| build_backend | PASS | — |
| static_jar | PASS | — |
| mysql_digest | PASS | — |
| mysql_ready | PASS | — |
| backend_startup | PASS | — |
| login_admin | PASS | — |
| login_member | PASS | — |
| campus_admin_publish | PASS | — |
| campus_member_cannot_publish | PASS | — |
| campus_enroll | PASS | — |
| campus_no_duplicate | PASS | — |
| campus_list_total | PASS | — |
| campus_cross_user_denied | PASS | — |
| campus_persists | PASS | — |
| campus_closed_rejected | PASS | — |

| 模型角色 | Provider / 模型 | 思考模式 / 强度 | 输入 / 输出 tokens | 成本（微元） | 状态 |
|---|---|---|---|---|---|
| — | — | — | 0 / 0 | 0 | 无调用 |

| 被拒资产 | 原因 |
|---|---|
| capability-pack:task-crud-backend-mb@0.1.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.1.1 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.2.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.3.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend@0.1.3 | profile_incompatible |

### campus 第 2 次

- 运行目录：`/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/campus-2/run-1`；run ID：1
- 状态：SUCCEEDED；全部必需项通过：是；首次通过：是；修复次数：0；Agent 调用：0
- 构建：PASS；HTTP 验收：PASS；耗时：39.2s；已结算成本：0 微元（≈0.0000 元）；未知调用：0
- 索引快照：`16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341`；已锁 Method Unit：无；已锁 Capability Pack：task-crud-backend@0.4.0, task-crud-frontend@0.2.0
- 检索候选未声明覆盖的验收 ID：AC01, AC02, AC03, AC04
- 候选 Method Unit：0；兼容通过：0；候选 Capability Pack：11；兼容通过：6；Method Unit 复用路径：否；Patch：否
- 首次失败快照：`无`
- 说明：—

| 必需构建检查 | 结果 | 失败码 |
|---|---|---|
| static_manifest | PASS | — |
| backend_test | PASS | — |
| backend_build | PASS | — |
| frontend_build | PASS | — |

| 固定业务验收 | 结果 | 失败码 |
|---|---|---|
| build_backend | PASS | — |
| static_jar | PASS | — |
| mysql_digest | PASS | — |
| mysql_ready | PASS | — |
| backend_startup | PASS | — |
| login_admin | PASS | — |
| login_member | PASS | — |
| campus_admin_publish | PASS | — |
| campus_member_cannot_publish | PASS | — |
| campus_enroll | PASS | — |
| campus_no_duplicate | PASS | — |
| campus_list_total | PASS | — |
| campus_cross_user_denied | PASS | — |
| campus_persists | PASS | — |
| campus_closed_rejected | PASS | — |

| 模型角色 | Provider / 模型 | 思考模式 / 强度 | 输入 / 输出 tokens | 成本（微元） | 状态 |
|---|---|---|---|---|---|
| — | — | — | 0 / 0 | 0 | 无调用 |

| 被拒资产 | 原因 |
|---|---|
| capability-pack:task-crud-backend-mb@0.1.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.1.1 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.2.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.3.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend@0.1.3 | profile_incompatible |

### campus 第 3 次

- 运行目录：`/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/campus-3/run-1`；run ID：1
- 状态：SUCCEEDED；全部必需项通过：是；首次通过：是；修复次数：0；Agent 调用：0
- 构建：PASS；HTTP 验收：PASS；耗时：31.4s；已结算成本：0 微元（≈0.0000 元）；未知调用：0
- 索引快照：`16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341`；已锁 Method Unit：无；已锁 Capability Pack：task-crud-backend@0.4.0, task-crud-frontend@0.2.0
- 检索候选未声明覆盖的验收 ID：AC01, AC02, AC03, AC04
- 候选 Method Unit：0；兼容通过：0；候选 Capability Pack：11；兼容通过：6；Method Unit 复用路径：否；Patch：否
- 首次失败快照：`无`
- 说明：—

| 必需构建检查 | 结果 | 失败码 |
|---|---|---|
| static_manifest | PASS | — |
| backend_test | PASS | — |
| backend_build | PASS | — |
| frontend_build | PASS | — |

| 固定业务验收 | 结果 | 失败码 |
|---|---|---|
| build_backend | PASS | — |
| static_jar | PASS | — |
| mysql_digest | PASS | — |
| mysql_ready | PASS | — |
| backend_startup | PASS | — |
| login_admin | PASS | — |
| login_member | PASS | — |
| campus_admin_publish | PASS | — |
| campus_member_cannot_publish | PASS | — |
| campus_enroll | PASS | — |
| campus_no_duplicate | PASS | — |
| campus_list_total | PASS | — |
| campus_cross_user_denied | PASS | — |
| campus_persists | PASS | — |
| campus_closed_rejected | PASS | — |

| 模型角色 | Provider / 模型 | 思考模式 / 强度 | 输入 / 输出 tokens | 成本（微元） | 状态 |
|---|---|---|---|---|---|
| — | — | — | 0 / 0 | 0 | 无调用 |

| 被拒资产 | 原因 |
|---|---|
| capability-pack:task-crud-backend-mb@0.1.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.1.1 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.2.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.3.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend@0.1.3 | profile_incompatible |

### task 第 1 次

- 运行目录：`/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/task-1/run-1`；run ID：1
- 状态：SUCCEEDED；全部必需项通过：是；首次通过：是；修复次数：0；Agent 调用：0
- 构建：PASS；HTTP 验收：PASS；耗时：25.9s；已结算成本：0 微元（≈0.0000 元）；未知调用：0
- 索引快照：`16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341`；已锁 Method Unit：无；已锁 Capability Pack：task-crud-backend@0.4.0, task-crud-frontend@0.2.0
- 检索候选未声明覆盖的验收 ID：AC01, AC02
- 候选 Method Unit：0；兼容通过：0；候选 Capability Pack：11；兼容通过：6；Method Unit 复用路径：否；Patch：否
- 首次失败快照：`无`
- 说明：—

| 必需构建检查 | 结果 | 失败码 |
|---|---|---|
| static_manifest | PASS | — |
| backend_test | PASS | — |
| backend_build | PASS | — |
| frontend_build | PASS | — |

| 固定业务验收 | 结果 | 失败码 |
|---|---|---|
| build_backend | PASS | — |
| static_jar | PASS | — |
| mysql_digest | PASS | — |
| mysql_ready | PASS | — |
| backend_startup | PASS | — |
| login_admin | PASS | — |
| login_member | PASS | — |
| create_task | PASS | — |
| list_total | PASS | — |
| get_own | PASS | — |
| update_own | PASS | — |
| search | PASS | — |
| pagination_page_zero | PASS | — |
| pagination_size_too_large | PASS | — |
| cross_user_denied | PASS | — |
| delete_own | PASS | — |

| 模型角色 | Provider / 模型 | 思考模式 / 强度 | 输入 / 输出 tokens | 成本（微元） | 状态 |
|---|---|---|---|---|---|
| — | — | — | 0 / 0 | 0 | 无调用 |

| 被拒资产 | 原因 |
|---|---|
| capability-pack:task-crud-backend-mb@0.1.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.1.1 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.2.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.3.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend@0.1.3 | profile_incompatible |

### task 第 2 次

- 运行目录：`/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/task-2/run-1`；run ID：1
- 状态：SUCCEEDED；全部必需项通过：是；首次通过：是；修复次数：0；Agent 调用：0
- 构建：PASS；HTTP 验收：PASS；耗时：25.5s；已结算成本：0 微元（≈0.0000 元）；未知调用：0
- 索引快照：`16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341`；已锁 Method Unit：无；已锁 Capability Pack：task-crud-backend@0.4.0, task-crud-frontend@0.2.0
- 检索候选未声明覆盖的验收 ID：AC01, AC02
- 候选 Method Unit：0；兼容通过：0；候选 Capability Pack：11；兼容通过：6；Method Unit 复用路径：否；Patch：否
- 首次失败快照：`无`
- 说明：—

| 必需构建检查 | 结果 | 失败码 |
|---|---|---|
| static_manifest | PASS | — |
| backend_test | PASS | — |
| backend_build | PASS | — |
| frontend_build | PASS | — |

| 固定业务验收 | 结果 | 失败码 |
|---|---|---|
| build_backend | PASS | — |
| static_jar | PASS | — |
| mysql_digest | PASS | — |
| mysql_ready | PASS | — |
| backend_startup | PASS | — |
| login_admin | PASS | — |
| login_member | PASS | — |
| create_task | PASS | — |
| list_total | PASS | — |
| get_own | PASS | — |
| update_own | PASS | — |
| search | PASS | — |
| pagination_page_zero | PASS | — |
| pagination_size_too_large | PASS | — |
| cross_user_denied | PASS | — |
| delete_own | PASS | — |

| 模型角色 | Provider / 模型 | 思考模式 / 强度 | 输入 / 输出 tokens | 成本（微元） | 状态 |
|---|---|---|---|---|---|
| — | — | — | 0 / 0 | 0 | 无调用 |

| 被拒资产 | 原因 |
|---|---|
| capability-pack:task-crud-backend-mb@0.1.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.1.1 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.2.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.3.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend@0.1.3 | profile_incompatible |

### task 第 3 次

- 运行目录：`/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/task-3/run-1`；run ID：1
- 状态：SUCCEEDED；全部必需项通过：是；首次通过：是；修复次数：0；Agent 调用：0
- 构建：PASS；HTTP 验收：PASS；耗时：27.4s；已结算成本：0 微元（≈0.0000 元）；未知调用：0
- 索引快照：`16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341`；已锁 Method Unit：无；已锁 Capability Pack：task-crud-backend@0.4.0, task-crud-frontend@0.2.0
- 检索候选未声明覆盖的验收 ID：AC01, AC02
- 候选 Method Unit：0；兼容通过：0；候选 Capability Pack：11；兼容通过：6；Method Unit 复用路径：否；Patch：否
- 首次失败快照：`无`
- 说明：—

| 必需构建检查 | 结果 | 失败码 |
|---|---|---|
| static_manifest | PASS | — |
| backend_test | PASS | — |
| backend_build | PASS | — |
| frontend_build | PASS | — |

| 固定业务验收 | 结果 | 失败码 |
|---|---|---|
| build_backend | PASS | — |
| static_jar | PASS | — |
| mysql_digest | PASS | — |
| mysql_ready | PASS | — |
| backend_startup | PASS | — |
| login_admin | PASS | — |
| login_member | PASS | — |
| create_task | PASS | — |
| list_total | PASS | — |
| get_own | PASS | — |
| update_own | PASS | — |
| search | PASS | — |
| pagination_page_zero | PASS | — |
| pagination_size_too_large | PASS | — |
| cross_user_denied | PASS | — |
| delete_own | PASS | — |

| 模型角色 | Provider / 模型 | 思考模式 / 强度 | 输入 / 输出 tokens | 成本（微元） | 状态 |
|---|---|---|---|---|---|
| — | — | — | 0 / 0 | 0 | 无调用 |

| 被拒资产 | 原因 |
|---|---|
| capability-pack:task-crud-backend-mb@0.1.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.1.1 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.2.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.3.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend@0.1.3 | profile_incompatible |

### lending 第 1 次

- 运行目录：`/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/lending-1/run-1`；run ID：1
- 状态：SUCCEEDED；全部必需项通过：是；首次通过：是；修复次数：0；Agent 调用：0
- 构建：PASS；HTTP 验收：PASS；耗时：36.2s；已结算成本：0 微元（≈0.0000 元）；未知调用：0
- 索引快照：`16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341`；已锁 Method Unit：无；已锁 Capability Pack：task-crud-backend@0.4.0, task-crud-frontend@0.2.0
- 检索候选未声明覆盖的验收 ID：AC01, AC02, AC03
- 候选 Method Unit：0；兼容通过：0；候选 Capability Pack：11；兼容通过：6；Method Unit 复用路径：否；Patch：否
- 首次失败快照：`无`
- 说明：—

| 必需构建检查 | 结果 | 失败码 |
|---|---|---|
| static_manifest | PASS | — |
| backend_test | PASS | — |
| backend_build | PASS | — |
| frontend_build | PASS | — |

| 固定业务验收 | 结果 | 失败码 |
|---|---|---|
| build_backend | PASS | — |
| static_jar | PASS | — |
| mysql_digest | PASS | — |
| mysql_ready | PASS | — |
| backend_startup | PASS | — |
| login_admin | PASS | — |
| login_member | PASS | — |
| lending_admin_creates_book | PASS | — |
| lending_member_cannot_create_book | PASS | — |
| lending_member_sees_book | PASS | — |
| lending_member_lists_books | PASS | — |
| lending_borrow | PASS | — |
| lending_book_marked_borrowed | PASS | — |
| lending_duplicate_rejected | PASS | — |
| lending_admin_cannot_reset_borrowed_book | PASS | — |
| lending_admin_cannot_delete_borrowed_book | PASS | — |
| lending_member_reads_own | PASS | — |
| lending_cross_user_denied | PASS | — |
| lending_list_total | PASS | — |
| lending_pagination_limit | PASS | — |
| lending_return | PASS | — |
| lending_return_releases_book | PASS | — |
| lending_borrow_after_return | PASS | — |

| 模型角色 | Provider / 模型 | 思考模式 / 强度 | 输入 / 输出 tokens | 成本（微元） | 状态 |
|---|---|---|---|---|---|
| — | — | — | 0 / 0 | 0 | 无调用 |

| 被拒资产 | 原因 |
|---|---|
| capability-pack:task-crud-backend-mb@0.1.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.1.1 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.2.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.3.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend@0.1.3 | profile_incompatible |

### lending 第 2 次

- 运行目录：`/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/lending-2/run-1`；run ID：1
- 状态：SUCCEEDED；全部必需项通过：是；首次通过：是；修复次数：0；Agent 调用：0
- 构建：PASS；HTTP 验收：PASS；耗时：33.2s；已结算成本：0 微元（≈0.0000 元）；未知调用：0
- 索引快照：`16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341`；已锁 Method Unit：无；已锁 Capability Pack：task-crud-backend@0.4.0, task-crud-frontend@0.2.0
- 检索候选未声明覆盖的验收 ID：AC01, AC02, AC03
- 候选 Method Unit：0；兼容通过：0；候选 Capability Pack：11；兼容通过：6；Method Unit 复用路径：否；Patch：否
- 首次失败快照：`无`
- 说明：—

| 必需构建检查 | 结果 | 失败码 |
|---|---|---|
| static_manifest | PASS | — |
| backend_test | PASS | — |
| backend_build | PASS | — |
| frontend_build | PASS | — |

| 固定业务验收 | 结果 | 失败码 |
|---|---|---|
| build_backend | PASS | — |
| static_jar | PASS | — |
| mysql_digest | PASS | — |
| mysql_ready | PASS | — |
| backend_startup | PASS | — |
| login_admin | PASS | — |
| login_member | PASS | — |
| lending_admin_creates_book | PASS | — |
| lending_member_cannot_create_book | PASS | — |
| lending_member_sees_book | PASS | — |
| lending_member_lists_books | PASS | — |
| lending_borrow | PASS | — |
| lending_book_marked_borrowed | PASS | — |
| lending_duplicate_rejected | PASS | — |
| lending_admin_cannot_reset_borrowed_book | PASS | — |
| lending_admin_cannot_delete_borrowed_book | PASS | — |
| lending_member_reads_own | PASS | — |
| lending_cross_user_denied | PASS | — |
| lending_list_total | PASS | — |
| lending_pagination_limit | PASS | — |
| lending_return | PASS | — |
| lending_return_releases_book | PASS | — |
| lending_borrow_after_return | PASS | — |

| 模型角色 | Provider / 模型 | 思考模式 / 强度 | 输入 / 输出 tokens | 成本（微元） | 状态 |
|---|---|---|---|---|---|
| — | — | — | 0 / 0 | 0 | 无调用 |

| 被拒资产 | 原因 |
|---|---|
| capability-pack:task-crud-backend-mb@0.1.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.1.1 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.2.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.3.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend@0.1.3 | profile_incompatible |

### lending 第 3 次

- 运行目录：`/Users/wangkefan/Projects/AgentForge/platform-api/var/r27-shared/r27-0d261eb30fee/lending-3/run-1`；run ID：1
- 状态：SUCCEEDED；全部必需项通过：是；首次通过：是；修复次数：0；Agent 调用：0
- 构建：PASS；HTTP 验收：PASS；耗时：30.1s；已结算成本：0 微元（≈0.0000 元）；未知调用：0
- 索引快照：`16cfe33b3da5ecccb0c1e4e3109c4c8884f825d8eaa98f572e8ebfd95be09341`；已锁 Method Unit：无；已锁 Capability Pack：task-crud-backend@0.4.0, task-crud-frontend@0.2.0
- 检索候选未声明覆盖的验收 ID：AC01, AC02, AC03
- 候选 Method Unit：0；兼容通过：0；候选 Capability Pack：11；兼容通过：6；Method Unit 复用路径：否；Patch：否
- 首次失败快照：`无`
- 说明：—

| 必需构建检查 | 结果 | 失败码 |
|---|---|---|
| static_manifest | PASS | — |
| backend_test | PASS | — |
| backend_build | PASS | — |
| frontend_build | PASS | — |

| 固定业务验收 | 结果 | 失败码 |
|---|---|---|
| build_backend | PASS | — |
| static_jar | PASS | — |
| mysql_digest | PASS | — |
| mysql_ready | PASS | — |
| backend_startup | PASS | — |
| login_admin | PASS | — |
| login_member | PASS | — |
| lending_admin_creates_book | PASS | — |
| lending_member_cannot_create_book | PASS | — |
| lending_member_sees_book | PASS | — |
| lending_member_lists_books | PASS | — |
| lending_borrow | PASS | — |
| lending_book_marked_borrowed | PASS | — |
| lending_duplicate_rejected | PASS | — |
| lending_admin_cannot_reset_borrowed_book | PASS | — |
| lending_admin_cannot_delete_borrowed_book | PASS | — |
| lending_member_reads_own | PASS | — |
| lending_cross_user_denied | PASS | — |
| lending_list_total | PASS | — |
| lending_pagination_limit | PASS | — |
| lending_return | PASS | — |
| lending_return_releases_book | PASS | — |
| lending_borrow_after_return | PASS | — |

| 模型角色 | Provider / 模型 | 思考模式 / 强度 | 输入 / 输出 tokens | 成本（微元） | 状态 |
|---|---|---|---|---|---|
| — | — | — | 0 / 0 | 0 | 无调用 |

| 被拒资产 | 原因 |
|---|---|
| capability-pack:task-crud-backend-mb@0.1.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.1.1 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.2.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend-mb@0.3.0 | profile_incompatible, unmet_requirement:template:mybatis |
| capability-pack:task-crud-backend@0.1.3 | profile_incompatible |


> 说明：本报告只反映**真实运行**结果；缺失或超时的必需测试计为失败；未达标如实标注，不伪造 PASS。
