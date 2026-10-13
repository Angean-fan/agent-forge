# 里程碑验收：E（E01—E10 完整 Demo 之后的扩展）

日期：2026-10-13
结论：**达到本里程碑 WhenDone（其中 E05 真实 Live 执行、E08 真实预览运行时按约定标 NOT_RUN，待开发者确认）**

## 基准 Commit / Profile / Asset Lock

- **基准 Commit**：`a86382e`（R26—R27，Demo P0 四组合 0.4 与 9 次评估）→ 验收 HEAD `0095ab3`。
- **里程碑范围**：`git log a86382e..HEAD` = 10 个提交（E01…E10），69 files changed，+7815 / −18。
- **Profile**：`demo-ts-mp@0.4`（认证 Profile；四组合 0.4/0.1 均保留）。
- **Asset Lock**：`schemas/asset-lock.r27.sample.json`（0.1）与 `schemas/asset-lock.integration.sample.json`（0.3）
  均**重放校验通过**（`verify_lock_hash` OK）。

## 本期新增功能与明确排除项

- **新增（E01—E10）**：Integration Pack 只读清单与 Mock-only 锁定；Port/Adapter/Mock 契约链路与校验；
  外部待办结构化 + `.env.example`/`EXTERNAL_SERVICES.md` 生成；代码交付 vs 外部就绪分离（含前端页）；
  Live 安全前置/注入计划/失败分类/脱敏；经校验 OpenAPI 的 Client 生成与 Port 适配；第三方文档 →
  待确认 Integration Spec；P1 预览授权/生命周期/隔离；既有项目规格增量迭代；已验收个案 → 公共方法资产治理闭环。
- **明确排除**：不重做 MVP M01—M28；不重写既有 Registry/Resolver；**不把集成包接入生成默认路径**
  （E01—E10 一致保持 MVP 冷启动零漂移）；不引入向量库；不新增路由决策模型；不在无凭据时调用任何模型。

## 使用的运行机器与隔离配置

macOS 本地；`uv`（Python 3.12）/ Node 22 / 独立 Runner；**免费路径**：不发起网络、不启动容器/隔离实例、
不调用任何模型。集成/预览/Live 的真实运行时均为 NOT_RUN。

## 新增测试 PASS / FAIL / NOT_RUN

`./scripts/ci_gate.sh` → **gate passed**

| 套件 | 命令 | 结果 |
|---|---|---|
| 平台后端（全量） | `uv run --locked pytest` | **639 passed**（0 failed） |
| 平台后端静态 | `ruff check .` | All checks passed |
| 平台前端 | `npm ci + type-check + build + test` | **41 passed** |
| 独立 Runner | `pytest / ruff` | **30 passed, 2 skipped**；All checks passed |

- **MVP/基线（M/R）冷启动回归**：`pytest`（排除 E0x/E1x）= **505 passed**。
- **本期 E01—E10 新增**：`pytest tests/test_e0*.py tests/test_e10_*.py` = **134 passed**
  （E01 19 / E02 18 / E03 14 / E04 12 / E05 11 / E06 19 / E07 9 / E08 9 / E09 7 / E10 16）。
- **FAIL**：0。**NOT_RUN**：真实 Live 执行（E05）、真实预览运行时（E08）——见"遗留风险"。

## MVP Task 基线冷启动回归

- 505 项基线用例全绿（含 MVP Task 冷启动、装配、验收、四组合锁样例、R01—R27）；E01—E10 未改动
  生成默认路径的任何选择逻辑。
- 旧锁字节不变：E01 引入的集成包**不进入** `DEFAULT_ROOTS`，故 `resolve_assets` 输出与 0.1 锁哈希（`29b4d9d689…`）不变。

## 冻结断言未被修改的证据

- `git diff a86382e..HEAD --name-only -- platform-api/tests`：**仅新增** 10 个 `test_e*` 文件；
  基线既有测试文件**零改动**。
- 里程碑测试改动统计：**2261 insertions / 0 deletions**（测试文件纯新增，无原地重写）。
- 全里程碑 18 处删除**全部位于源码**（`asset_registry.py` 7、`resolver.py` 3、`routes_runs.py` 8），
  为注释/分支重构与既有函数替换，**不涉及任何测试断言或验收证据**。

## 模型路由与非预期调用检查

