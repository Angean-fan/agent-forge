"""R09：四组合 Platform Profile 的**确定性**选项矩阵。

只从**已认证 Profile 文件**（`profiles/certified.json` + `profiles/<id>-<ver>.json`）派生选项与
「特点/局限/推荐理由」，不读原始聊天、不调用模型。面向用户的**通俗解释**由
:mod:`agentforge_platform.profile_explain` 在需要时单独调用 DeepSeek。
"""

from __future__ import annotations

import json
from pathlib import Path

from agentforge_platform.spec import certified_profiles

PROFILES_DIR = Path(__file__).resolve().parents[3] / "profiles"
# 默认推荐：TypeScript 前端 + MyBatis-Plus（与 MVP 基线一致）。
RECOMMENDED = ("demo-ts-mp", "0.3", "demo-0.2")

_FRONTEND_NOTE = {
    "typescript": "前端 TypeScript：类型安全，构建含类型检查（vue-tsc）。",
    "javascript": "前端 JavaScript：更轻量，无需类型检查步骤。",
}
_ORM_NOTE = {
    "mybatis-plus": "ORM MyBatis-Plus：约定式 CRUD，业务代码更少。",
    "mybatis": "ORM MyBatis：XML 显式 SQL，SQL 更透明可控、依赖更少。",
}


def _load_profile(profile_id: str, profile_version: str) -> dict:
    path = PROFILES_DIR / f"{profile_id}-{profile_version}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _notes(payload: dict) -> list[str]:
    notes: list[str] = []
    frontend = payload.get("frontend", {}).get("language")
    if frontend in _FRONTEND_NOTE:
        notes.append(_FRONTEND_NOTE[frontend])
    orm = payload.get("backend", {}).get("orm")
    if orm in _ORM_NOTE:
        notes.append(_ORM_NOTE[orm])
    return notes


def list_profile_options() -> list[dict]:
    """已认证四组合的确定性选项：`profile_id/version/template` + 前端语言/ORM + 推荐 + 特点。"""
    options: list[dict] = []
    for profile_id, profile_version, template_version in sorted(certified_profiles()):
        payload = _load_profile(profile_id, profile_version)
        options.append(
            {
                "profile_id": profile_id,
                "profile_version": profile_version,
                "template_version": template_version,
                "frontend_language": payload.get("frontend", {}).get("language"),
                "orm": payload.get("backend", {}).get("orm"),
                "recommended": (profile_id, profile_version, template_version) == RECOMMENDED,
                "notes": _notes(payload),
            }
        )
    return options


def find_option(profile_id: str) -> dict | None:
    for option in list_profile_options():
        if option["profile_id"] == profile_id:
            return option
    return None


__all__ = ["RECOMMENDED", "find_option", "list_profile_options"]
