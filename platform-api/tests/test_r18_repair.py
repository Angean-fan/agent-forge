"""R18：失败分类与有限自动修复。

覆盖：失败分类（可复现代码/契约错误 vs 基础设施）；角色升级（第 2 次才升到 complex_repair，且需已开启/在额度内）；
每任务与全 run 上限；基础设施不计业务次数、不启动引擎；修复前资产替换（复用，不调用 Agent）；
以及生成图接入：注入编译错误→第 1 轮 coding 修复并复验；配置默认关闭时不修复。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentforge_platform import model_gateway
from agentforge_platform.agent_files import FileBackend
from agentforge_platform.asset_finder import AssetSearchResult
from agentforge_platform.asset_registry import default_registry
from agentforge_platform.contract import compile_from_spec
from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.models import ModelCall, Project, Run, RunTask, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.repair import (
    CODE,
    CODING_ROLE,
    COMPLEX_ROLE,
    INFRASTRUCTURE,
    PASS,
    RepairLimits,
    choose_repair_role,
    classify_repair_failure,
    replacement_candidate,
    run_repair_loop,
)
from agentforge_platform.runs import create_run, run_directory
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
SERVICE_DIR = "backend/src/main/java/com/agentforge/template/service/"
_TARGET = SERVICE_DIR + "Fix.java"


def _spec(path: Path) -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(path.read_text(encoding="utf-8"))


def _checks(**status_by_name: str) -> dict:
    checks = [{"name": name, "status": status_by_name.get(name, "PASS")} for name in VERIFY_REQUIRED]
    for check in checks:
        if check["status"] == "FAIL":
            check["error_code"] = "BUILD_FAILED"
    return {"overall": "FAIL" if any(c["status"] == "FAIL" for c in checks) else "PASS", "checks": checks}


def _accept(overall: str = "PASS") -> dict:
    return {"overall": overall, "cases": [{"name": "login_admin", "status": overall}]}


class _ScriptedRunner:
    def __init__(self, sequence: list[dict]) -> None:
        self._sequence = list(sequence)
        self.verify_calls = 0

    def verify(self, artifact_dir: str) -> dict:
        self.verify_calls += 1
        return self._sequence.pop(0) if self._sequence else _checks(backend_build="PASS")

    def accept(self, artifact_dir: str) -> dict:
        return _accept()


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run-1"
    (run_dir / SERVICE_DIR).mkdir(parents=True)
    return run_dir


def _stub_factory(run_dir: Path, roles_seen: list[str], *, fail_write: bool = False):
    counter = {"n": 0}

    def factory(role: str, allowed: list[str], _task: str):
        roles_seen.append(role)
        file_backend = FileBackend(run_dir, list(allowed))

        def _run(_prompt: str) -> dict:
            if fail_write:
                raise AssertionError("不应调用引擎")
            counter["n"] += 1
            file_backend.write(_TARGET, f"class Fix {{ int v{counter['n']}; }}\n".encode())
            return {"change_set": file_backend.build_change_set().to_dict()}

        return SimpleNamespace(run=_run), file_backend

    return factory


# --- 失败分类 -----------------------------------------------------------------

def test_classification_code_vs_infrastructure() -> None:
    assert classify_repair_failure(_checks(backend_build="PASS"), _accept()) == PASS
    assert classify_repair_failure(_checks(backend_build="FAIL"), _accept()) == CODE
    # NOT_RUN / 基础设施错误码 → 不修、不计业务次数
    assert classify_repair_failure(
        {"overall": "NOT_RUN", "checks": [{"name": "backend_build", "status": "NOT_RUN",
                                           "error_code": "INFRASTRUCTURE"}]}, _accept()) == INFRASTRUCTURE
    for code in ("UNKNOWN_CHECK", "NO_TESTS"):
        assert classify_repair_failure(
            {"overall": "NOT_RUN", "checks": [{"name": "backend_test", "status": "NOT_RUN",
                                               "error_code": code}]}, _accept()) == INFRASTRUCTURE


def test_choose_repair_role_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTFORGE_COMPLEX_REPAIR_ENABLED", raising=False)
    assert choose_repair_role(0) == CODING_ROLE
    assert choose_repair_role(1) == CODING_ROLE  # 未开启升级 → 仍 coding

    monkeypatch.setenv("AGENTFORGE_COMPLEX_REPAIR_ENABLED", "1")
    monkeypatch.setenv("AGENTFORGE_CODING_ENABLED", "1")
    assert choose_repair_role(0) == CODING_ROLE
    assert choose_repair_role(1) == COMPLEX_ROLE  # 第 2 次才升级
    # 路由/成本日志：升级角色绑定 qwen3.8-max
    assert model_gateway.resolve_route(COMPLEX_ROLE).model == "qwen3.8-max"
    assert model_gateway.resolve_route(CODING_ROLE).model == "qwen3-coder-flash"


# --- 修复循环 -----------------------------------------------------------------

def _loop(run_dir: Path, runner: _ScriptedRunner, factory, **kwargs):
    contract = compile_from_spec(_spec(TASK))
    return run_repair_loop(
        verify=runner.verify, accept=runner.accept, artifact_dir=str(run_dir),
        contract=contract, run_dir=run_dir, allowed_paths=[SERVICE_DIR],
        task_id="repair:verify", engine_factory=factory, **kwargs,
    )


def test_first_attempt_calls_coding_and_reverifies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTFORGE_COMPLEX_REPAIR_ENABLED", raising=False)
    run_dir = _run_dir(tmp_path)
    roles: list[str] = []
    runner = _ScriptedRunner([_checks(backend_build="FAIL"), _checks()])  # 首次失败→修复→通过

    outcome = _loop(run_dir, runner, _stub_factory(run_dir, roles))

    assert outcome.classification == CODE and outcome.passed is True
    assert outcome.attempts == 1 and roles == [CODING_ROLE]
    assert (run_dir / _TARGET).is_file()  # 修复变更已提交


def test_second_attempt_escalates_to_complex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_COMPLEX_REPAIR_ENABLED", "1")
    run_dir = _run_dir(tmp_path)
    roles: list[str] = []
    runner = _ScriptedRunner([_checks(backend_build="FAIL"), _checks(backend_build="FAIL"), _checks()])

    outcome = _loop(run_dir, runner, _stub_factory(run_dir, roles))

    assert outcome.passed is True
    assert outcome.attempts == 2 and roles == [CODING_ROLE, COMPLEX_ROLE]


def test_stops_at_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTFORGE_COMPLEX_REPAIR_ENABLED", raising=False)
    run_dir = _run_dir(tmp_path)
    roles: list[str] = []
    runner = _ScriptedRunner([_checks(backend_build="FAIL")] * 5)  # 始终失败

    outcome = _loop(run_dir, runner, _stub_factory(run_dir, roles), limits=RepairLimits(per_task=2, per_run=6))

    assert outcome.passed is False and outcome.attempts == 2  # 每任务上限即停
    assert len(roles) == 2


def test_run_limit_blocks_further_repair(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    roles: list[str] = []
    runner = _ScriptedRunner([_checks(backend_build="FAIL")] * 3)

    outcome = _loop(run_dir, runner, _stub_factory(run_dir, roles),
                    limits=RepairLimits(per_task=5, per_run=6), run_used=6)

    assert outcome.attempts == 0 and roles == []  # 全 run 额度用尽即不修复


def test_infrastructure_is_not_repaired(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    roles: list[str] = []
    runner = _ScriptedRunner([{"overall": "NOT_RUN", "checks": [
        {"name": "backend_build", "status": "NOT_RUN", "error_code": "INFRASTRUCTURE"}]}])

    outcome = _loop(run_dir, runner, _stub_factory(run_dir, roles, fail_write=True))

    assert outcome.classification == INFRASTRUCTURE and outcome.attempts == 0
    assert roles == []  # 引擎未被调用（基础设施不计业务修复）


# --- 修复前资产替换（复用） ----------------------------------------------------

def _temp_methods(tmp_path: Path) -> Path:
    from agentforge_platform.asset_registry import compute_checksum

    d = tmp_path / "methods" / "report-helper" / "1.0.0"
    (d / "src").mkdir(parents=True)
    (d / "src" / "ReportHelper.java").write_text("class ReportHelper {}\n", encoding="utf-8")
    manifest = {
        "id": "report-helper", "version": "1.0.0", "kind": "method-unit",
        "symbol": "com.agentforge.template.domain.ReportHelper", "signature": "class ReportHelper",
        "layer": "domain", "language": "java", "framework": "spring-boot",
        "description": "x", "tags": ["report"], "provides": ["data_ownership"], "requires": [],
        "conflicts": [], "parameter_schema": {}, "extension_points": [],
        "compatible_profiles": ["demo-ts-mp"],
        "owned_paths": ["backend/src/main/java/com/agentforge/template/domain/"],
        "target_dir": "backend/src/main/java/com/agentforge/template/domain/",
        "checks": ["mvn test"], "license": "internal", "acceptance_ids": ["AC01"],
        "source_provenance": "internal:test/r18", "checksum": compute_checksum(d),
    }
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return tmp_path / "methods"


def test_replacement_candidate_and_reuse(tmp_path: Path) -> None:
    methods_root = _temp_methods(tmp_path)
    run_dir = _run_dir(tmp_path)
    spec = _spec(TASK)
    search = AssetSearchResult(query_hash="q", index_revision=None, match_type="EXACT",
                               candidate_refs=("method-unit:report-helper@1.0.0",),
                               selected_refs=("method-unit:report-helper@1.0.0",))

    assert replacement_candidate(search, spec, methods_root) == "method-unit:report-helper@1.0.0"

    roles: list[str] = []
    runner = _ScriptedRunner([_checks(backend_build="FAIL"), _checks()])
    outcome = _loop(run_dir, runner, _stub_factory(run_dir, roles, fail_write=True),
                    search=search, spec=spec, methods_root=methods_root)

    assert outcome.replaced_refs == ["method-unit:report-helper@1.0.0"]
    assert roles == [] and outcome.roles == ["reuse"]  # 复用替代修复，不调用 Agent
    assert (run_dir / "backend/src/main/java/com/agentforge/template/domain/ReportHelper.java").is_file()


# --- 生成图接入 ---------------------------------------------------------------

def _seed(factory, spec: SoftwareSpecification) -> int:
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        row = SpecVersion(project_id=project.id, version=1, body_json=canonical_json(spec),
                          hash=content_hash(spec), confirmed_at=datetime(2026, 1, 1))
        session.add(row)
        session.flush()
        project.active_spec_id = row.id
        return create_run(session, project, row.id, idempotency_key="k1",
                          spec_hash=content_hash(spec)).id


def test_generation_repairs_injected_compile_error(
    platform_app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = platform_app
    run_id = _seed(factory, _spec(TASK))
    work_root = tmp_path / "runs"
    run_dir = run_directory(work_root, run_id)
    # verify 调用序：verify 节点 → repair 初始复验（均 FAIL）→ 修复后复验 PASS
    runner = _ScriptedRunner([_checks(backend_build="FAIL"), _checks(backend_build="FAIL"), _checks()])
    roles: list[str] = []
    monkeypatch.delenv("AGENTFORGE_COMPLEX_REPAIR_ENABLED", raising=False)

    context = GenerationContext(
        session_factory=factory, runner=runner, work_root=work_root,
        registry=default_registry(), repair_engine_factory=_stub_factory(run_dir, roles),
    )
    result = run_generation(context, run_id)

    assert result["ok"] is True, result
    assert roles == [CODING_ROLE]
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"
        repair = session.query(RunTask).filter(RunTask.task_id == "repair:verify").one()
        assert repair.attempt == 1 and repair.status == "DONE"
        assert session.query(ModelCall).count() == 0  # 测试用脚本引擎，不产生真实模型调用


def test_generation_without_repair_factory_fails_and_records(
    platform_app, tmp_path: Path
) -> None:
    _, factory = platform_app
    run_id = _seed(factory, _spec(TASK))
    runner = _ScriptedRunner([_checks(backend_build="FAIL"), _checks(backend_build="FAIL")])

    context = GenerationContext(
        session_factory=factory, runner=runner, work_root=tmp_path / "runs",
        registry=default_registry(),  # 未注入 repair 工厂 → 不修复
    )
    result = run_generation(context, run_id)

    assert result["ok"] is False
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "FAILED"
        assert session.query(RunTask).filter(RunTask.task_id == "repair:verify").count() == 0
