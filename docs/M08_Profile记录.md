# M08｜锁定第一份 Platform Profile

日期：2026-10-11
状态：完成

## 对应 Goal / Context / Constraints / WhenDone

- **Goal**：定义并锁定第一个 Profile：生成前端 TS、后端 MyBatis-Plus，记录 Java/Node/MySQL/Maven 版本与 Runner 镜像引用。
- **Context**：依赖 M01；技术文档第 2 节"版本与模板管理"；M09 基础模板、M14 asset-lock 以它为兼容性依据。
- **Constraints**：版本须经本地工具和构建验证后写入；Profile 一经发布不可原地覆盖；只启用这一组合，不提前实现其余三组合。
- **WhenDone**：`profiles/` 有可机器解析、可校验版本号的 Profile；不存在不受控依赖范围；记录兼容性和未验证项。

## 新增/变更文件

```
profiles/
├── demo-ts-mp-0.1.json     # 锁定的 Profile
└── README.md               # 不可变策略与校验方式
platform-api/
├── src/agentforge_platform/profile.py   # PlatformProfile 解析 + 精确版本校验
└── tests/test_profile.py                # 7 例
```

## Profile 内容（`demo-ts-mp@0.1`，status=locked）

| 维度 | 值 |
|---|---|
| 前端 | TypeScript / Vue；npm；ESM |
| 前端运行时 | node 22.19.0、npm 10.9.3 |
| 前端依赖 | vue 3.5.43、vue-router 5.4.0、pinia 4.0.3、element-plus 2.14.7 |
| 前端 dev | typescript 7.0.2、vite 8.3.4、@vitejs/plugin-vue 6.0.10、vue-tsc 3.3.13、vitest 5.0.3 |
| 后端 | Java 17 / Spring Boot 3.5.16 / MyBatis-Plus / Maven 3.9.11 |
| 后端依赖 | mybatis-plus-spring-boot3-starter 3.5.17、mybatis 3.5.19、mybatis-spring 3.0.5、mysql-connector-j 9.7.0 |
| 数据库 | MySQL 8.0.43 |
| template_version | demo-0.1 |
| Runner 镜像 | **pending**（M16 锁定 reference/digest） |

所有版本为**精确版本**，无 `^`/`~`/范围/`latest`。

## 版本来源与验证（关键：非凭空选定）

1. **工具链（本地实测）**：java 17.0.17、maven 3.9.11、node 22.19.0、npm 10.9.3、mysql 8.0.43。
2. **后端构建验证**：在临时 Maven 工程（`spring-boot-starter-parent 3.5.16` + `mybatis-plus-spring-boot3-starter 3.5.17` + `mysql-connector-j`，Java 17）执行 `mvn -DskipTests package` → **BUILD SUCCESS**（约 39s）；`dependency:list` 实读解析版本：spring-boot 3.5.16、mybatis-plus 3.5.17、mybatis 3.5.19、mybatis-spring 3.0.5、mysql-connector-j 9.7.0。
3. **前端解析验证**：以精确版本 `npm install` → **added 125 packages**，无 peer 冲突，锁文件解析出的版本与 Profile 一致。
4. **版本检索**：npm registry 与 Maven Central 实检；Spring Boot 的 `<release>` 为里程碑 4.2.0-M2，故选用最新 **3.5.x GA（3.5.16）** 以匹配 Java 17 与 MyBatis-Plus spring-boot3 starter。

## 不可变策略

文件名 `<profile_id>-<profile_version>.json`；发布后不得原地修改，变更须新增 `profile_version`。详见 `profiles/README.md`。

## 实际执行的测试与结果

1. `uv run pytest -v` → **67 passed**（M01–M07 60 + M08 7）。
2. `uv run ruff check .` → **All checks passed**。
3. Profile 校验 CLI：`load_profile('../profiles/demo-ts-mp-0.1.json')` → `OK demo-ts-mp 0.1 locked`。
4. 覆盖点：Profile 校验通过且 `status=locked`；版本全为精确号；与 M06 `MVP_PROFILE_ID/VERSION` 及样例 spec 的 `template_version` 一致；范围版本（`^3.5.43`）、`latest`、缺段、未知段均抛 `ValidationError`。

## 未验证项（如实记录）

1. **Runner 镜像**：Docker 未安装、Docker Hub digest 不可取 → `runner_image.status=pending`，M16 回填。
2. **前端完整构建**：本步仅验证依赖解析；`vite build` / `vue-tsc` 在 M09 基础模板执行。

## 剩余风险 / 阻塞

- M06 的 `MVP_PROFILE_ID/VERSION` 为代码常量，已加一致性测试；M14/M09 接入时可改为读取 Profile 注册表。
- 无阻塞。

## WhenDone 逐项判定

| 条目 | 判定 | 依据 |
|---|---|---|
| `profiles/` 有可机器解析、可校验版本号的 Profile | 满足 | `demo-ts-mp-0.1.json` + `PlatformProfile` 校验 + 测试 |
| 不存在不受控依赖范围 | 满足 | 精确版本校验器拒绝范围/标签；测试覆盖 |
| 记录兼容性和未验证项 | 满足 | `verification` 段 + 本文"未验证项" |
