# R16｜核对并接入 DeepAgent 的实际工具集

日期：2026-10-13
状态：完成（受限 patch engine 落地；北京地域 Function Calling 探针实测通过）
前置：R15（`c36c69a`）；迭代文档 D3 的 R16；技术文档第 1/6/9 节（`create_deep_agent` 是 fallback，不是独立模型）。

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：用**经兼容性验证**的 `deepagents` 构造单任务 patch engine，接入 **Qwen3-Coder-Flash（`qwen3-coder-flash`）**，
  验证所选地域的工具调用、backend、账本与输出协议。
- **Context**：依赖 M21 跨 Provider 网关、R15 文件 backend；`create_deep_agent` 是 fallback patch engine；coding 角色指定百炼 `qwen3-coder-flash`。
- **Constraints**：**先对实际地域执行 Function Calling 探针**（不支持则 `NOT_RUN/阻塞`，不假装有工具能力、不擅自换型号）；
  审计真实工具表，禁止 shell/任意网络/未受控委派；每个内部模型请求继承 `run/task/call_id` 独立预留结算，不只统计外层 invoke。
- **WhenDone**：`qwen3-coder-flash` 在目标地域完成真实受限工具调用与暂存写入；危险工具/越界均被拒；内部所有调用入账。

## 输入 / 输出契约

- **`build_patch_engine(*, ledger_binding, file_backend, system_prompt, model_name=None, model=None, extra_tools=()) -> PatchEngine`**（`patch_engine.py`）：
  - 内部构造 coding 账本模型（`build_coding_model`）→ 注册受限 harness profile → 装配 `FilesystemMiddleware(tools=RESTRICTED_TOOLS, backend=StagingBackend)` → `create_deep_agent(...)` → **审计真实工具表**。
  - `RESTRICTED_TOOLS = (ls, read_file, write_file, edit_file, glob, grep)`；`FORBIDDEN_TOOL_NAMES = {execute, task}`。
  - `PatchEngine.run(prompt, recursion_limit=40) -> {messages, change_set}`：一次 invoke 产出**待提交**变更集（不自动提交，提交/复验属 R17）。
- **`StagingBackend(FileBackend)`**：deepagents 文件工具 → 当前 run。**读**＝授权源码叠加暂存视图（越界/遍历即时拒）；
  **写**＝经 R15 `FileBackend` 落到 run 专属暂存，受 `allowed_paths` 与受保护清单约束，**不碰正式源码**。
- **`LedgerChatModel(ChatOpenAI)`**：每次 `_generate/_agenerate` 先 `model_gateway.reserve` 后 `settle`，
  携带独立 `call_id` 并继承 `run_id/task_id`；预算不足在**发起供应商请求之前**抛出。
- **coding 角色门禁**：`model_gateway._role_enabled("coding")` 由 `AGENTFORGE_CODING_ENABLED` 显式开启（默认关闭）；
  地域端点由 `AGENTFORGE_DASHSCOPE_BASE_URL` 指定，模型名由 `AGENTFORGE_CODING_MODEL` 指定（默认 `qwen3-coder-flash`）。

## 变更文件

```
platform-api/
├── pyproject.toml / uv.lock        # + deepagents==0.7.24 / langchain==1.4.4 / langchain-openai==1.7.0（langchain-core 1.6.9 不变）
├── src/agentforge_platform/
│   ├── patch_engine.py             # 新增：StagingBackend / LedgerChatModel / PatchEngine / 工具表审计
│   └── model_gateway.py            # + coding 角色环境变量开关
└── tests/test_r16_patch_engine.py  # 新增：11 例（免费层）
scripts/coding_tools_probe.py       # 新增：真实 Function Calling 探针（计费，默认不随门禁运行）
```

## 设计

- **受限工具表 + 构造后审计**：只暴露 6 个文件工具；`execute`（shell）与 `task`（未受控子代理委派）显式移除
  （`FilesystemMiddleware(tools=...)` 白名单 + harness profile `excluded_tools={execute}` 与 `general_purpose_subagent(enabled=False)`）。
  `create_deep_agent` 后读取 `agent.nodes["tools"].bound.tools_by_name` 复核真实工具表，出现 shell/委派/白名单之外的工具即**拒绝构造**。
