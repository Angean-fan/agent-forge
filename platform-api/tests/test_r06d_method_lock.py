"""R06D：方法索引接入 Resolver/装配器与 Asset Lock。

覆盖：无请求保持 MVP 0.1 锁不变；有请求升 0.2 并纳入方法单元；未命中/路径覆盖 → 结构化缺口；
装配 COPY 方法文件且可复现；生成图端到端（含方法）通过；0.2 锁重放/篡改被拒；旧 0.1 锁仍可重放。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from agentforge_platform.assembler import assemble
from agentforge_platform.asset_registry import compute_checksum, default_registry
from agentforge_platform.contract import compile_from_spec
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.method_index import build_index
from agentforge_platform.method_lock import resolve_lock
from agentforge_platform.models import Project, Run, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.resolver import AssetLock, resolve_assets, verify_lock_hash
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
METHODS = REPO / "methods"
SAMPLE = REPO / "schemas" / "task-management.spec.json"
FIXTURE_0_1 = Path(__file__).resolve().parent / "fixtures" / "asset-lock-0.1-baseline.json"

_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


def _spec(method_requests: list[str] | None = None) -> SoftwareSpecification:
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    if method_requests is not None:
        payload["method_requests"] = method_requests
    return SoftwareSpecification.model_validate(payload)


def _make_method(root: Path, method_id: str, *, owned_paths: list[str]) -> Path:
    directory = root / method_id / "1.0.0"
    (directory / "src").mkdir(parents=True)
    (directory / "src" / "Helper.java").write_text("class Helper {}", encoding="utf-8")
    manifest = {
        "id": method_id, "version": "1.0.0", "kind": "method-unit",
        "compatible_profiles": ["demo-ts-mp"], "requires": [], "provides": [], "conflicts": [],
        "parameter_schema": {}, "extension_points": [], "owned_paths": owned_paths,
        "checks": [], "checksum": "", "license": "internal",
        "symbol": f"pkg.{method_id.replace('-', '_')}.run", "signature": "void run()",
        "layer": "util", "language": "java", "framework": "spring-boot",
        "source_provenance": "test:r06d",
    }
    manifest["checksum"] = compute_checksum(directory)
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return directory


def _lock(method_requests: list[str], tmp_path: Path) -> AssetLock:
    spec = _spec(method_requests)
    index = tmp_path / "idx.sqlite"
    build_index(METHODS, index)
    return resolve_lock(
        default_registry(), "demo-ts-mp", spec, compile_from_spec(spec),
        methods_root_path=METHODS, index_path_override=index,
    )


# --- MVP 路径不变 -------------------------------------------------------------

def test_no_method_requests_keeps_mvp_lock(tmp_path: Path) -> None:
    spec = _spec()  # method_requests 默认空
    lock = resolve_lock(
        default_registry(), "demo-ts-mp", spec, compile_from_spec(spec),
        methods_root_path=METHODS,
    )
    base = resolve_assets(default_registry(), "demo-ts-mp")

    assert lock.schema_version == "0.1"
    assert lock.model_dump() == base.model_dump()
    assert lock.lock_hash == base.lock_hash


# --- 含方法 → 锁升 0.2 --------------------------------------------------------

def test_method_request_bumps_lock_to_0_2(tmp_path: Path) -> None:
    lock = _lock(["status-guard"], tmp_path)
    base = resolve_assets(default_registry(), "demo-ts-mp")

    assert lock.schema_version == "0.2"
    assert [unit.id for unit in lock.method_units] == ["status-guard"]
    assert lock.lock_hash != base.lock_hash
    assert lock.index_revision
    verify_lock_hash(lock)


def test_0_2_lock_tamper_is_rejected(tmp_path: Path) -> None:
    lock = _lock(["status-guard"], tmp_path)
    tampered = lock.model_copy(update={
        "method_units": [lock.method_units[0].model_copy(update={"checksum": "0" * 64})]
    })

    with pytest.raises(AppException) as error:
        verify_lock_hash(tampered)
    assert error.value.code == "ASSET_CHECKSUM_MISMATCH"


def test_old_0_1_lock_still_replays() -> None:
    lock = AssetLock.model_validate_json(FIXTURE_0_1.read_text(encoding="utf-8"))

    verify_lock_hash(lock)  # 0.1 白名单哈希，不受新字段影响


# --- 结构化缺口 ---------------------------------------------------------------

def test_missing_method_request_is_structured_gap(tmp_path: Path) -> None:
    spec = _spec(["does-not-exist"])
    with pytest.raises(AppException) as error:
        resolve_lock(
            default_registry(), "demo-ts-mp", spec, compile_from_spec(spec),
            methods_root_path=METHODS,
        )

    assert error.value.code == "ASSET_CONFLICT"
    assert "未找到方法" in error.value.message


def test_method_path_overlap_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "methods"
    _make_method(root, "template-clobber", owned_paths=["backend/"])  # 覆盖模板 owned_paths
    spec = _spec(["template-clobber"])

    with pytest.raises(AppException) as error:
        resolve_lock(
            default_registry(), "demo-ts-mp", spec, compile_from_spec(spec),
            methods_root_path=root,
        )

    assert "path_overlap" in error.value.message


# --- 装配 COPY 与可复现 -------------------------------------------------------

def test_assembler_copies_method_unit(tmp_path: Path) -> None:
    lock = _lock(["status-guard"], tmp_path)
    spec = _spec(["status-guard"])
    registry = default_registry()
    contract = compile_from_spec(spec)

    first = assemble(contract, lock, registry, tmp_path / "out-a",
                     spec=spec, methods_root=METHODS)
    second = assemble(contract, lock, registry, tmp_path / "out-b",
                      spec=spec, methods_root=METHODS)

    copied = tmp_path / "out-a" / "backend/src/main/java/com/agentforge/template/domain/StatusGuard.java"
    assert copied.is_file()
    assert "class StatusGuard" in copied.read_text(encoding="utf-8")
    lock_json = json.loads((tmp_path / "out-a" / "asset-lock.json").read_text(encoding="utf-8"))
    assert lock_json["schema_version"] == "0.2"
    assert [u["id"] for u in lock_json["method_units"]] == ["status-guard"]
    assert first.manifest_hash == second.manifest_hash  # 重复装配可复现


# --- 生成图端到端 -------------------------------------------------------------

class _PassRunner:
    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


def _seed_confirmed_run(factory, spec: SoftwareSpecification) -> int:
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        row = SpecVersion(
            project_id=project.id, version=1,
            body_json=canonical_json(spec), hash=content_hash(spec),
            confirmed_at=datetime(2026, 1, 1),
        )
        session.add(row)
        session.flush()
        project.active_spec_id = row.id
        run = create_run(session, project, row.id, idempotency_key="k1", spec_hash=content_hash(spec))
        return run.id


def test_generation_graph_with_methods_end_to_end(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed_confirmed_run(factory, _spec(["status-guard"]))
    context = GenerationContext(
        session_factory=factory, runner=_PassRunner(),
        work_root=tmp_path / "runs", registry=default_registry(),
        methods_root=METHODS,
    )

    result = run_generation(context, run_id)

    assert result["ok"] is True
    run_dir = tmp_path / "runs" / f"run-{run_id}"
    lock_json = json.loads((run_dir / "asset-lock.json").read_text(encoding="utf-8"))
    assert lock_json["schema_version"] == "0.2"
    assert (run_dir / "backend/src/main/java/com/agentforge/template/domain/StatusGuard.java").is_file()
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"


def test_generation_graph_without_methods_unchanged(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed_confirmed_run(factory, _spec())
    context = GenerationContext(
        session_factory=factory, runner=_PassRunner(),
        work_root=tmp_path / "runs", registry=default_registry(),
        methods_root=METHODS,
    )

    run_generation(context, run_id)

    run_dir = tmp_path / "runs" / f"run-{run_id}"
    lock_json = json.loads((run_dir / "asset-lock.json").read_text(encoding="utf-8"))
    assert lock_json["schema_version"] == "0.1"
    assert lock_json["method_units"] == []
    assert not (run_dir / "backend/src/main/java/com/agentforge/template/util/StatusGuard.java").exists()
