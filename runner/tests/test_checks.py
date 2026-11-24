"""M17 校验管线单元测试（分类/脱敏/静态清单，不依赖 Docker）。"""

from __future__ import annotations

from pathlib import Path

from agentforge_runner.checks import (
    STATIC_REQUIRED,
    CheckResult,
    classify_failure,
    overall_of,
    redact,
    run_backend_static_check,
    run_static_check,
    run_verification,
    truncate,
)
from agentforge_runner.config import RunnerConfig


def test_classify_build_failure() -> None:
    assert classify_failure(1, "compilation error: cannot find symbol") == "BUILD_FAILED"


def test_classify_infrastructure_on_offline_deps() -> None:
    log = "Could not transfer artifact ... Connection refused"
    assert classify_failure(1, log) == "INFRASTRUCTURE"


def test_classify_infrastructure_on_npm_network_error() -> None:
    assert classify_failure(1, "npm ERR! code ENOTFOUND") == "INFRASTRUCTURE"


def test_classify_timeout() -> None:
    assert classify_failure(124, "") == "TIMEOUT"


def test_redact_hides_secret_values() -> None:
    redacted = redact("password=abc123\ntoken: xyz\neyJhbGciOi.aaa.bbb")

    assert "abc123" not in redacted
    assert "xyz" not in redacted
    assert "eyJhbGciOi.aaa.bbb" not in redacted


def test_truncate_bounds_length() -> None:
    truncated = truncate("a" * 10000, limit=100)

    assert len(truncated) < 200
    assert "truncated" in truncated


def test_static_check_passes_for_complete_project(tmp_path: Path) -> None:
    for name in STATIC_REQUIRED:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    migration = tmp_path / "backend/src/main/resources/db/migration/V2__create_task.sql"
    migration.parent.mkdir(parents=True, exist_ok=True)
    migration.write_text("-- migration", encoding="utf-8")

    assert run_static_check(tmp_path).status == "PASS"


def test_static_check_reports_missing_files(tmp_path: Path) -> None:
    result = run_static_check(tmp_path)

    assert result.status == "FAIL"
    assert result.error_code == "MISSING_FILES"


def test_overall_rollup() -> None:
    assert overall_of([CheckResult(name="a", status="PASS")]) == "PASS"
    assert overall_of(
        [CheckResult(name="a", status="PASS"), CheckResult(name="b", status="NOT_RUN")]
    ) == "NOT_RUN"
    assert overall_of(
        [CheckResult(name="a", status="PASS"), CheckResult(name="b", status="FAIL")]
    ) == "FAIL"


def test_backend_static_manifest_passes_without_frontend(tmp_path: Path) -> None:
    pom = tmp_path / "backend/pom.xml"
    pom.parent.mkdir(parents=True, exist_ok=True)
    pom.write_text("<project/>", encoding="utf-8")
    mig = tmp_path / "backend/src/main/resources/db/migration/V100__auth.sql"
    mig.parent.mkdir(parents=True, exist_ok=True)
    mig.write_text("CREATE TABLE users(id BIGINT);", encoding="utf-8")

    assert run_backend_static_check(tmp_path).status == "PASS"
    cfg = RunnerConfig(token="t", work_root=tmp_path, executor_image="agentforge-executor:test")
    results = run_verification(tmp_path, cfg, ["backend_static_manifest"])
    assert len(results) == 1
    assert results[0].name == "backend_static_manifest"
    assert results[0].status == "PASS"


def test_backend_static_manifest_fails_when_pom_or_sql_missing(tmp_path: Path) -> None:
    res = run_backend_static_check(tmp_path)
    assert res.status == "FAIL"
    assert res.error_code == "MISSING_FILES"