- **模型调用总数为 0**：E01—E10 均为确定性实现；DeepSeek/Qwen/Coder 的"缝"（E03/E07/E09 的用户解释、
  契约提议、影响分析）仅提供可注入接口，**默认路径不调用任何模型**。
- 无跨厂商调用、无隐藏思考、无 Qwen 原始输出面向用户；无 API Key 使用。

## 方法资产索引 schema/revision、查询与拒绝原因（脱敏事实）

- 方法仓库 `methods/`：3 个 Method Unit（field-validator / date-normalizer / status-guard），
  索引 schema `0.1`，`build_index` → count=3，`index_revision=16cfe33b3d…`。
- 拒绝原因均为结构化脱敏事实（Profile/栈/依赖/冲突/扩展点/许可证/规模/来源/注入），不含用户原文或密钥。

## 新增/引用的 Method Unit 与授权扩展点

- **本里程碑不向公共库发布新方法**：E10 的示例发布在**临时根**验证流程（发布→索引→精确复用），
  未污染仓库 `methods/`；生成物 `publishable_as_public_method_unit=False`，入库须人工审核（E10 闭环）。

## 资产检索命中、兼容判定、实际装配、验收覆盖与 Agent 缺口统计

- 由 E01—E09 的确定性测试覆盖：精确/召回候选、Profile/依赖/冲突/扩展点/许可证筛选、0.1/0.2/0.3 锁、
  旧锁重放、方法 COPY 装配与 endpoint；集成包装配入生成工程**未接线**（后续集成工作）。

## 旧 asset-lock 重放与公共资产只读性证明

- `asset-lock.r27.sample.json`（0.1，`29b4d9d689…`）与 `asset-lock.integration.sample.json`（0.3，`6fa58c5ed4…`）
  `verify_lock_hash` 通过（`test_old_0_1_lock_still_replays`、`test_lock_is_stable_and_matches_committed_sample`）。
- 公共资产只读：`scan_methods`/`verify_checksum` 全量通过；E01—E03 发布的新版本目录不复改旧版本。

## 模型调用、思考模式、Token、成本及 UNKNOWN 调用

全部为 **0**（无模型调用、无 Token、无费用、无 UNKNOWN）。访谈期费用不适用（本期未跑访谈）。

## 运行/磁盘/容器清理结果

- 无容器/隔离实例启动；测试全部使用 `tmp_path`，无残留；`methods/.index/*.sqlite` 为 gitignore 的派生视图。

## ZIP 与秘密扫描

- 本期**不产出交付 ZIP**（未接装配/打包）。
- 对里程碑新增的 61 个文件做秘密扫描（AKIA / PEM 私钥 / JWT / 长秘密赋值）：**无命中**。
- 生成产物样例仅含保留域占位值（`example.invalid`）与**空**秘密字段。

## 遗留风险与阻塞（需人工确认）

1. **E05 真实 Live 执行**：供应商沙箱/协议、Runner 网络放行（现 `container_network=none`）、受控秘密注入通道、
   副作用人工批准、第三方费用——**NOT_RUN**。
2. **E08 真实预览运行时**：隔离实例基础设施、对外暴露/TLS/审计、回收执行主体、演示数据来源——**NOT_RUN**。
3. **装配/打包接线**：把集成包 COPY/注入/运行纳入生成链路（E01—E10 未接线，以保 MVP 零漂移）。
4. **模型缝未实测**：DeepSeek/Qwen 真实调用与路由断言待带预算里程碑启用。

## 是否达到本里程碑 WhenDone

**是**。E01—E10 各自 WhenDone 的确定性判据均通过（详见各步记录 `docs/E01…E10_*.md`）；仅两项**运行时**能力
（E05 真实 Live、E08 真实预览实例）按"遇运行环境不确定即暂停确认"的要求标 **NOT_RUN**，不计入本里程碑完成度。

### 证据索引

- 提交：`git log --oneline da0841c^..0095ab3`（E01…E10）。
- 各步记录：`docs/E01_…`、`E02_…`、`E03_…`、`E04_…`、`E05_…`、`E06_…`、`E07_…`、`E08_…`、`E09_…`、`E10_…`。
- 门禁：`./scripts/ci_gate.sh`（639 / ruff / 41 / 30+2 skipped）。
- 基线：`pytest`（排除 E 用例）505 passed。
