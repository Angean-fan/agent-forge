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
| `demo-ts-mp` | `0.1` | 前端 TypeScript(Vue 3) + 后端 MyBatis-Plus | locked |

MVP 仅启用 `demo-ts-mp` 组合；JS/`MyBatis` 其余三种组合在本阶段不实现。

## 校验

```bash
cd platform-api
uv run python -c "from agentforge_platform.profile import load_profile; load_profile('../profiles/demo-ts-mp-0.1.json'); print('OK')"
uv run pytest tests/test_profile.py
```
