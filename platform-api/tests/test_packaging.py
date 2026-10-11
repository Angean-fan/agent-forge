"""M24 打包与交付报告测试。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from agentforge_platform.asset_registry import default_registry
from agentforge_platform.contract import compile_from_spec
from agentforge_platform.db import session_scope
from agentforge_platform.models import ModelCall
from agentforge_platform.packaging import (
    PackageLimits,
    PackagingError,
    build_cost_report,
    build_verification,
    package_run,
)
from agentforge_platform.resolver import resolve_assets
from agentforge_platform.spec import SoftwareSpecification

REPO = Path(__file__).resolve().parents[2]
SAMPLE_SPEC = REPO / "schemas" / "task-management.spec.json"

_PASS_VERIFY = {
    "overall": "PASS",
    "checks": [{"name": n, "status": "PASS"} for n in
               ("static_manifest", "backend_test", "backend_build", "frontend_build")],
}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


def _spec() -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(SAMPLE_SPEC.read_text(encoding="utf-8"))


def _run_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "run-1"
    for name in (
        "backend/pom.xml",
        "frontend/package.json",
        "frontend/package-lock.json",
        "compose.yaml",
        "backend/src/main/resources/db/migration/V2__create_task.sql",
        "backend/.dockerignore",
        "frontend/.dockerignore",
    ):
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    (directory / ".env").write_text("DEEPSEEK_API_KEY=secret", encoding="utf-8")
    modules = directory / "frontend" / "node_modules"
    modules.mkdir(parents=True, exist_ok=True)
    (modules / "x.js").write_text("x", encoding="utf-8")
    return directory


def _package(tmp_path: Path, *, limits: PackageLimits | None = None, verification=None):
    spec = _spec()
    return package_run(
        _run_dir(tmp_path),
        spec=spec,
        lock=resolve_assets(default_registry(), "demo-ts-mp"),
        contract=compile_from_spec(spec),
        verification=verification or _PASS_VERIFY,
        acceptance=_PASS_ACCEPT,
        cost_report={"draft_phase": {}, "generation_phase": {}},
        limits=limits,
    )


def test_package_run_produces_zip_with_reports(tmp_path: Path) -> None:
    result = _package(tmp_path)
    run_dir = tmp_path / "run-1"

    assert result.verified is True
    with zipfile.ZipFile(run_dir / "source.zip") as archive:
        names = set(archive.namelist())
    assert {
        "backend/pom.xml", "spec.json", "asset-lock.json", "verification.json",
        "cost-report.json", "EXTERNAL_SERVICES.md", "API.md",
    } <= names
    assert "backend/.dockerignore" in names and "frontend/.dockerignore" in names
    assert not any(name.startswith(".env") for name in names)
    assert not any("node_modules" in name for name in names)

    verification = json.loads((run_dir / "verification.json").read_text(encoding="utf-8"))
    assert verification["validation_scope"] == "MVP"
    assert verification["verified"] is True and verification["label"] == "已验证"
    assert "无须配置" in (run_dir / "EXTERNAL_SERVICES.md").read_text(encoding="utf-8")


def test_secret_scan_rejects_and_leaves_no_zip(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    (run_dir / "leak.txt").write_text("token=sk-abcdefghijklmnop123456", encoding="utf-8")

    with pytest.raises(PackagingError) as error:
        _package(tmp_path)

    assert error.value.code == "SECRET_DETECTED"
    assert not (run_dir / "source.zip").exists()


def test_secret_after_first_64_kib_is_rejected(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    (run_dir / "late-secret.txt").write_text(
        "x" * 65536 + "sk-abcdefghijklmnop123456", encoding="utf-8"
    )

    with pytest.raises(PackagingError) as error:
        _package(tmp_path)

    assert error.value.code == "SECRET_DETECTED"
    assert not (run_dir / "source.zip").exists()


def test_secret_split_across_scan_chunks_is_rejected(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    (run_dir / "split-secret.txt").write_bytes(
        b"x" * (65536 - 2) + b"sk-abcdefghijklmnop123456"
    )

    with pytest.raises(PackagingError) as error:
        _package(tmp_path)

    assert error.value.code == "SECRET_DETECTED"


def test_symlink_to_file_outside_run_is_rejected(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("private content", encoding="utf-8")
    (run_dir / "linked.txt").symlink_to(outside)

    with pytest.raises(PackagingError) as error:
        _package(tmp_path)

    assert error.value.code == "UNSAFE_PACKAGE_PATH"
    assert not (run_dir / "source.zip").exists()


def test_symlink_inside_excluded_dir_is_skipped(tmp_path: Path) -> None:
    # 回归：npm 会在 node_modules/.bin 生成符号链接；该目录本就不打包，不应导致整个 run 失败。
    run_dir = _run_dir(tmp_path)
    bin_dir = run_dir / "frontend" / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    (bin_dir / "cli").symlink_to(outside)

    result = _package(tmp_path)

    assert result.zip_path == "source.zip"
    with zipfile.ZipFile(run_dir / "source.zip") as archive:
        names = archive.namelist()
    assert not any("node_modules" in name for name in names)


def test_size_cap_rejects_and_cleans_up(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)

    with pytest.raises(PackagingError) as error:
        _package(tmp_path, limits=PackageLimits(max_files=1, max_total_bytes=10, max_zip_bytes=10))

    assert error.value.code == "PACKAGE_TOO_LARGE"
    assert not (run_dir / "source.zip").exists()


def test_verification_label_marks_unverified() -> None:
    report = build_verification({"overall": "FAIL", "checks": []}, {"overall": "FAIL", "cases": []})

    assert report["verified"] is False
    assert report["label"] == "未验证"


def test_cost_report_separates_draft_and_generation(platform_app) -> None:
    _, factory = platform_app
    with session_scope(factory) as session:
        session.add(ModelCall(call_id="d1", model_role="user_interaction",
                              provider="deepseek_official", model="deepseek-flash",
                              project_id=1, draft_id=1, status="SETTLED",
                              input_tokens=100, output_tokens=50, cost_micro=2000))
        session.add(ModelCall(call_id="g1", model_role="user_interaction",
                              provider="deepseek_official", model="deepseek-flash",
                              project_id=1, run_id=7, status="UNKNOWN",
                              input_tokens=10, output_tokens=5))

    with session_scope(factory) as session:
        report = build_cost_report(session, project_id=1, run_id=7)

    assert report["draft_phase"]["calls"] == 1 and report["draft_phase"]["cost_micro"] == 2000
    assert report["generation_phase"]["calls"] == 1 and report["generation_phase"]["unknown_calls"] == 1
