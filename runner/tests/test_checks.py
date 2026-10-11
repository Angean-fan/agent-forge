"""M17 校验管线单元测试（分类/脱敏/静态清单，不依赖 Docker）。"""

from __future__ import annotations

from pathlib import Path

from agentforge_runner.checks import (
    STATIC_REQUIRED,
    CheckResult,
    classify_failure,
    overall_of,
    redact,
    run_static_check,
    truncate,
)


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
