### 实施记录：A06｜模块与接口契约底座升级（`APPROVED_FOR_BUILD`、SQL 号段与 `Facade Stub` 生成）

- 基准 Commit / 结束 Commit：`eb577ed`（本步独立提交见 git log `feat(a06): ...`）
- 对应文档与步骤编号：《AgentForge_02_现有底座改造与适配_分步开发Prompt.md》`A06`
- 当前状态：**PASS**
- 接口选用（复用已有接口 / 本步新增接口）：
  - **复用并扩展**：
    - `interface_registry.py`：复用 `PublicCapability`、`InterfaceRegistry`、`STATUS_DRAFT`、`STATUS_VERIFIED`、`STATUS_DEPRECATED`、`breaking_change()`、`registry_issues()`；新增 `STATUS_APPROVED_FOR_BUILD = "APPROVED_FOR_BUILD"`、`approve_for_build()`、`lookup(..., include_approved=False)`（不增减 `PublicCapability` 字段，保持 F03 14 字段严格契约零破坏）。
    - `module_registry.py`：复用 `Module`、`ModuleRegistry`、`registry_issues()`、`owners_of()`、`write_issues()`、`effective_write_scope()`；在 `Module` 上新增向后兼容可选字段 `migration_range: tuple[int, int] | None = None`、`test_paths: list[str] = []`、`owned_package: str | None = None`，并在 `registry_issues()` 中补齐 `test_paths` 唯一写入权冲突、SQL 号段非法/重叠与 `owned_package` 冲突校验。
    - `module_parallel.py`：复用 `plan_waves()`、`_resources()`、`_conflicts()`；当同波模块均显式分配了互不重叠的 `migration_range` 时允许同波并行编写各自迁移脚本，未设 `migration_range` 或号段重叠时保留原有全局 `MIGRATION_RESOURCE` 互斥串行语义。
    - `agent_files.py`：扩展 `_is_protected()`，将 `.../modules/<dept_id>/api/*.java` 跨模块 Facade Stub 与公共 DTO 纳入只读保护，禁止单部门私自篡改接口签名。
  - **本步新增模块与接口**：
    - `platform-api/src/agentforge_platform/blueprint_contracts.py`：
      - `DtoField`、`DtoContract`、`ArchitectureBlueprint`（含 `to_module_registry()`、`to_interface_registry()`、`blueprint_hash()`、`facade_stub_paths()`）
      - `validate_blueprint(blueprint, spec) -> list[str]`：执行 5 项确定性硬校验（目录/测试路径/包零交集且禁写 `api/`、SQL 号段 `10..899` 零交集、依赖契约全存在且禁模糊签名与未声明跨模块依赖、模块与接口联合依赖图无环、覆盖全部冻结业务实体与验收场景）
      - `generate_java_facade_stubs(blueprint, target_dir) -> list[str]`：确定性生成合法可编译的 `modules/<dept_id>/api/*Facade.java` 接口与 `*Dto.java` Java record 骨架（自动解析标准库及跨模块 DTO `import`），落盘权限设为 `0o444` 只读。
- 实际新增或修改文件：
  - 修改 `platform-api/src/agentforge_platform/interface_registry.py`
  - 修改 `platform-api/src/agentforge_platform/module_registry.py`
  - 修改 `platform-api/src/agentforge_platform/module_parallel.py`
  - 修改 `platform-api/src/agentforge_platform/agent_files.py`
  - 新增 `platform-api/src/agentforge_platform/blueprint_contracts.py`
  - 新增 `platform-api/tests/test_a06_blueprint_contracts.py`
  - 新增 `docs/A06_契约冻结与Facade骨架记录.md`
- 兼容性说明（旧 SQLite 测试 / 旧 asset-lock / 原主生成图开关关闭回归）：
  - `Module` 新增字段均带默认向后兼容值（`None`/`[]`）；`PublicCapability` 字段集合未做任何变更；`interface_registry.lookup()` 默认仍仅返回 `VERIFIED`；`module_parallel.plan_waves()` 对未声明 `migration_range` 的旧模块保持全局互斥语义；
  - 既有 `test_f01_module_registry.py`、`test_f03_interface_registry.py`、`test_f06_module_integration.py`、`test_f08_module_parallel.py` 共 44 项测试 100% 零改动通过。
- 执行命令与真实测试结果（PASS / FAIL / NOT_RUN）：
  - `javac -version` → `javac 17.0.17`（测试中对生成的 10 个 Java Facade/DTO 骨架执行了真实 `javac` 编译校验并产出 `.class`）
  - `cd platform-api && uv run --locked ruff check .` → **All checks passed!**
  - `cd platform-api && uv run --locked pytest tests/test_a06_blueprint_contracts.py tests/test_f01_module_registry.py tests/test_f03_interface_registry.py tests/test_f06_module_integration.py tests/test_f08_module_parallel.py -v` → **53 passed**（含 9 项 A06 新增测试 + 44 项 F-series 既有测试）
  - `./scripts/ci_gate.sh`：
    - `platform-api`：`uv run --locked pytest` → **804 passed, 3 skipped**（3 项实时 PG 测试因本地 15432 隧道未开启按约定 skip/NOT_RUN）；`uv run --locked ruff check .` → **All checks passed!**
    - `platform-ui`：`npm ci && npm run type-check && npm run build && npm test` → **41 passed**
    - `runner`：`uv run --locked pytest && uv run --locked ruff check .` → **31 passed, 1 skipped**，ruff clean
- WhenDone 逐条判定与证据位置：
  1. 给定一份 5 部门电商 `ArchitectureBlueprint`（`auth`/`product`/`cart`/`order`/`payment`），`validate_blueprint` 通过并确定性生成合法可编译的 `modules/<dept_id>/api/*Facade.java` 与 DTO 骨架（且通过 `javac 17.0.17` 真实编译与 `FileBackend` 只读拦截校验）：**PASS**（见 `test_5_department_ecommerce_blueprint_validates_and_generates_compilable_facade_stubs`、`test_generated_facade_stubs_are_protected_in_file_backend`）。
  2. 覆盖全部 5 类负例（目录与 `api/` 路径重叠、SQL 号段重叠/越界、模糊签名 `TODO/*`、模块依赖环、未声明跨模块依赖与实体覆盖遗漏）全部被确定性拒绝：**PASS**（见 `test_negative_1_directory_and_api_path_overlap_rejected` 至 `test_negative_5_undeclared_cross_module_dependency_and_missing_coverage_rejected`）。
  3. 互不重叠的 `migration_range` 支持同波并行写迁移脚本，未设 `migration_range` 或重叠号段自动互斥串行：**PASS**（见 `test_module_parallel_respects_disjoint_migration_range_and_preserves_legacy_mutex`）。
  4. 新增 `tests/test_a06_blueprint_contracts.py` 全绿、`./scripts/ci_gate.sh` 全绿、记录写入 `docs/A06_契约冻结与Facade骨架记录.md` 并独立 git commit：**PASS**。
- 遗留风险与下一步依赖：
  - 开工前契约冻结态、SQL 号段并行调度与只读 Java Facade Stub 骨架生成器已就绪；
  - 下一步进入 `A07`（并发预算池与事务账本升级：PG 行锁防双扣 + 部门维度记账）。
