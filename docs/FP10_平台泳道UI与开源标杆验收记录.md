### 实施记录：FP10｜平台 UI 多部门泳道进度页与「电商沙箱」开源端到端标杆验收

- 基准 Commit / 结束 Commit：`0701a64`（本步独立提交见 git log `feat(fp10): ...`）
- 对应文档与步骤编号：《AgentForge_03_异步工厂流水线与开源交付_分步开发Prompt.md》`FP10`
- 当前状态：**PASS**（`GET /api/runs/{run_id}/factory` 只读归属校验与脱敏审计、`platform-ui` 多部门泳道进度页与运行模式切换器、XSS 零 `v-html` 防护、电商 5 部门沙箱 ZIP 解压后真实前端冷启动验证、开源发布 6 项门槛审计与 `docs/FACTORY_PIPELINE_ACCEPTANCE.md` 全部 PASS；宿主机实机 Docker Runner 容器与真实付费商业 LLM 调用如实标 `NOT_RUN`）
- 接口选用（复用已有接口 / 本步新增接口）：
  - **后端新增只读接口（`platform-api/src/agentforge_platform/routes_runs.py`）**：
    - `GET /api/runs/{run_id}/factory`（依赖 `require_run_owner` 强制校验项目归属；返回 `FactoryRunView`：当前阶段 `backend/frontend/idle`、冻结蓝图摘要 `blueprints`、后端/前端多部门泳道状态/小循环轮次/耗时/测试证据/按部门成本 `backend_departments` & `frontend_departments`、大循环轮次 `integration_rounds`、经 `sanitize_defect_log` 脱敏的未解决/已解决缺陷单摘要 `open_defects` & `resolved_defects`、以及经 `_sanitized_routing_snapshot()` 仅暴露域名与环境变量名的自定义模型路由脱敏快照 `model_routing_snapshot`）
    - `RunCreateRequest` 扩展可选 `mode: Literal["standard", "factory"] | None = None`（默认 `None`，旧调用方 100% 兼容）
  - **前端扩展（`platform-ui/src/`）**：
    - `api/types.ts` & `api/client.ts`：新增 `RunMode`、`FactoryRunView` 系列类型与 `runsApi.factory(runId)`；`runsApi.start(...)` 支持可选 `mode` 参数（默认标准模式保持原 3 参请求体不变）
    - `views/RunDetailView.vue`：新增「异步工厂多部门泳道视图」（按后端阶段/前端阶段分区展示各部门 `规划 → 编码 → 测试 → 通过/返工` 状态卡片、小循环轮次、耗时、测试退出码、按部门费用、未解决/已解决缺陷单与脱敏模型路由快照；沿用 2 秒增量轮询与指数退避，零虚假进度百分比；全部文本经 `{{ ... }}` / `<SafeText>` 渲染，源码严禁 `v-html`）
    - `views/ProjectDetailView.vue`：新增「运行模式」选择（标准装配模式 / 异步工厂模式，展示模块数 ≤ 8、业务实体 ≤ 25 软上限及本地沙箱模拟提示）
- 实际新增或修改文件：
  - 修改 `platform-api/src/agentforge_platform/routes_runs.py`
  - 新增 `platform-api/tests/test_fp10_factory_api_and_benchmark.py`
  - 修改 `platform-ui/src/api/types.ts`
  - 修改 `platform-ui/src/api/client.ts`
  - 修改 `platform-ui/src/views/RunDetailView.vue`
  - 修改 `platform-ui/src/views/ProjectDetailView.vue`
  - 新增 `platform-ui/test/factory-run.test.ts`
  - 新增 `docs/FACTORY_PIPELINE_ACCEPTANCE.md`
  - 新增 `docs/FP10_平台泳道UI与开源标杆验收记录.md`
- 兼容性说明（旧 SQLite 测试 / 旧 asset-lock / 原主生成图开关关闭回归）：
  - 标准装配运行调用 `GET /api/runs/{run_id}/factory` 返回 `factory_active=false`，`RunDetailView.vue` 保持原有简洁视图不变；
  - 未修改既有串行 `generation_graph.py` 逻辑、未修改任何已发布公共资产原件、未削弱任何既有后端/前端/Runner 测试断言。
- 执行命令与真实测试结果（PASS / FAIL / NOT_RUN）：
  - `cd platform-api && uv run --locked ruff check .` → **All checks passed!**
  - `cd platform-api && uv run --locked pytest tests/test_fp10_factory_api_and_benchmark.py -v` → **2 passed in 7.53s**（含归属校验、密钥/PG 密码脱敏审计与电商沙箱 ZIP 解压后真实 `npm ci --offline && npm run type-check && npm test` 退出码 0）
  - `cd platform-ui && npm run type-check && npm run build && npm test` → **8 test files, 44 passed (44)**（含新增 `test/factory-run.test.ts` 3/3 PASS）
  - `./scripts/ci_gate.sh`：
    - `platform-api`：`uv run --locked pytest` → **874 passed in 214.69s**（0 failed, 0 skipped，全部实库 PostgreSQL 用例实跑通过）；`uv run --locked ruff check .` → **All checks passed!**
    - `platform-ui`：`npm ci && npm run type-check && npm run build && npm test` → **44 passed**
    - `runner`：`uv run --locked pytest && uv run --locked ruff check .` → **49 passed, 1 skipped**，ruff clean
  - 开源隐私审计：`git ls-files` 无 `.env` 文件（0 命中），非测试源码正则扫描真实密钥（`sk-*` / `AKIA*` / `ghp_*` / PEM 私钥）0 命中
  - 实机宿主机 Docker Runner 容器 + 真实商业付费 LLM 端到端标杆实测：**NOT_RUN**（本机未安装 Docker 且无真实商业模型 API Key，已如实记录在 `docs/FACTORY_PIPELINE_ACCEPTANCE.md`）
- WhenDone 逐条判定与证据位置：
  1. `GET /api/runs/{run_id}/factory` 具备归属权校验、脱敏审计与完整单测：**PASS**（见 `tests/test_fp10_factory_api_and_benchmark.py`）。
  2. `platform-ui` 通过 `npm run type-check`、`npm run build` 与新增 `test/factory-run.test.ts`（含 XSS 防护、零 `v-html` 源码断言、泳道 2 秒轮询与运行模式切换）：**PASS**（见 `platform-ui/test/factory-run.test.ts`）。
  3. 产出 `docs/FACTORY_PIPELINE_ACCEPTANCE.md`（含电商 5 部门沙箱完整验收证据、ZIP 解压前端冷启动实测、原有 MVP/Demo P0 零回归证明与 `NOT_RUN` 诚实标注）：**PASS**（见 `docs/FACTORY_PIPELINE_ACCEPTANCE.md`）。
  4. `./scripts/ci_gate.sh` 全绿并独立 git commit：**PASS**。
- 遗留风险与后续行动：
  - 文档三全部 10 步（`FP01`–`FP10`）增量实施与全仓门禁均已闭环；
  - 后续在具备 Docker Engine 与真实商业大模型 API Key 的环境中，按 `docs/FACTORY_PIPELINE_ACCEPTANCE.md` §5 补跑实机端到端标杆并回填真实 Token/费用与容器冷启动日志即可完成公开发布前实机终验。
