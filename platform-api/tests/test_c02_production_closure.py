"""C02：技术规划、受限 Patch、修复与 Runner 的生产链路闭环（S2）。

本文件用**真实生产装配对象**（`build_production_routing` → `default_context` → `run_generation`）
驱动端到端路径，覆盖：

- **run ① 标准已覆盖 Task**：开关全关 → 确定性 COPY/RENDER，Qwen 编码调用数为零，旧行为不变；
- **run ② 受控缺口（模拟）**：开关开+凭据（dummy）+ 脚本化模型 → 生产路由构造受限引擎 →
  受限暂存提交 → Runner 复验 → coverage 记录 Gap/资产来源 → 账本记录 usage/run/task/call；
- **负例**：Provider 无工具能力（模型不产出工具调用）→ 诚实失败、不产生变更；
- **worker 闭环**：`build_generation_handler` + `run_worker` 领取并完成一个 run。

真实模型 run ②（付费）与真实 Runner e2e 本机不可用 → **NOT_RUN**（见 `docs/C02_..._记录.md`）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

from agentforge_platform import worker as worker_mod
from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import default_context, run_generation
from agentforge_platform.models import ModelCall, Project, Run, RunTask, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.production_routing import build_production_routing
from agentforge_platform.runs import claim_run, create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
SERVICE_DIR = "backend/src/main/java/com/agentforge/template/service/"
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


class _PassRunner:
    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


def _spec(path: Path) -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(path.read_text(encoding="utf-8"))


def _report_spec() -> SoftwareSpecification:
    """未知主线形状（单实体 Report）→ 平台无固定验收证据 → 真实缺口（no_candidate）。"""
    return SoftwareSpecification.model_validate({
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
        "acceptance_cases": [{"id": "AC01", "scenario": "成员创建报表", "expected": "成功",
                              "source": "user_confirmed"}],
        "technical_profile": {"profile_id": "demo-ts-mp", "profile_version": "0.3",
                              "template_version": "demo-0.2"},
    })


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
        return create_run(session, project, row.id, idempotency_key="c02",
                          spec_hash=content_hash(spec)).id


def _scripted_generate(responses: list[Any]):
    """替换 ChatOpenAI._generate：按序返回脚本化消息，携带 usage（无网络）。"""
    queue = list(responses)

    def _fake(self, messages, stop=None, run_manager=None, **kwargs):
        message = queue.pop(0) if queue else AIMessage(content="done")
        return ChatResult(
            generations=[ChatGeneration(message=message)],
            llm_output={"token_usage": {"prompt_tokens": 12, "completion_tokens": 6}},
        )

    return _fake


def _write_tool_message(path: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": "write_file", "args": {"file_path": "/" + path, "content": "class Patched {}\n"},
        "id": "c1", "type": "tool_call"}])


def _checks(**overrides) -> dict:
    status = {name: "PASS" for name in VERIFY_REQUIRED}
    status.update(overrides)
    overall = "FAIL" if any(value != "PASS" for value in status.values()) else "PASS"
    return {"overall": overall, "checks": [{"name": n, "status": s} for n, s in status.items()]}


class _SeqRunner:
    """按序返回 verify 结果（首次失败 → 修复后通过）。"""

    def __init__(self, sequence: list[dict]) -> None:
        self._sequence = list(sequence)

    def verify(self, artifact_dir: str) -> dict:
        return self._sequence.pop(0) if self._sequence else _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


def _enable_coding(monkeypatch, tmp_path: Path, responses: list[Any]) -> None:
    monkeypatch.setenv("AGENTFORGE_CODING_ENABLED", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dummy-key")
    price_file = tmp_path / "price.json"
    price_file.write_text(json.dumps({
        "version": "c02-1",
        "models": {"qwen3-coder-flash": {"input_micro_per_million": 1_000_000,
                                         "output_micro_per_million": 2_000_000}},
    }), encoding="utf-8")
    monkeypatch.setenv("AGENTFORGE_PRICE_FILE", str(price_file))
    monkeypatch.setattr(ChatOpenAI, "_generate", _scripted_generate(responses))


# --- run ①：标准已覆盖 Task（生产装配，开关全关） ------------------------------

def test_run1_standard_task_via_production_zero_coding_calls(
    platform_app, tmp_path: Path, monkeypatch
) -> None:
    _, factory = platform_app
    for flag in ("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "AGENTFORGE_CODING_ENABLED",
                 "AGENTFORGE_COMPLEX_REPAIR_ENABLED", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(flag, raising=False)
    run_id = _seed(factory, _spec(TASK))

    routing = build_production_routing(session_factory=factory, work_root=tmp_path / "runs")
    assert routing.coding_routing is None  # 开关全关 → 无授权编码路由
    context = default_context(
        session_factory=factory, work_root=tmp_path / "runs", runner=_PassRunner(), routing=routing
    )
    result = run_generation(context, run_id)

    assert result["ok"] is True, result
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"
        assert session.query(ModelCall).count() == 0  # 零编码/规划调用，旧行为不变


# --- run ②：受控缺口经生产路由进入受限 Patch（脚本化模型） ---------------------

def test_run2_controlled_gap_patched_via_production(
    platform_app, tmp_path: Path, monkeypatch
) -> None:
    _, factory = platform_app
    monkeypatch.setenv("AGENTFORGE_CODING_ENABLED", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dummy-key")
    monkeypatch.delenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", raising=False)
    price_file = tmp_path / "price.json"
    price_file.write_text(json.dumps({
        "version": "c02-1",
        "models": {"qwen3-coder-flash": {"input_micro_per_million": 1_000_000,
                                         "output_micro_per_million": 2_000_000}},
    }), encoding="utf-8")
    monkeypatch.setenv("AGENTFORGE_PRICE_FILE", str(price_file))
    monkeypatch.setattr(
        ChatOpenAI, "_generate",
        _scripted_generate([
            _write_tool_message(SERVICE_DIR + "PatchedByAgent.java"),
            AIMessage(content="done"),
        ]),
    )
    run_id = _seed(factory, _report_spec())
    methods_root = tmp_path / "methods"  # 空方法树：无兼容方法 → 走真实 Patch
    methods_root.mkdir()
    work_root = tmp_path / "runs"

    routing = build_production_routing(session_factory=factory, work_root=work_root)
    assert routing.coding_routing is not None  # 开关开+凭据 → 生产编码路由就绪
    context = default_context(
        session_factory=factory, work_root=work_root, runner=_PassRunner(),
        methods_root=methods_root, routing=routing,
    )
    result = run_generation(context, run_id)

    assert result["ok"] is True, result
    run_dir = work_root / f"run-{run_id}"
    assert (run_dir / SERVICE_DIR / "PatchedByAgent.java").is_file()  # 受限暂存→原子提交
    coverage = json.loads((run_dir / "coverage.json").read_text("utf-8"))
    assert coverage["resolutions"][0]["action"] == "patch"  # Gap 被受限 Patch 覆盖
    assert coverage["gaps"] and coverage["gaps"][0]["acceptance_id"] == "AC01"

    with session_scope(factory) as session:
        rows = session.query(ModelCall).filter(ModelCall.run_id == run_id).all()
    # 生产路径真实进入 coding 模型：账本记录 provider/model_role/run/task/usage
    assert rows and all(r.provider == "dashscope" for r in rows)
    assert all(r.model_role == "coding" and r.status == "SETTLED" for r in rows)
    assert all(r.task_id and r.call_id for r in rows)  # run/task/call 可审计
    assert all(r.input_tokens == 12 and r.output_tokens == 6 for r in rows)


# --- 负例：Provider 无工具能力（模型不产出工具调用） ---------------------------

def test_negative_provider_without_tool_calls_fails_honestly(
    platform_app, tmp_path: Path, monkeypatch
) -> None:
    _, factory = platform_app
    monkeypatch.setenv("AGENTFORGE_CODING_ENABLED", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dummy-key")
    monkeypatch.setattr(ChatOpenAI, "_generate", _scripted_generate([AIMessage(content="无法完成")]))
    run_id = _seed(factory, _report_spec())
    methods_root = tmp_path / "methods"
    methods_root.mkdir()
    work_root = tmp_path / "runs"

    routing = build_production_routing(session_factory=factory, work_root=work_root)
    context = default_context(
        session_factory=factory, work_root=work_root, runner=_PassRunner(),
        methods_root=methods_root, routing=routing,
    )
    result = run_generation(context, run_id)

    # 无工具调用 → 空变更集 → 诚实失败，不产生文件，不伪造成 PASS
    assert result["ok"] is False, result
    run_dir = work_root / f"run-{run_id}"
    assert not (run_dir / SERVICE_DIR / "PatchedByAgent.java").exists()


# --- worker 闭环：build_generation_handler + run_worker ------------------------

def test_worker_handler_drives_run_to_success(platform_app, tmp_path: Path, monkeypatch) -> None:
    _, factory = platform_app
    for flag in ("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "AGENTFORGE_CODING_ENABLED",
                 "AGENTFORGE_COMPLEX_REPAIR_ENABLED", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(worker_mod, "HttpRunnerClient", lambda **_: _PassRunner())
    run_id = _seed(factory, _spec(TASK))

    handler = worker_mod.build_generation_handler(factory)
    worker_mod.run_worker(session_factory=factory, handler=handler, worker_id="w-c02",
                          iterations=2, poll_interval=0.0)

    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"
        assert claim_run(session, "w-probe") is None  # 已终结，不再可领


# --- 修复链路经生产路由（R18） -------------------------------------------------

def test_repair_via_production_routing(platform_app, tmp_path: Path, monkeypatch) -> None:
    _, factory = platform_app
    monkeypatch.delenv("AGENTFORGE_COMPLEX_REPAIR_ENABLED", raising=False)
    _enable_coding(monkeypatch, tmp_path, [
        _write_tool_message(SERVICE_DIR + "Fix.java"),
        AIMessage(content="done"),
    ])
    run_id = _seed(factory, _spec(TASK))
    work_root = tmp_path / "runs"

    routing = build_production_routing(session_factory=factory, work_root=work_root)
    runner = _SeqRunner([_checks(backend_build="FAIL"), _checks(backend_build="FAIL"), _checks()])
    context = default_context(
        session_factory=factory, work_root=work_root, runner=runner, routing=routing
    )
    result = run_generation(context, run_id)

    assert result["ok"] is True, result
    assert (work_root / f"run-{run_id}" / SERVICE_DIR / "Fix.java").is_file()
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"
        repair = session.query(RunTask).filter(RunTask.task_id == "repair:verify").one()
        assert repair.attempt == 1 and repair.status == "DONE"
        rows = session.query(ModelCall).filter(ModelCall.run_id == run_id).all()
    assert rows and all(r.model_role == "coding" and r.status == "SETTLED" for r in rows)
