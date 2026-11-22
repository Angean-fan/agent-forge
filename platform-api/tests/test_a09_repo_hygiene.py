"""A09：开源仓库卫生、Docker 资产打包修复与 README 同步校验测试。"""

from __future__ import annotations

import re

from agentforge_platform.asset_registry import REPO_ROOT

_REAL_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?:ghp|gho|ghu|ghs)_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def test_api_dockerfile_bundles_readonly_asset_seed_dirs_with_correct_repo_depth() -> None:
    dockerfile = (REPO_ROOT / "deploy" / "api.Dockerfile").read_text(encoding="utf-8")

    # 必须打包全部 6 个资产与契约目录，且位于 /app 根下并保持 /app/platform-api 工作目录深度
    for asset_dir in (
        "templates",
        "capability-packs",
        "methods",
        "integration-packs",
        "profiles",
        "schemas",
    ):
        assert f"COPY {asset_dir} ./{asset_dir}" in dockerfile
    assert "WORKDIR /app/platform-api" in dockerfile
    assert "chmod -R a-w" in dockerfile
    assert "uv sync --locked --no-dev" in dockerfile


def test_compose_files_provide_postgres_without_hardcoded_passwords() -> None:
    local_pg = (REPO_ROOT / "deploy" / "compose.local-pg.yaml").read_text(encoding="utf-8")
    compose = (REPO_ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")

    for content in (local_pg, compose):
        assert "postgres:16-alpine" in content
        assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?" in content
        for pat in _REAL_SECRET_PATTERNS:
            assert pat.search(content) is None


def test_gitignore_covers_all_sensitive_and_runtime_paths() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    required_entries = (
        ".env",
        "platform-api/config/accounts.json",
        "platform-api/config/models.json",
        "*.db",
        "out/",
        ".cache/",
    )
    for entry in required_entries:
        assert entry in gitignore


def test_readme_is_synced_with_current_architecture_and_three_docs() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for token in (
        "AgentForge_01_现状盘点与开发约束.md",
        "AgentForge_02_现有底座改造与适配_分步开发Prompt.md",
        "AgentForge_03_异步工厂流水线与开源交付_分步开发Prompt.md",
        "compose.local-pg.yaml",
        "seed_assets",
        "models.example.json",
        "/verify-module",
        "ci_gate.sh",
    ):
        assert token in readme
    for pat in _REAL_SECRET_PATTERNS:
        assert pat.search(readme) is None
