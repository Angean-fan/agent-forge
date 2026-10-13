"""R16：DeepAgent 受限 patch engine。

免费层（不发起真实供应商调用）覆盖：受限工具表审计；``execute``/``task`` 移除且越权即拒；
暂存 backend 的读叠加视图与写入约束（allowed_paths/受保护/遍历）；coding 角色门禁与缺密钥前置拒绝；
以及**每次内部模型请求**都继承 run/task/call_id 并入账（不只统计外层 invoke）。

真实 Function Calling 探针见 ``scripts/probe_coding_tools.py``（需 DASHSCOPE_API_KEY，计费）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

from agentforge_platform import model_gateway, patch_engine
from agentforge_platform.agent_files import FileBackend
from agentforge_platform.clients import ProviderPreflightError, Usage
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException
from agentforge_platform.models import ModelCall, Project, Run, SpecVersion
from agentforge_platform.patch_engine import (
    RESTRICTED_TOOLS,
    LedgerBinding,
    LedgerChatModel,
    StagingBackend,
    assert_restricted_tools,
    build_coding_model,
    build_patch_engine,
)
from agentforge_platform.runs import create_run, record_task
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
CAMPUS = REPO / "schemas" / "campus-activity.spec.json"

SERVICE_DIR = "backend/src/main/java/com/agentforge/template/service/"


# --- 夹具 ---------------------------------------------------------------------

def _file_backend(tmp_path: Path, allowed: list[str] | None = None) -> FileBackend:
    run_dir = tmp_path / "run-1"
    (run_dir / SERVICE_DIR).mkdir(parents=True)
    (run_dir / SERVICE_DIR / "ReportService.java").write_text(
        "class ReportService {}\n", encoding="utf-8"
    )
    (run_dir / "asset-lock.json").write_text("{}\n", encoding="utf-8")
    return FileBackend(run_dir, allowed or [SERVICE_DIR])


class _ScriptedModel(BaseChatModel):
    """脚本化聊天模型：按序列返回消息，支持 bind_tools（无网络）。

    显式给出 ``model_name`` 与 ``ls_provider``，使 deepagents 能推导出 ``openai:qwen3-coder-flash``
    的 harness profile 键，从而与真实 ChatOpenAI 走同一条受限工具配置路径。
    """

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


def _seed_run(factory) -> int:
    spec = SoftwareSpecification.model_validate_json(CAMPUS.read_text(encoding="utf-8"))
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        row = SpecVersion(
            project_id=project.id,
            version=1,
            body_json=canonical_json(spec),
            hash=content_hash(spec),
            confirmed_at=datetime(2026, 1, 1),
        )
        session.add(row)
        session.flush()
        project.active_spec_id = row.id
        run = create_run(session, project, row.id, idempotency_key="k1", spec_hash=content_hash(spec))
        return run.id


# --- 暂存 backend：读叠加 + 写约束 --------------------------------------------

def test_backend_reads_source_and_overlays_staging(tmp_path: Path) -> None:
    backend = StagingBackend(_file_backend(tmp_path))
    path = "/" + SERVICE_DIR + "ReportService.java"

    assert backend.read(path).file_data["content"] == "class ReportService {}\n"

    backend.write(path, "class ReportService { /* extended */ }\n")
    assert "extended" in backend.read(path).file_data["content"]  # 读叠加视图看到暂存写入

    names = {entry["path"] for entry in backend.ls("/" + SERVICE_DIR).entries}
    assert path in names
    assert backend.glob("*.java", "/" + SERVICE_DIR).matches
    assert backend.grep("extended", "/").matches


def test_backend_write_outside_allowed_is_rejected(tmp_path: Path) -> None:
    backend = StagingBackend(_file_backend(tmp_path))
    result = backend.write("/backend/src/main/java/com/agentforge/template/controller/Evil.java", "x")
    assert result.error and "FORBIDDEN" not in result.error  # 返回错误而非落盘
    assert result.path is None


def test_backend_write_protected_is_rejected(tmp_path: Path) -> None:
    backend = StagingBackend(_file_backend(tmp_path, allowed=["."]))
    assert backend.write("/asset-lock.json", "x").error
    assert backend.edit("/asset-lock.json", "{}", "x").error


def test_backend_path_traversal_is_rejected(tmp_path: Path) -> None:
    backend = StagingBackend(_file_backend(tmp_path, allowed=["."]))
    with pytest.raises(AppException) as error:
        backend.read("/../outside.txt")
    assert error.value.code == "BAD_REQUEST"


# --- 工具表审计 ---------------------------------------------------------------

class _FakeBound:
    def __init__(self, names: list[str]) -> None:
        self.tools_by_name = {name: object() for name in names}


class _FakeAgent:
    def __init__(self, names: list[str]) -> None:
        self.nodes = {"tools": type("N", (), {"bound": _FakeBound(names)})()}


def test_assert_restricted_rejects_forbidden_tools() -> None:
    for bad in ("execute", "task"):
        with pytest.raises(AppException) as error:
            assert_restricted_tools(_FakeAgent(["read_file", bad]))
        assert error.value.code == "FORBIDDEN"

    with pytest.raises(AppException) as error:
        assert_restricted_tools(_FakeAgent(["read_file", "mystery_tool"]))
    assert error.value.code == "FORBIDDEN"

    assert assert_restricted_tools(_FakeAgent(list(RESTRICTED_TOOLS))) == set(RESTRICTED_TOOLS)


def test_engine_exposes_only_restricted_tools(tmp_path: Path) -> None:
    backend = _file_backend(tmp_path)
    engine = build_patch_engine(
        ledger_binding=LedgerBinding(
            session_factory=None, project_id=1, run_id=1, task_id="t"
        ),
        file_backend=backend,
        system_prompt="restricted",
        model=_ScriptedModel(responses=[]),
    )
    assert engine.tool_names == frozenset(RESTRICTED_TOOLS)
    assert not (engine.tool_names & patch_engine.FORBIDDEN_TOOL_NAMES)


def test_engine_run_stages_tool_call_into_change_set(tmp_path: Path) -> None:
    backend = _file_backend(tmp_path)
    target = "/" + SERVICE_DIR + "Note.java"
    scripted = _ScriptedModel(responses=[
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"file_path": target, "content": "class Note {}\n"},
                    "id": "call_1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="done"),
    ])
    engine = build_patch_engine(
        ledger_binding=LedgerBinding(session_factory=None, project_id=1, run_id=1, task_id="t"),
        file_backend=backend,
        system_prompt="restricted",
        model=scripted,
    )

    result = engine.run("create Note.java")

    changes = result["change_set"]["changes"]
    assert [c["path"] for c in changes] == [SERVICE_DIR + "Note.java"]
    assert changes[0]["action"] == "create"
    assert not (backend.run_dir / (SERVICE_DIR + "Note.java")).exists()  # 只在暂存，未提交正式源码
    assert (backend.staging / (SERVICE_DIR + "Note.java")).is_file()


# --- coding 角色门禁与缺密钥 --------------------------------------------------

def test_coding_role_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTFORGE_CODING_ENABLED", raising=False)
    with pytest.raises(AppException):
        model_gateway.resolve_route("coding")

    monkeypatch.setenv("AGENTFORGE_CODING_ENABLED", "1")
    assert model_gateway.resolve_route("coding").model == "qwen3-coder-flash"


def test_build_coding_model_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(ProviderPreflightError):
        build_coding_model(
            ledger_binding=LedgerBinding(
                session_factory=None, project_id=1, run_id=1, task_id="t"
            )
        )


# --- 内部请求逐次入账 ---------------------------------------------------------

def test_ledger_model_records_each_internal_call(
    platform_app, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory)
    monkeypatch.setenv("AGENTFORGE_CODING_ENABLED", "1")
    price_file = tmp_path / "price.json"
    price_file.write_text(
        json.dumps(
            {
                "version": "test-1",
                "models": {
                    "qwen3-coder-flash": {
                        "input_micro_per_million": 1_000_000,
                        "output_micro_per_million": 2_000_000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTFORGE_PRICE_FILE", str(price_file))
    with session_scope(factory) as session:
        record_task(session, session.get(Run, run_id), "gap:AC01", status="RUNNING")

    model = LedgerChatModel(
        ledger_binding=LedgerBinding(
            session_factory=factory, project_id=1, run_id=run_id, task_id="gap:AC01"
        ),
        model="qwen3-coder-flash",
        base_url="http://localhost:1/v1",
        api_key="dummy",
        max_tokens=64,
    )

    calls = {"n": 0}

    def _fake_generate(self, messages, stop=None, run_manager=None, **kwargs):
        calls["n"] += 1
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="ok"))],
            llm_output={"token_usage": {"prompt_tokens": 11, "completion_tokens": 7}},
        )

    monkeypatch.setattr(ChatOpenAI, "_generate", _fake_generate)

    model._generate([AIMessage(content="hi")])
    model._generate([AIMessage(content="again")])

    assert calls["n"] == 2
    with session_scope(factory) as session:
        rows = session.query(ModelCall).filter(ModelCall.run_id == run_id).all()
    assert len(rows) == 2  # 每次内部请求各一行，互不复用 call_id
    assert {row.task_id for row in rows} == {"gap:AC01"}
    assert all(row.model_role == "coding" and row.status == "SETTLED" for row in rows)
    assert all(row.input_tokens == 11 and row.output_tokens == 7 for row in rows)
    assert all(row.cost_micro == 25 for row in rows)  # 11*1 + 7*2 微单位


def test_usage_extraction_falls_back_to_token_usage() -> None:
    result = ChatResult(
        generations=[ChatGeneration(message=AIMessage(content="x"))],
        llm_output={"token_usage": {"prompt_tokens": 3, "completion_tokens": 4}},
    )
    assert patch_engine._usage_from_chat_result(result) == Usage(3, 4)
