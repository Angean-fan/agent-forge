"""M08 Platform Profile 测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentforge_platform import spec
from agentforge_platform.profile import PlatformProfile, load_profile

PROFILE_PATH = (
    Path(__file__).resolve().parents[2] / "profiles" / "demo-ts-mp-0.4.json"
)
SUPERSEDED_PROFILE_PATHS = [
    Path(__file__).resolve().parents[2] / "profiles" / "demo-ts-mp-0.1.json",
    Path(__file__).resolve().parents[2] / "profiles" / "demo-ts-mp-0.2.json",
]
SAMPLE_SPEC_PATH = (
    Path(__file__).resolve().parents[2] / "schemas" / "task-management.spec.json"
)


def _raw() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def test_profile_file_is_valid_and_locked() -> None:
    profile = load_profile(PROFILE_PATH)

    assert profile.profile_id == "demo-ts-mp"
    assert profile.profile_version == "0.4"
    assert profile.template_version == "demo-0.2"
    assert profile.status == "locked"
    assert profile.frontend.syntax == "typescript"
    assert profile.backend.orm == "mybatis-plus"
    assert profile.backend.build_tool == "maven"


def test_profile_versions_are_all_exact() -> None:
    profile = load_profile(PROFILE_PATH)
    versions = [
        *profile.frontend.runtime.values(),
        *profile.frontend.dependencies.values(),
        *profile.frontend.dev_dependencies.values(),
        *profile.backend.dependencies.values(),
        profile.backend.java_version,
        profile.backend.spring_boot_version,
        profile.backend.maven_version,
        profile.database.version,
    ]
    assert versions
    for value in versions:
        assert value == value.strip()
        assert not any(op in value for op in ("^", "~", ">", "<", "*", " ", "||"))


def test_profile_matches_spec_constants() -> None:
    profile = load_profile(PROFILE_PATH)
    sample = json.loads(SAMPLE_SPEC_PATH.read_text(encoding="utf-8"))

    assert profile.profile_id == spec.MVP_PROFILE_ID
    assert profile.profile_version == spec.MVP_PROFILE_VERSION
    assert profile.template_version == sample["technical_profile"]["template_version"]


def test_superseded_profiles_still_validate() -> None:
    # 已发布 Profile 不可原地覆盖：旧版本文件保留且仍可校验。
    assert [load_profile(p).profile_version for p in SUPERSEDED_PROFILE_PATHS] == ["0.1", "0.2"]
    for path in SUPERSEDED_PROFILE_PATHS:
        assert load_profile(path).status == "locked"


def test_range_version_is_rejected() -> None:
    raw = _raw()
    raw["frontend"]["dependencies"]["vue"] = "^3.5.43"

    with pytest.raises(ValidationError):
        PlatformProfile.model_validate(raw)


def test_dependency_without_version_is_rejected() -> None:
    raw = _raw()
    raw["backend"]["dependencies"]["com.mysql:mysql-connector-j"] = "latest"

    with pytest.raises(ValidationError):
        PlatformProfile.model_validate(raw)


def test_missing_section_is_rejected() -> None:
    raw = _raw()
    del raw["backend"]

    with pytest.raises(ValidationError):
        PlatformProfile.model_validate(raw)


def test_unknown_section_is_rejected() -> None:
    raw = _raw()
    raw["extra_stack"] = {"note": "out of contract"}

    with pytest.raises(ValidationError):
        PlatformProfile.model_validate(raw)
