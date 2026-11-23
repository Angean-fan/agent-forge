### 实施记录：FP03｜只读后端公共底座装配与跨模块 `api/*Facade.java` 骨架自动生成

- 基准 Commit / 结束 Commit：`bd66854`（本步独立提交见 git log `feat(fp03): ...`）
- 对应文档与步骤编号：《AgentForge_03_异步工厂流水线与开源交付_分步开发Prompt.md》`FP03`
- 当前状态：**PASS**（离线单测 + 本机 JDK 17 / Maven `mvn -B -o test-compile` 纯骨架编译全绿；实时 PG 隧道未启动，实库 PG 用例如实标 `skipped/NOT_RUN`）
- 接口选用（复用已有接口 / 本步新增接口）：
  - **复用**：
    - `asset_cache.py`：`AssetMaterializer.materialize_release()`、`remove_readonly_tree()`（从本地 PG/SQLite 物化已认证模板到只读缓存并核对 checksum）
    - `assembler.py`：`_validate_rel()`、`_is_within()`、`_EXCLUDED_COPY`、`AssemblyLimits`、`AssemblyError`（未修改 `assembler.py` 对旧 Demo 的任何装配逻辑）
    - `blueprint_contracts.py`：`ArchitectureBlueprint`、`generate_java_facade_stubs()`（确定性生成只读 `modules/<dept_id>/api/*Facade.java` 与公共 DTO）
    - `agent_files.py`：`PROTECTED_PREFIXES`
    - `resolver.py`：`AssetLock`、`verify_lock_hash()`
  - **本步新增模块与接口**：
    - `platform-api/src/agentforge_platform/factory_base_builder.py`：
      - `BACKEND_BASE_DIRNAME = "backend_base"`、`READONLY_MANIFEST_NAME = "readonly-manifest.json"`
      - `BaseManifestEntry`、`BaseManifest`、`BaseBuildResult`
      - `build_backend_base(run_dir, blueprint, lock, materializer, *, limits=None) -> BaseBuildResult`：全程确定性执行（**0 模型调用**），在 `run_dir/backend_base/` 原子装配只读后端底座（`pom.xml`、`common/ApiResponse.java`、`security/`、`config/`、`exception/`、基础 Flyway 迁移与模板基座测试）及全量 `modules/<dept_id>/api/*Facade.java` 与共享 DTO 骨架，全量文件设只读权限（`0o444`）并登记 SHA-256 至 `readonly-manifest.json`
      - `load_base_manifest()`、`verify_base_integrity()`、`assert_base_integrity()`、`check_department_paths_against_base()`：供总装与部门合并前拦截任何篡改底座、删除受保护文件、篡改他部门 `api/*Facade.java` 签名或私自新增未冻结 `api/` 接口的行为
- 实际新增或修改文件：
  - 新增 `platform-api/src/agentforge_platform/factory_base_builder.py`
  - 新增 `platform-api/tests/test_fp03_backend_base_builder.py`
  - 新增 `docs/FP03_后端只读底座与Facade骨架记录.md`
- 兼容性说明（旧 SQLite 测试 / 旧 asset-lock / 原主生成图开关关闭回归）：
  - 未修改 `assembler.py`、`generation_graph.py`、`templates/` 源件与任何既有测试断言；
  - 模板 `templates/demo-0.2` 已含 `V1__create_app_user.sql`，底座构建器自动保留该基座迁移且不生成与之 Flyway 版本号冲突的 `V001__base.sql`（仅当模板缺少 V1/V001 时才生成兜底 `V001__base.sql`）。
- 执行命令与真实测试结果（PASS / FAIL / NOT_RUN）：
  - `cd platform-api && uv run --locked ruff check .` → **All checks passed!**
  - `cd platform-api && uv run --locked pytest tests/test_fp03_backend_base_builder.py -v` → **4 passed, 1 skipped**
    - `test_build_backend_base_5_department_ecommerce_deterministic_and_readonly`：**PASS**
    - `test_base_integrity_detects_tampering_deletion_and_rogue_api_files`：**PASS**
    - `test_negative_tampered_lock_and_wrong_phase_rejected`：**PASS**
    - `test_maven_test_compile_on_pure_skeleton_when_mvn_available`：**PASS**（本机 JDK 17.0.17 + Maven 执行 `mvn -B -o test-compile` 编译纯骨架目录，验证 `TemplateApplication`、`ApiResponse`、`HealthTest` 及全部 5 份 `*Facade.class` 与 5 份 DTO `.class` 语法/包名/导入 100% 合法，且编译后底座完整性零篡改）
    - `test_live_postgres_materialize_and_build_backend_base`：**NOT_RUN / skipped**（本次执行时本机 PostgreSQL 15432 隧道未开启，按规范跳过）
  - `./scripts/ci_gate.sh`：
    - `platform-api`：`uv run --locked pytest` → **837 passed, 7 skipped**（7 项均为未连接实时 PG 隧道时自动 skip 的实库 PG 用例）；`uv run --locked ruff check .` → **All checks passed!**
    - `platform-ui`：`npm ci && npm run type-check && npm run build && npm test` → **41 passed**
    - `runner`：`uv run --locked pytest && uv run --locked ruff check .` → **49 passed, 1 skipped**，ruff clean
- WhenDone 逐条判定与证据位置：
  1. 针对 5 部门电商蓝图，确定性生成只读底座 + 5 份 `modules/<dept>/api/*Facade.java` + 5 份公共 DTO 的完整骨架目录并产出 SHA-256 哈希清单（重复构建 `manifest_hash` 逐字节一致，0 模型调用）：**PASS**（见 `test_build_backend_base_5_department_ecommerce_deterministic_and_readonly`）。
  2. 只读清单登记全部底座与 `api/` 路径 SHA-256，篡改底座 `pom.xml`、篡改他部门 `ProductFacade.java`、删除安全文件或向 `api/` 私自新增文件均被确定性拦截报错（`CONTRACT_CONFLICT`）：**PASS**（见 `test_base_integrity_detects_tampering_deletion_and_rogue_api_files`）。
  3. 本机 Maven/JDK 环境下，纯骨架目录直接通过 `mvn -B -o test-compile`：**PASS**（见 `test_maven_test_compile_on_pure_skeleton_when_mvn_available`）。
  4. 新增 `tests/test_fp03_backend_base_builder.py` 全绿、`./scripts/ci_gate.sh` 全绿、记录写入 `docs/FP03_后端只读底座与Facade骨架记录.md` 并独立 git commit：**PASS**。
- 遗留风险与下一步依赖：
  - 本次执行时未开启本地 PostgreSQL 15432 隧道，`test_live_postgres_materialize_and_build_backend_base` 标为 `NOT_RUN/skipped`，开启 PG 隧道后可一键复跑；
  - 下一步进入 `FP04`（部门三角色最小职责引擎 `DeptLead + DeptCoder + DeptTester` 与 `DefectTicket`）。
