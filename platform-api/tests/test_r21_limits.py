"""R21：模型调用次数、工具循环与运行时限统一拦截。

覆盖：运行护栏默认值与环境覆盖；单次输出上限；全 run 模型调用次数（所有角色共用，访谈期草稿独立）；
Agent 工具循环超限映射为 TOOL_LIMIT_EXCEEDED；运行总时限触发 FAILED 并尽力清理 Runner；模型等待时限可配置。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agentforge_platform.agent_files import FileBackend
from agentforge_platform.asset_registry import default_registry
from agentforge_platform.clients import DeepSeekClient
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.limits import runtime_limits
from agentforge_platform.model_gateway import reserve
from agentforge_platform.models import Project, Run, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.patch_engine import LedgerBinding, build_patch_engine
from agentforge_platform.runs import create_run, record_task
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
SERVICE_DIR = "backend/src/main/java/com/agentforge/template/service/"
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


def _seed_run(factory) -> int:
    spec = SoftwareSpecification.model_validate_json(TASK.read_text(encoding="utf-8"))
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


# --- 护栏配置 -----------------------------------------------------------------

def test_defaults_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AGENTFORGE_MAX_MODEL_CALLS_PER_RUN", "AGENTFORGE_MAX_OUTPUT_TOKENS",
                "AGENTFORGE_MODEL_TIMEOUT_SECONDS", "AGENTFORGE_MAX_TOOL_OPS",
                "AGENTFORGE_RUN_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    limits = runtime_limits()
    assert (limits.max_model_calls_per_run, limits.max_output_tokens_per_call,
            limits.model_timeout_seconds, limits.max_tool_ops, limits.run_timeout_seconds) == (
        30, 12_000, 120, 40, 1200)
    assert limits.agent_recursion_limit == 2 * 40 + 5

    monkeypatch.setenv("AGENTFORGE_MAX_MODEL_CALLS_PER_RUN", "5")
    monkeypatch.setenv("AGENTFORGE_MODEL_TIMEOUT_SECONDS", "99")
    assert runtime_limits().max_model_calls_per_run == 5
    assert DeepSeekClient().timeout_seconds == 99  # 模型等待时限可配置


# --- 单次输出上限 -------------------------------------------------------------

def test_output_limit_exceeded_rejected(platform_app, monkeypatch: pytest.MonkeyPatch) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory)
    monkeypatch.setenv("AGENTFORGE_MAX_OUTPUT_TOKENS", "1000")

    with session_scope(factory) as session:
        record_task(session, session.get(Run, run_id), "t")
    with session_scope(factory) as session, pytest.raises(AppException) as error:
        reserve(session, call_id="c1", model_role="user_interaction", project_id=1,
                max_input_tokens=100, max_output_tokens=1001, run_id=run_id, task_id="t")
    assert error.value.code == "OUTPUT_LIMIT_EXCEEDED"


# --- 全 run 调用次数（所有角色共用） ------------------------------------------

def test_run_call_count_limit(platform_app, monkeypatch: pytest.MonkeyPatch) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory)
    monkeypatch.setenv("AGENTFORGE_MAX_MODEL_CALLS_PER_RUN", "2")
    monkeypatch.setenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "1")
    monkeypatch.setenv("AGENTFORGE_CODING_ENABLED", "1")

    with session_scope(factory) as session:
        record_task(session, session.get(Run, run_id), "t")
    with session_scope(factory) as session:
        reserve(session, call_id="c1", model_role="user_interaction", project_id=1,
                max_input_tokens=10, max_output_tokens=10, run_id=run_id, task_id="t")
        reserve(session, call_id="c2", model_role="technical_planning", project_id=1,
                max_input_tokens=10, max_output_tokens=10, run_id=run_id, task_id="t")  # 另一角色同计入
    with session_scope(factory) as session, pytest.raises(AppException) as error:
        reserve(session, call_id="c3", model_role="coding", project_id=1,
                max_input_tokens=10, max_output_tokens=10, run_id=run_id, task_id="t")
    assert error.value.code == "CALL_LIMIT_EXCEEDED"


def test_interview_draft_limit_is_independent(platform_app, monkeypatch: pytest.MonkeyPatch) -> None:
    _, factory = platform_app
    monkeypatch.setenv("AGENTFORGE_MAX_MODEL_CALLS_PER_RUN", "1")  # run 限额很紧

    # 访谈期草稿调用（run_id 为空）走独立的 project/draft 限额，不受 run 限额影响
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
    with session_scope(factory) as session:
        row = reserve(session, call_id="d1", model_role="user_interaction", project_id=1,
                      max_input_tokens=10, max_output_tokens=10, draft_id=None)
        assert row.run_id is None and row.draft_id is None  # 生成前：run 级为空且可追溯


# --- Agent 工具循环上限 -------------------------------------------------------

class _LoopingModel(BaseChatModel):
    """永远返回工具调用、从不结束的脚本化模型（用于触发工具循环上限）。"""

    model_name: str = "qwen3-coder-flash"

    @property
    def _llm_type(self) -> str:
        return "looping"

    def _get_ls_params(self, **kwargs):
        return {"ls_provider": "openai", "ls_model_name": self.model_name}

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[{
            "name": "write_file", "args": {"file_path": "/" + SERVICE_DIR + "X.java", "content": "x"},
            "id": "c", "type": "tool_call"}]))])


def test_tool_loop_limit_maps_to_tool_limit_exceeded(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-1"
    (run_dir / SERVICE_DIR).mkdir(parents=True)
    backend = FileBackend(run_dir, [SERVICE_DIR])
    engine = build_patch_engine(
        ledger_binding=LedgerBinding(session_factory=None, project_id=1, run_id=1, task_id="t"),
        file_backend=backend, system_prompt="loop", model=_LoopingModel(),
    )
    with pytest.raises(AppException) as error:
        engine.run("loop forever", recursion_limit=5)  # 极小递归上限 → 工具循环超限
    assert error.value.code == "TOOL_LIMIT_EXCEEDED"


# --- 运行总时限 ---------------------------------------------------------------

class _CleanupRunner:
    def __init__(self) -> None:
        self.cleaned: list[str] = []

    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT

    def cleanup(self, artifact_dir: str) -> None:
        self.cleaned.append(artifact_dir)


def test_run_timeout_fails_and_cleans_runner(platform_app, tmp_path: Path, monkeypatch) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory)
    monkeypatch.setenv("AGENTFORGE_RUN_TIMEOUT_SECONDS", "0")  # 立即超时
    runner = _CleanupRunner()

    context = GenerationContext(session_factory=factory, runner=runner,
                                work_root=tmp_path / "runs", registry=default_registry())
    result = run_generation(context, run_id)

    assert result["ok"] is False and result["error_code"] == "RUN_TIMEOUT"
    assert runner.cleaned == [f"run-{run_id}"]  # 超时触发 Runner 清理（尽力）
    with session_scope(factory) as session:
        run = session.get(Run, run_id)
        assert run.status == "FAILED" and run.error_code == "RUN_TIMEOUT"
