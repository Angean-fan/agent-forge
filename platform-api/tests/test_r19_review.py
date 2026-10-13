"""R19：完整复验与最多一轮技术语义审查。

覆盖：完整复验缺项判定；审查上下文的冻结性与「不含对话历史」；问题→可验证修复任务的转换；
语义审查经 technical_planning（Qwen3.7 Plus）结构化调用并失败不伪装；审查不覆盖确定性结果；
报告保留首次失败与最终复验、索引版本/采用-拒绝方法/装配与 Agent 修改来源链；
以及「方法命中且构建成功但固定业务 HTTP 失败不得标为已覆盖」。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentforge_platform.agent_files import FileBackend
from agentforge_platform.asset_registry import default_registry
from agentforge_platform.clients import Completion, Usage
from agentforge_platform.contract import compile_from_spec
from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.models import ModelCall, Project, Run, RunTask, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.review import (
    ReviewFinding,
    assert_full_verification,
    build_review_context,
    findings_to_tasks,
    missing_required_checks,
    run_semantic_review,
)
from agentforge_platform.runs import create_run, run_directory
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
SERVICE_DIR = "backend/src/main/java/com/agentforge/template/service/"


def _spec(path: Path = TASK) -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(path.read_text(encoding="utf-8"))


def _checks(**status_by_name: str) -> dict:
    checks = [{"name": name, "status": status_by_name.get(name, "PASS")} for name in VERIFY_REQUIRED]
    for check in checks:
        if check["status"] == "FAIL":
            check["error_code"] = "BUILD_FAILED"
    return {"overall": "FAIL" if any(c["status"] == "FAIL" for c in checks) else "PASS", "checks": checks}


def _accept(overall: str = "PASS") -> dict:
    return {"overall": overall, "cases": [{"name": "login_admin", "status": overall}]}


class _FakeRunner:
    def __init__(self, sequence: list[tuple[dict, dict]]) -> None:
        self._verify_seq = [item[0] for item in sequence]
        self._accept_seq = [item[1] for item in sequence]

    def verify(self, artifact_dir: str) -> dict:
        return self._verify_seq.pop(0) if self._verify_seq else _checks()

    def accept(self, artifact_dir: str) -> dict:
        return self._accept_seq.pop(0) if self._accept_seq else _accept()


class _FakeReviewClient:
    def __init__(self, content: str, finish: str = "stop") -> None:
        self._content, self._finish = content, finish

    def complete(self, *, model, prompt, thinking_mode=None, json_mode=False,
                 max_output_tokens=None, reasoning_effort=None) -> Completion:
        return Completion(content=self._content, model=model, request_id="r",
                          usage=Usage(10, 5), finish_reason=self._finish)


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


# --- 完整复验 -----------------------------------------------------------------

def test_missing_required_checks_and_assert() -> None:
    partial = {"overall": "PASS", "checks": [{"name": "static_manifest", "status": "PASS"}]}
    assert set(missing_required_checks(partial)) == {"backend_test", "backend_build", "frontend_build"}
    with pytest.raises(AssertionError):
        assert_full_verification(partial)
    assert_full_verification(_checks())  # 完整 → 通过


# --- 审查上下文与任务转换 -----------------------------------------------------

def test_review_context_is_frozen_and_has_no_chat(tmp_path: Path) -> None:
    spec = _spec()
    contract = compile_from_spec(spec)
    run_dir = run_directory(tmp_path / "runs", 1)
    (run_dir / SERVICE_DIR).mkdir(parents=True)
    (run_dir / SERVICE_DIR / "TaskService.java").write_text("class TaskService {}\n", encoding="utf-8")

    context = build_review_context(spec, contract, _checks(), _accept(), run_dir=run_dir)

    assert context["spec"]["goal"]
    assert context["contract"]["tables"] and context["contract"]["routes"]
    assert context["deterministic_results"]["verification_overall"] == "PASS"
    assert any(f["path"].endswith("TaskService.java") for f in context["authorized_code"])
    assert "messages" not in context and "chat" not in context  # 不含对话历史


def test_findings_to_tasks_converts_to_repair_tasks() -> None:
    findings = [
        ReviewFinding(summary="缺失状态守卫", severity="major", related_acceptance_ids=["AC02"],
                      suggested_operation="add_status_guard"),
        ReviewFinding(summary="越权读取", severity="blocker"),
    ]
    tasks = findings_to_tasks(findings, compile_from_spec(_spec()))

    assert [t["task_id"] for t in tasks] == ["review:0", "review:1"]
    assert tasks[0]["acceptance_ids"] == ["AC02"] and tasks[0]["operation"] == "add_status_guard"
    assert all(SERVICE_DIR in t["allowed_paths"] for t in tasks)  # 有限授权


# --- 语义审查调用（结构化，失败不伪装） ---------------------------------------

def test_run_semantic_review_parses_findings(platform_app, monkeypatch: pytest.MonkeyPatch) -> None:
    _, factory = platform_app
    run_id = _seed(factory, _spec())
    monkeypatch.setenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "1")
    client = _FakeReviewClient(json.dumps({"findings": [
        {"summary": "缺少状态机", "severity": "major", "related_acceptance_ids": ["AC02"]}
    ]}))

    with session_scope(factory) as session:
        result = run_semantic_review(session, project_id=1, run_id=run_id, client=client, context={})

    assert result.invoked and result.error is None
    assert [f.summary for f in result.findings] == ["缺少状态机"]
    with session_scope(factory) as session:
        rows = session.query(ModelCall).filter(ModelCall.model_role == "technical_planning").all()
        assert len(rows) == 1 and rows[0].model == "qwen3.7-plus"  # 路由/成本日志


def test_run_semantic_review_invalid_json_marked_error(platform_app, monkeypatch: pytest.MonkeyPatch) -> None:
    _, factory = platform_app
    run_id = _seed(factory, _spec())
    monkeypatch.setenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "1")

    with session_scope(factory) as session:
        result = run_semantic_review(session, project_id=1, run_id=run_id,
                                     client=_FakeReviewClient("not json"), context={})

    assert result.invoked and result.error == "REVIEW_INVALID" and result.findings == []


# --- 生成图：审查 + 报告留痕 --------------------------------------------------

def _context(factory, tmp_path: Path, runner, **kwargs) -> GenerationContext:
    return GenerationContext(session_factory=factory, runner=runner,
                             work_root=tmp_path / "runs", registry=default_registry(), **kwargs)


def _review_finding_factory(monkeypatch: pytest.MonkeyPatch) -> _FakeReviewClient:
    monkeypatch.setenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "1")
    return _FakeReviewClient(json.dumps({"findings": [
        {"summary": "建议补状态守卫", "severity": "minor", "related_acceptance_ids": ["AC02"]}
    ]}))


def test_success_writes_report_with_review_and_provenance(platform_app, tmp_path: Path, monkeypatch) -> None:
    _, factory = platform_app
    run_id = _seed(factory, _spec())
    client = _review_finding_factory(monkeypatch)

    result = run_generation(_context(factory, tmp_path, _FakeRunner([(_checks(), _accept())]),
                                     review_client=client, planning_client=client), run_id)

    assert result["ok"] is True, result
    run_dir = tmp_path / "runs" / f"run-{run_id}"
    report = json.loads((run_dir / "report.json").read_text("utf-8"))
    assert report["verified"] is True and report["first_failure"] is None
    assert report["final_verification"]["verification"]["overall"] == "PASS"
    assert report["provenance"]["manifest_hash"]
    assert "agent_modifications" in report["provenance"]
    assert report["semantic_review"]["findings"][0]["summary"] == "建议补状态守卫"
    assert (run_dir / "review.json").is_file()
    with session_scope(factory) as session:
        assert session.query(RunTask).filter(RunTask.task_id == "review:0").count() == 1


def test_review_does_not_override_deterministic_failure(platform_app, tmp_path: Path, monkeypatch) -> None:
    _, factory = platform_app
    run_id = _seed(factory, _spec())
    client = _review_finding_factory(monkeypatch)  # 审查认为可改进，但确定性检查失败

    result = run_generation(_context(factory, tmp_path,
                                     _FakeRunner([(_checks(backend_build="FAIL"), _accept())]),
                                     review_client=client, planning_client=client), run_id)

    assert result["ok"] is False  # 模型评价不覆盖失败检查
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "FAILED"


def test_report_retains_first_failure_after_repair(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed(factory, _spec())
    work_root = tmp_path / "runs"
    run_dir = run_directory(work_root, run_id)
    counter = {"n": 0}

    def factory_engine(role, allowed, _task):
        fb = FileBackend(run_dir, list(allowed))

        def _run(_prompt):
            counter["n"] += 1
            fb.write(SERVICE_DIR + "Fix.java", f"class Fix {{ int v{counter['n']}; }}\n".encode())
            return {"change_set": fb.build_change_set().to_dict()}

        return SimpleNamespace(run=_run), fb

    runner = _FakeRunner([
        (_checks(backend_build="FAIL"), _accept()),  # verify 节点
        (_checks(backend_build="FAIL"), _accept()),  # repair 初始复验
        (_checks(), _accept()),                      # 修复后复验
    ])
    context = GenerationContext(session_factory=factory, runner=runner, work_root=work_root,
                                registry=default_registry(), repair_engine_factory=factory_engine)
    result = run_generation(context, run_id)

    assert result["ok"] is True, result
    report = json.loads((work_root / f"run-{run_id}" / "report.json").read_text("utf-8"))
    assert report["first_failure"] is not None  # 首次失败被保留
    assert report["first_failure"]["verification"]["overall"] == "FAIL"
    assert report["final_verification"]["verification"]["overall"] == "PASS"  # 最终复验 PASS
    assert report["provenance"]["agent_modifications"]["repair"]["attempts"] == 1


def test_method_hit_build_ok_but_accept_fail_is_not_covered(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed(factory, _spec())

    # 构建/静态全 PASS，但固定业务 HTTP 验收 FAIL → 不得标为已覆盖、不得 SUCCEEDED。
    result = run_generation(_context(factory, tmp_path,
                                     _FakeRunner([(_checks(), _accept("FAIL"))])), run_id)

    assert result["ok"] is False
    run_dir = tmp_path / "runs" / f"run-{run_id}"
    report = json.loads((run_dir / "report.json").read_text("utf-8"))
    assert report["verified"] is False
    coverage = json.loads((run_dir / "coverage.json").read_text("utf-8"))
    assert coverage["acceptance_passed"] is False and coverage["gaps"]  # 未被覆盖
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "FAILED"
