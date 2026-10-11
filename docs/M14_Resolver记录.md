# M14｜实现最小 Asset Resolver 与锁清单

日期：2026-10-11
状态：完成

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：为 Task 生成链路选择模板和能力包，在写文件前检测重复能力、路径冲突与依赖关系，并输出 `asset-lock.json`。
- **Context**：依赖 M13；技术文档第 3 节要求先解析、校验，再写入；M15 消费解析后的只读资产引用。
- **Constraints**：不让 LLM 直接指定任意磁盘路径、包地址和依赖版本；不使用"有冲突就覆盖"的策略；输出包含资产版本及 hash。
- **WhenDone**：正常 Task 计划得到稳定锁清单；构造的重复能力/路径冲突被拒；相同输入重复解析锁清单一致。

## 新增/变更文件

```
schemas/asset-lock.sample.json                    # Task 锁清单样例
platform-api/
├── src/agentforge_platform/resolver.py       # resolve_assets + AssetLock + lock_hash + CLI
├── src/agentforge_platform/asset_registry.py # + all_with_paths()
└── tests/test_resolver.py                    # 7 例
templates/manifest.json                       # provided_capabilities 增补 mybatis-plus
capability-packs/task-crud-backend/manifest.json  # requires 与模板能力名对齐
```

## 解析算法

选择仅依据注册表 manifest 与 Profile（**LLM 无法指定路径/包地址/依赖版本**）：

1. **模板**：`kind=template` 且 `compatible_profiles` 含该 Profile；必须**恰好 1 个**，否则 `ASSET_CONFLICT`。
2. **能力包**：`kind=capability-pack` 且兼容该 Profile 的全部包（MVP 恰为后端 + 前端两个；更细的按能力选择留待 R 阶段）。
3. 写文件前校验：
   - **重复能力**：任一 `provides` 能力名被两个资产提供 → `ASSET_CONFLICT`；
   - **路径冲突**：两资产 `owned_paths` 前缀重叠（含相等）→ `ASSET_CONFLICT`（不做"覆盖"）；
   - **依赖关系**：每个资产的 `requires`（如 `template:auth`）须被**模板 `provided_capabilities`** 满足，否则 `ASSET_CONFLICT`。

为对齐依赖，模板 `provided_capabilities` 增补 `mybatis-plus`，后端包 `requires` 调整为 `template:auth / template:exception-handling / template:mybatis-plus`。

## 锁清单（`asset-lock.json`）

```
schema_version / profile / assets[{kind,id,version,checksum,path}] / lock_hash
```

- 资产按 `(kind, id)` 排序；`path` 为仓库相对路径；
- `lock_hash = SHA-256(规范化 JSON 去掉 lock_hash 自身)`，同输入恒等；
- 样例（`schemas/asset-lock.sample.json`）：
  - `task-crud-backend@0.1.0` checksum `ab27d23b…`
  - `task-crud-frontend@0.1.0` checksum `bb6e0267…`
  - `base-template@demo-0.1` checksum `2a468fce…`
  - `lock_hash = 32b521be…`

CLI：`python -m agentforge_platform.resolver [profile]`。

## 验证（实测）

1. `uv run --locked pytest` → **102 passed**（M01–M13 95 + M14 7）。
2. `uv run --locked ruff check .` → **All checks passed**。
3. 覆盖：正常 Task 计划得到 3 资产锁清单（版本 + checksum 齐全）；重复解析 `lock_hash` 与内容一致；提交样例与解析一致；重复能力/路径冲突/依赖未满足/无兼容模板均返回 `ASSET_CONFLICT`。

## 剩余风险 / 阻塞

- 选择粒度为"兼容 Profile 的全部能力包"（MVP 足够）；按能力/实体类型的精细选择与冲突消解留待资产增多时（R 阶段 E10）扩展。
- 无阻塞。

## WhenDone 逐项判定

| 条目 | 判定 | 依据 |
|---|---|---|
| 正常 Task 计划得到稳定锁清单 | 满足 | `test_resolve_task_lock` + 样例文件 |
| 构造的重复能力/路径冲突被拒 | 满足 | `test_duplicate_capability_is_rejected` / `test_path_conflict_is_rejected`（附带依赖未满足） |
| 相同输入重复解析锁清单一致 | 满足 | `test_resolve_is_stable`（lock_hash 与内容一致） |