- **backend 读写都受限**：读走叠加视图（暂存优先），写经 R15 `FileBackend`——路径与权限检查在**工具层**执行，非仅最终提交。
- **逐次入账**：包装模型的 `_generate/_agenerate`，每次请求独立预留/结算；Agent 工具循环产生的**每个**内部请求都计入账本。
- **输出协议由程序负责**：变更集由 `FileBackend.build_change_set()` 计算，模型输出**不直接登记为新 Method Unit**。

## 依赖兼容性（技术文档第 327 行要求核对）

`deepagents 0.7.24` 要求 `langchain>=1.4.4,<2`、`langchain-core>=1.6.9,<2`（本机 1.6.9 未变）、`langchain-anthropic`、`langchain-google-genai`、`langsmith`；
对接百炼 OpenAI 兼容端点补装 `langchain-openai`（`ChatOpenAI`）。`langgraph 1.2.14` 保持不变。

## 真实探针证据（北京地域，2026-10-13，计费）

命令：`cd platform-api && set -a; source .env; set +a && uv run --locked python ../scripts/coding_tools_probe.py`

- **地域/端点**：`https://dashscope.aliyuncs.com/compatible-mode/v1`（北京）；`response_model=qwen3-coder-flash`。
- **阶段 A（原生 Function Calling）**：PASS —— 返回 `tool_calls=[{name:"write_file", arguments:...}]`，`usage.total_tokens=347`。
- **阶段 B（受限引擎端到端）**：PASS
  - `tool_names = [edit_file, glob, grep, ls, read_file, write_file]`（**无 `execute`/`task`**）；
  - `change_set`：`ReportService.java` `modify`（暂存写入成功，**未提交**正式源码）；
  - `ledger_rows = 4`：**每次内部请求各一行**（工具循环内多轮调用均入账）；
  - `status=UNKNOWN`、`price_version=UNKNOWN`、`cost_micro=null`：缺经核对的 `qwen3-coder-flash` 价目表，**如实记 UNKNOWN 未伪算**（价格完善见 R20）。

## 验证（实测）

`./scripts/ci_gate.sh` → `==> gate passed`

| 目标 | 命令 | 结果 |
|---|---|---|
| 平台后端 | `uv run --locked pytest` | **400 passed**（R15 后 389，本步 +11） |
| 平台后端静态 | `ruff check .` | All checks passed |
| 平台前端 | `npm ci + type-check + build + test` | 36 passed |
| 独立 Runner | `pytest / ruff` | 28 passed, 2 skipped；All checks passed |

## WhenDone 逐项判定

| 判据 | 结果 | 依据 |
|---|---|---|
| `qwen3-coder-flash` 在目标地域完成真实受限工具调用与暂存写入 | ✅ PASS | 探针阶段 A/B：真实 `write_file`/`edit_file` 调用 + 暂存 `modify` 变更集 |
| 危险工具/越界均被拒 | ✅ PASS | 工具表审计移除 `execute`/`task`；`test_assert_restricted_rejects_forbidden_tools`；backend 越界/受保护/遍历拒绝用例 |
| 内部所有调用入账 | ✅ PASS | 探针 `ledger_rows=4`；`test_ledger_model_records_each_internal_call`（逐次 reserve/settle、继承 task_id、成本核算） |
| 工具能力不支持即阻塞、不声明 PASS | ✅ PASS（未触发） | 北京地域支持；探针按“不支持则 NOT_RUN”逻辑实现 |

## 风险 / 下一步

- **价格未知**：`qwen3-coder-flash` 尚无经核对的人民币价目表，探针与测试均记 `UNKNOWN`，不伪算（R20 完善 Provider 价格/usage/共享预算）。
- **dependency 面扩大**：`deepagents` 传递引入 `langchain-anthropic`/`langchain-google-genai`/`langsmith`；本平台**路由未新增模型**，
  仅用其 `create_deep_agent` 框架能力，coding 仍绑定百炼 `qwen3-coder-flash`。
- **仅引擎，未接生成主线**：本步只交付受限引擎与探针；缺口如何装配优先、何时调用、变更集如何复验与原子提交见 **R17**；
  失败分类与有限修复见 R18；实际生成期全量复验见 R19。
- **下一步 R17**：实现“装配优先，缺口局部 Patch”——把 R14 的真实缺口交给 R16 引擎，变更校验通过后按 R15 原子提交；
  有可安全装配的方法资产时优先复用，不因 Planner 提议新写就直接调用 Agent。
