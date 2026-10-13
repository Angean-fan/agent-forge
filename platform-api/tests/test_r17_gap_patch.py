"""R17：装配优先，缺口局部 Patch。

覆盖：缺口决策（reuse/patch/refuse）；TaskContext 仅含冻结上下文且不含对话历史；变更集提交前确定性校验；
受限引擎产出的变更集经校验后原子提交并与检索轨迹关联；以及生成图三条 WhenDone 判据：
(a) 受控缺口由 Agent 小范围补全、变更集可审查；(b) 标准 Task 无缺口时不启动 Agent；
(c) 补充经审核的兼容方法资产后重跑，在**无 Coding Agent** 情况下复用该资产并通过。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agentforge_platform.agent_files import FileBackend
from agentforge_platform.asset_finder import AssetSearchResult
from agentforge_platform.asset_registry import default_registry
from agentforge_platform.contract import compile_from_spec
from agentforge_platform.coverage import CoverageGap
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException
from agentforge_platform.gap_patch import (
    PATCH,
    REFUSE,
    REUSE,
    build_task_context,
    choose_gap_action,
    render_task_prompt,
    resolve_gaps,
    validate_change_set,
)
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.method_lock import resolve_lock
from agentforge_platform.models import ModelCall, Project, Run, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.patch_engine import LedgerBinding, build_patch_engine
from agentforge_platform.runs import create_run, run_directory
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
SERVICE_DIR = "backend/src/main/java/com/agentforge/template/service/"
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


def _spec(path: Path) -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(path.read_text(encoding="utf-8"))


def _report_spec(**overrides) -> SoftwareSpecification:
    """未知主线形状：单实体 Report（平台无固定验收证据 → 真实缺口）。"""
    payload = {
        "schema_version": "0.1", "project_type": "crud_web", "goal": "报表管理。",
        "actors": ["ADMIN", "MEMBER"],
        "entities": [{"name": "Report", "source": "user_confirmed", "fields": [
            {"name": "id", "type": "long", "required": True},
            {"name": "title", "type": "string", "required": True},
            {"name": "status", "type": "string", "required": True},
            {"name": "owner_id", "type": "long", "required": True},
            {"name": "created_at", "type": "datetime", "required": True}]}],
        "permissions": [
            {"role": "ADMIN", "actions": ["create", "read", "update", "delete"], "source": "user_confirmed"},
            {"role": "MEMBER", "actions": ["create", "read", "update", "delete"], "source": "user_confirmed"}],
        "business_rules": [{"id": "BR01", "text": "报表标题必填", "source": "user_confirmed"}],
        "acceptance_cases": [{"id": "AC01", "scenario": "成员创建报表", "expected": "成功", "source": "user_confirmed"}],
        "technical_profile": {"profile_id": "demo-ts-mp", "profile_version": "0.3", "template_version": "demo-0.2"},
    }
    payload.update(overrides)
    return SoftwareSpecification.model_validate(payload)


def _gap(**overrides) -> CoverageGap:
    payload = {"acceptance_id": "AC01", "code": "no_candidate", "reason": "无候选方法",
               "allowed_paths": (SERVICE_DIR,), "task_id": "gap:AC01"}
    payload.update(overrides)
    return CoverageGap(**payload)


def _search(selected: tuple[str, ...] = ()) -> AssetSearchResult:
    return AssetSearchResult(query_hash="q", index_revision=None, match_type="NO_MATCH",
                             candidate_refs=selected, selected_refs=selected)


class _ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    model_name: str = "qwen3-coder-flash"

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _get_ls_params(self, **kwargs):
        return {"ls_provider": "openai", "ls_model_name": self.model_name}

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = self.responses.pop(0) if self.responses else AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=message)])


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run-1"
    (run_dir / SERVICE_DIR).mkdir(parents=True)
    (run_dir / SERVICE_DIR / "ReportService.java").write_text("class ReportService {}\n", encoding="utf-8")
    return run_dir


def _engine_for(run_dir: Path, allowed: list[str], target_file: str):
    """构造受限引擎：脚本化模型向 allowed 内写一个文件。"""
    file_backend = FileBackend(run_dir, list(allowed))
    scripted = _ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{
            "name": "write_file",
            "args": {"file_path": "/" + target_file, "content": "class Helper {}\n"},
            "id": "c1", "type": "tool_call"}]),
        AIMessage(content="done"),
    ])
    engine = build_patch_engine(
        ledger_binding=LedgerBinding(session_factory=None, project_id=1, run_id=1, task_id="gap:AC01"),
        file_backend=file_backend, system_prompt="restricted", model=scripted,
    )
    return engine, file_backend


# --- 决策：装配优先 / patch / refuse -----------------------------------------

def test_choose_action_prefers_reuse_over_patch(tmp_path: Path) -> None:
    methods_root = _temp_methods_with_ac01(tmp_path)
    decision = choose_gap_action(_gap(), _search(selected=("method-unit:report-helper@1.0.0",)), methods_root)
    assert decision.action == REUSE and decision.reuse_ref == "method-unit:report-helper@1.0.0"


def test_choose_action_patches_when_no_method(tmp_path: Path) -> None:
    methods_root = _temp_methods_with_ac01(tmp_path)
    assert choose_gap_action(_gap(), _search(), methods_root).action == PATCH


def test_choose_action_refuses_without_extension_point(tmp_path: Path) -> None:
    methods_root = _temp_methods_with_ac01(tmp_path)
    decision = choose_gap_action(_gap(allowed_paths=()), _search(), methods_root)
    assert decision.action == REFUSE


# --- TaskContext 与变更集校验 -------------------------------------------------

def test_task_context_has_frozen_fields_and_no_chat(tmp_path: Path) -> None:
    spec = _report_spec()
    contract = compile_from_spec(spec)
    lock = resolve_lock(default_registry(), "demo-ts-mp", spec, contract)
    run_dir = _run_dir(tmp_path)

    context = build_task_context(_gap(), spec, contract, lock, run_dir=run_dir)

    assert context["acceptance_id"] == "AC01"
    assert context["allowed_paths"] == [SERVICE_DIR]
    assert context["spec_fragment"]["acceptance"][0]["id"] == "AC01"
    assert context["contract"]["tables"] and context["contract"]["routes"]
    assert SERVICE_DIR + "ReportService.java" in context["authorized_files"]
    assert "messages" not in context and "chat" not in context  # 不含对话历史
    assert context == build_task_context(_gap(), spec, contract, lock, run_dir=run_dir)  # 确定性
    assert "allowed_paths" in render_task_prompt(context)


def test_validate_change_set_rejects_unsafe() -> None:
    allowed = [SERVICE_DIR]
    with pytest.raises(AppException):
        validate_change_set({"changes": [{"path": "frontend/x.ts", "action": "create"}]}, allowed_paths=allowed)
    with pytest.raises(AppException):
        validate_change_set({"changes": [{"path": "../escape.java", "action": "create"}]}, allowed_paths=allowed)
    with pytest.raises(AppException):
        validate_change_set({"changes": []}, allowed_paths=allowed)
    validate_change_set(
        {"changes": [{"path": SERVICE_DIR + "X.java", "action": "create"}]}, allowed_paths=allowed
    )  # 通过


# --- resolve_gaps：reuse 不调用 Agent / patch 走引擎 --------------------------

def test_resolve_gaps_reuse_assembles_without_agent(tmp_path: Path) -> None:
    methods_root = _temp_methods_with_ac01(tmp_path)
    run_dir = _run_dir(tmp_path)
    spec = _report_spec()
    contract = compile_from_spec(spec)
    lock = resolve_lock(default_registry(), "demo-ts-mp", spec, contract)
    domain_dir = SERVICE_DIR.replace("service/", "domain/")

    def _explode(_allowed, _task):  # 复用路径绝不能调用引擎工厂
        raise AssertionError("复用缺口不应调用 Coding Agent")

    resolutions = resolve_gaps(
        [_gap()], search=_search(selected=("method-unit:report-helper@1.0.0",)), spec=spec,
        contract=contract, lock=lock, run_dir=run_dir, methods_root=methods_root,
        engine_factory=_explode,
    )
    assert resolutions[0].action == REUSE
    assert resolutions[0].detail["locked_unit"]["id"] == "report-helper"
    assert (run_dir / domain_dir / "ReportHelper.java").is_file()


def test_resolve_gaps_patch_commits_change_set(tmp_path: Path) -> None:
    methods_root = _temp_methods_with_ac01(tmp_path)
    run_dir = _run_dir(tmp_path)
    spec = _report_spec()
    contract = compile_from_spec(spec)
    lock = resolve_lock(default_registry(), "demo-ts-mp", spec, contract)
    target = SERVICE_DIR + "Helper.java"

    def factory(allowed, _task):
        return _engine_for(run_dir, allowed, target)

    resolutions = resolve_gaps(
        [_gap()], search=_search(), spec=spec, contract=contract, lock=lock,
        run_dir=run_dir, methods_root=methods_root, engine_factory=factory,
    )

    assert resolutions[0].action == PATCH
    assert (run_dir / target).read_text("utf-8") == "class Helper {}\n"
    detail = resolutions[0].detail
    assert detail["change_set"]["changes"][0]["action"] == "create"
    assert detail["correlation"]["query_hash"] == "q"  # 与检索轨迹关联


# --- 生成图三条 WhenDone 判据 -------------------------------------------------

class _PassRunner:
    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


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


def test_b_standard_task_no_gap_never_starts_agent(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed(factory, _spec(TASK))

    def _explode(_allowed, _task):
        raise AssertionError("标准主線无缺口时不应启动 Agent")

    context = GenerationContext(
        session_factory=factory, runner=_PassRunner(), work_root=tmp_path / "runs",
        registry=default_registry(), gap_engine_factory=_explode,
    )
    result = run_generation(context, run_id)

    assert result["ok"] is True, result
    coverage = json.loads((tmp_path / "runs" / f"run-{run_id}" / "coverage.json").read_text("utf-8"))
    assert coverage["gaps"] == []
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"
        assert session.query(ModelCall).filter(ModelCall.model_role == "coding").count() == 0


def test_a_controlled_gap_is_patched_by_agent(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed(factory, _report_spec())
    methods_root = tmp_path / "methods"  # 空方法树：无兼容方法 → 走 Patch
    methods_root.mkdir()
    work_root = tmp_path / "runs"
    run_dir = run_directory(work_root, run_id)  # 目录按 run_id 确定，可在运行前构造 FileBackend

    def gap_engine_factory(allowed, _task):
        return _engine_for(run_dir, allowed, SERVICE_DIR + "PatchedByAgent.java")

    context = GenerationContext(
        session_factory=factory, runner=_PassRunner(), work_root=work_root,
        registry=default_registry(), methods_root=methods_root,
        gap_engine_factory=gap_engine_factory,
    )
    result = run_generation(context, run_id)

    assert result["ok"] is True, result
    assert (run_dir / SERVICE_DIR / "PatchedByAgent.java").is_file()  # Agent 小范围补全并提交
    coverage = json.loads((run_dir / "coverage.json").read_text("utf-8"))
    assert coverage["resolutions"][0]["action"] == "patch"


def test_c_adding_reviewed_method_reuses_without_agent(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed(factory, _report_spec())
    methods_root = _temp_methods_with_ac01(tmp_path)  # 补充经审核兼容方法（声明 AC01）

    def _explode(_allowed, _task):
        raise AssertionError("有可复用兼容方法时不应启动 Agent")

    context = GenerationContext(
        session_factory=factory, runner=_PassRunner(), work_root=tmp_path / "runs",
        registry=default_registry(), methods_root=methods_root, gap_engine_factory=_explode,
    )
    result = run_generation(context, run_id)

    assert result["ok"] is True, result
    coverage = json.loads((tmp_path / "runs" / f"run-{run_id}" / "coverage.json").read_text("utf-8"))
    assert "AC01" in coverage["covered_acceptance_ids"]  # 复用资产使缺口被覆盖
    assert coverage["gaps"] == []
    assert coverage["resolutions"][0]["action"] == "reuse"


# --- 临时方法树：一份兼容且声明 AC01 的经审核方法 ------------------------------

def _temp_methods_with_ac01(tmp_path: Path) -> Path:
    from agentforge_platform.asset_registry import compute_checksum

    root = tmp_path / "reviewed-methods"
    d = root / "report-helper" / "1.0.0"
    (d / "src").mkdir(parents=True)
    (d / "src" / "ReportHelper.java").write_text(
        "package com.agentforge.template.domain;\nclass ReportHelper {}\n", encoding="utf-8"
    )
    manifest = {
        "id": "report-helper", "version": "1.0.0", "kind": "method-unit",
        "symbol": "com.agentforge.template.domain.ReportHelper",
        "signature": "class ReportHelper", "layer": "domain",
        "language": "java", "framework": "spring-boot",
        "description": "报表标题必填校验辅助。", "tags": ["report", "validation"],
        "provides": ["data_ownership"], "requires": [], "conflicts": [],
        "parameter_schema": {}, "extension_points": [], "compatible_profiles": ["demo-ts-mp"],
        "owned_paths": ["backend/src/main/java/com/agentforge/template/domain/"],
        "target_dir": "backend/src/main/java/com/agentforge/template/domain/",
        "checks": ["mvn test"], "license": "internal",
        "acceptance_ids": ["AC01"], "source_provenance": "internal:test/r17",
        "checksum": compute_checksum(d),
    }
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return root
