"""Platform Profile 解析与校验（M08）。

Profile 是可版本化、不可原地覆盖的兼容性基线；所有版本号必须为**精确版本**，
不允许范围/标签，以避免不受控依赖。
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

_EXACT_VERSION = re.compile(r"^\d+(\.\d+)*$")
_STRICT = ConfigDict(extra="forbid")


class FrontendProfile(BaseModel):
    model_config = _STRICT

    language: str
    framework: str
    package_manager: str
    module_system: str
    syntax: str
    runtime: dict[str, str]
    dependencies: dict[str, str]
    dev_dependencies: dict[str, str]


class BackendProfile(BaseModel):
    model_config = _STRICT

    language: str
    java_version: str
    framework: str
    spring_boot_version: str
    orm: str
    build_tool: str
    maven_version: str
    dependencies: dict[str, str]


class DatabaseProfile(BaseModel):
    model_config = _STRICT

    engine: str
    version: str


class RunnerImage(BaseModel):
    model_config = _STRICT

    status: str
    reference: str | None
    digest: str | None
    note: str


class ProfileVerification(BaseModel):
    model_config = _STRICT

    toolchain: str
    backend_build: str
    frontend_resolution: str
    frontend_build: str
    runner_image: str


class PlatformProfile(BaseModel):
    model_config = _STRICT

    profile_id: str
    profile_version: str
    status: str
    template_version: str
    generated_at: str
    frontend: FrontendProfile
    backend: BackendProfile
    database: DatabaseProfile
    runner_image: RunnerImage
    verification: ProfileVerification

    @model_validator(mode="after")
    def _check_exact_versions(self) -> PlatformProfile:
        entries: list[tuple[str, str]] = [
            ("profile_version", self.profile_version),
            ("backend.java_version", self.backend.java_version),
            ("backend.spring_boot_version", self.backend.spring_boot_version),
            ("backend.maven_version", self.backend.maven_version),
            ("database.version", self.database.version),
        ]
        for group, mapping in (
            ("frontend.runtime", self.frontend.runtime),
            ("frontend.dependencies", self.frontend.dependencies),
            ("frontend.dev_dependencies", self.frontend.dev_dependencies),
            ("backend.dependencies", self.backend.dependencies),
        ):
            entries.extend((f"{group}.{key}", value) for key, value in mapping.items())

        for name, value in entries:
            if not _EXACT_VERSION.match(value):
                raise ValueError(
                    f"{name} 必须为精确版本号（不允许范围/标签）：{value!r}"
                )
        return self


def load_profile(path: str | Path) -> PlatformProfile:
    return PlatformProfile.model_validate_json(Path(path).read_text(encoding="utf-8"))
