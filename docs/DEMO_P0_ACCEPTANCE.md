# Demo P0 验收报告（R27｜正式 9 次独立生成评估）

生成时间：2026-10-13T09:24:31.520951+00:00
运行环境说明：Runner 不可达（http://127.0.0.1:9000）——本报告 NOT_RUN，未达到目标。

## 发布判定

- 通过（全部必需检查 PASS）次数：**0 / 9**（门槛 ≥ 7）
- 校园活动成功：**否**
- 结论：**未达到文档建议验收目标（如实标注）**

## 逐次结果

| 用例 | 第几次 | run | 判定 | 耗时 | 失败码 | 修复次数 | Agent 调用 | 说明 |
|---|---|---|---|---|---|---|---|---|
| campus | 1 | — | NOT_RUN | — | — | 0 | 0 | Runner 不可达（http://127.0.0.1:9000）：必需构建/验收未运行，计为失败 |
| campus | 2 | — | NOT_RUN | — | — | 0 | 0 | Runner 不可达（http://127.0.0.1:9000）：必需构建/验收未运行，计为失败 |
| campus | 3 | — | NOT_RUN | — | — | 0 | 0 | Runner 不可达（http://127.0.0.1:9000）：必需构建/验收未运行，计为失败 |
| task | 1 | — | NOT_RUN | — | — | 0 | 0 | Runner 不可达（http://127.0.0.1:9000）：必需构建/验收未运行，计为失败 |
| task | 2 | — | NOT_RUN | — | — | 0 | 0 | Runner 不可达（http://127.0.0.1:9000）：必需构建/验收未运行，计为失败 |
| task | 3 | — | NOT_RUN | — | — | 0 | 0 | Runner 不可达（http://127.0.0.1:9000）：必需构建/验收未运行，计为失败 |
| lending | 1 | — | NOT_RUN | — | — | 0 | 0 | Runner 不可达（http://127.0.0.1:9000）：必需构建/验收未运行，计为失败 |
| lending | 2 | — | NOT_RUN | — | — | 0 | 0 | Runner 不可达（http://127.0.0.1:9000）：必需构建/验收未运行，计为失败 |
| lending | 3 | — | NOT_RUN | — | — | 0 | 0 | Runner 不可达（http://127.0.0.1:9000）：必需构建/验收未运行，计为失败 |

## 费用与延迟

- 成功项目平均成本：不可计算
- 所有尝试总费用 / 成功次数：不可计算（总费用 0 微元（≈0.0000 元））
- 平均耗时：—

## 分角色模型用量（DeepSeek 交互 / Qwen 规划·编码·升级）

| 角色 | 调用 | 输入 tokens | 输出 tokens | 成本 | 未知 |
|---|---|---|---|---|---|

## 资产覆盖四层指标（关键词命中不算可复用覆盖）

- 检索命中 run 数：0
- 兼容通过 run 数：0
- 实际复用 run 数：0
- 验收通过 run 数：0

## 各用例汇总

| 用例 | 次数 | 通过 |
|---|---|---|
| campus | 3 | 0 |
| task | 3 | 0 |
| lending | 3 | 0 |

## 模型路由断言

| 角色 | 期望(provider, model) | 实际(provider, model) | 结果 |
|---|---|---|---|
| user_interaction | ('deepseek_official', 'deepseek-flash') | ('deepseek_official', 'deepseek-flash') | OK |
| technical_planning | ('dashscope', 'qwen3.7-plus') | ('dashscope', 'qwen3.7-plus') | OK |
| coding | ('dashscope', 'qwen3-coder-flash') | ('dashscope', 'qwen3-coder-flash') | OK |
| complex_repair | ('dashscope', 'qwen3.8-max') | ('dashscope', 'qwen3.8-max') | OK |

## 可追溯证据（每次 run）

| 用例 | run | 索引快照 | 已锁方法 | 拒绝原因数 | 复用:Patch |
|---|---|---|---|---|---|
| campus | None | — | 0 | 0 | 确定性装配 |
| campus | None | — | 0 | 0 | 确定性装配 |
| campus | None | — | 0 | 0 | 确定性装配 |
| task | None | — | 0 | 0 | 确定性装配 |
| task | None | — | 0 | 0 | 确定性装配 |
| task | None | — | 0 | 0 | 确定性装配 |
| lending | None | — | 0 | 0 | 确定性装配 |
| lending | None | — | 0 | 0 | 确定性装配 |
| lending | None | — | 0 | 0 | 确定性装配 |

> 说明：本报告只反映**真实运行**结果；缺失或超时的必需测试计为失败；未达标如实标注，不伪造 PASS。
