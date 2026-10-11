# Platform Profiles

锁定的兼容性基线，供基础模板（M09）、能力包与 asset-lock（M14）引用。

## 不可变策略

- 一个 Profile 由 `profile_id + profile_version` 唯一标识，文件名为 `<profile_id>-<profile_version>.json`。
- **一经发布不可原地覆盖**：如需变更任何版本，新增一个 `profile_version` 文件，不得修改已发布文件内容。
- 所有版本号必须是**精确版本**（形如 `3.5.16`），不允许 `^`/`~`/`>`/范围/`latest` 等不受控写法；由 `agentforge_platform.profile.PlatformProfile` 校验。
- 冻结是维护策略，不假定框架不再升级；升级通过新增 Profile 完成。

## 已发布

| profile_id | version | 组合 | 状态 |
|---|---|---|---|
| `demo-ts-mp` | `0.1` | 前端 TypeScript(Vue 3) + 后端 MyBatis-Plus | locked（已被 0.2 取代，保留不改） |
| `demo-ts-mp` | `0.2` | 同上，前端 `typescript` 由 7.0.2 修正为 5.9.3 | locked（已被 0.3 取代，保留不改） |
| `demo-ts-mp` | `0.3` | 同上，模板切换为 `demo-0.2`（M15A：角色/分页/内存令牌/.dockerignore）；记录 Runner 镜像引用 | locked（当前） |

`0.2` 产生原因：M09 验证发现 `vue-tsc 3.3.13` 与 `typescript 7.0.2` 不兼容（TS 7 不再导出
`./lib/tsc`），类型检查失败；按"版本以兼容性测试结果为准"将 `typescript` 修正为 `5.9.3`
（`vue-tsc` 声明支持 `typescript>=5.0.0`，实测通过）。`0.1` 依不可变策略保留，未原地修改。

`0.3` 产生原因（M16）：以新 `profile_version` 记录 M15A 的新模板（`demo-0.2`）与 Runner 执行镜像引用；
因本机无 Docker，执行镜像 digest 记 `pending`（见 `docs/M16_运行器记录.md`）。

MVP 仅启用 `demo-ts-mp` 组合；JS/`MyBatis` 其余三种组合在本阶段不实现。

## 校验

```bash
cd platform-api
uv run python -c "from agentforge_platform.profile import load_profile; load_profile('../profiles/demo-ts-mp-0.1.json'); print('OK')"
uv run pytest tests/test_profile.py
```
