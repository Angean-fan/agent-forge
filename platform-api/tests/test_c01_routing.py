"""C01：生产模型路由注入（S2/S3）。

免费层（**不发起任何供应商调用**）覆盖：

- 默认全关：不构造任何客户端/工厂，未就绪角色报 ``DISABLED``；
- 开关开但缺凭据：报 ``CONFIG_REQUIRED``，仍**不构造**客户端（安全禁用）；
- 开关开且有凭据：显式注入 planning/review 客户端与运行级 coding 路由；
- ``default_context`` 注入语义：``routing=None`` → 全为 ``None``；传 routing → 逐项注入；
- 生产 coding 路由按 run 构造**受限**引擎（构造离线；无 ``execute``/``task`` 工具）；
- “无意外跨厂商调用”：标准 Task 全流程（默认路由 / 缺 Key）零 dashscope 调用。

真实工具调用探针（计费）见 ``scripts/coding_tools_probe.py``；本文件不联网、不付费。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import default_context, run_generation
from agentforge_platform.models import ModelCall, Project, Run, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.patch_engine import FORBIDDEN_TOOL_NAMES, RESTRICTED_TOOLS
from agentforge_platform.production_routing import (
    CODING_ROLE,
    COMPLEX_ROLE,
    DASHSCOPE_KEY_ENV,
    PLANNING_ROLE,
    build_production_routing,
)
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"

_ALL_FLAGS = (
    "AGENTFORGE_TECHNICAL_PLANNING_ENABLED",
    "AGENTFORGE_CODING_ENABLED",
    "AGENTFORGE_COMPLEX_REPAIR_ENABLED",
)
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


class _PassRunner:
    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


def _spec(path: Path) -> SoftwareSpecification:
    return SoftwareSpecification.model_validate_json(path.read_text(encoding="utf-8"))


def _seed(factory, spec: SoftwareSpecification) -> int:
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        row = SpecVersion(
            project_id=project.id, version=1, body_json=canonical_json(spec),
            hash=content_hash(spec), confirmed_at=datetime(2026, 1, 1),
        )
        session.add(row)
        session.flush()
        project.active_spec_id = row.id
        return create_run(
            session, project, row.id, idempotency_key="c01", spec_hash=content_hash(spec)
        ).id


def _routing(monkeypatch, *, flags=(), key=None, factory=None, work_root=Path("/tmp/c01")):
    for flag in _ALL_FLAGS:
        monkeypatch.delenv(flag, raising=False)
    for flag in flags:
        monkeypatch.setenv(flag, "1")
    if key is None:
        monkeypatch.delenv(DASHSCOPE_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(DASHSCOPE_KEY_ENV, key)
    return build_production_routing(session_factory=factory, work_root=work_root)


def _status_by_role(routing) -> dict:
    return {entry.role: entry for entry in routing.status}


# --- 就绪判定 -----------------------------------------------------------------

def test_default_routing_disabled_injects_nothing(monkeypatch) -> None:
    routing = _routing(monkeypatch, flags=())
    assert routing.any_enabled() is False
    assert routing.planning_client is None
    assert routing.review_client is None
    assert routing.coding_routing is None
    for entry in routing.status:
        assert entry.enabled is False
        assert entry.reason == "DISABLED"
        assert entry.flag_on is False


def test_flag_on_without_key_reports_config_required(monkeypatch) -> None:
    routing = _routing(
        monkeypatch,
        flags=("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "AGENTFORGE_CODING_ENABLED"),
        key=None,
    )
    by_role = _status_by_role(routing)
    assert by_role[PLANNING_ROLE].flag_on is True
    assert by_role[PLANNING_ROLE].reason == "CONFIG_REQUIRED"
    assert by_role[CODING_ROLE].reason == "CONFIG_REQUIRED"
    # 缺凭据 → 不构造任何已授权客户端/工厂（安全禁用）
    assert routing.planning_client is None
    assert routing.coding_routing is None
    assert routing.any_enabled() is False


def test_flag_on_with_key_enables_clients(monkeypatch) -> None:
    routing = _routing(
        monkeypatch,
        flags=("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "AGENTFORGE_CODING_ENABLED"),
        key="dummy-key",
    )
    by_role = _status_by_role(routing)
    assert by_role[PLANNING_ROLE].enabled is True and by_role[PLANNING_ROLE].reason is None
    assert by_role[CODING_ROLE].enabled is True
    assert routing.planning_client is not None
    assert routing.review_client is routing.planning_client  # 规划与审查同角色，共享实例
    assert routing.coding_routing is not None


def test_complex_repair_alone_also_builds_coding_routing(monkeypatch) -> None:
    routing = _routing(monkeypatch, flags=("AGENTFORGE_COMPLEX_REPAIR_ENABLED",), key="dummy-key")
    by_role = _status_by_role(routing)
    assert by_role[COMPLEX_ROLE].enabled is True
    assert routing.coding_routing is not None  # 升级角色就绪也应提供受限修复引擎


# --- default_context 注入语义 -------------------------------------------------

def test_default_context_injects_routing(monkeypatch, tmp_path: Path) -> None:
    routing = _routing(
        monkeypatch, flags=("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "AGENTFORGE_CODING_ENABLED"),
        key="dummy-key",
    )
    ctx = default_context(
        session_factory=object(), work_root=tmp_path / "runs", runner=object(), routing=routing
    )
    assert ctx.planning_client is routing.planning_client
    assert ctx.review_client is routing.review_client
    assert ctx.coding_routing is routing.coding_routing

    ctx_none = default_context(
        session_factory=object(), work_root=tmp_path / "runs", runner=object()
    )
    assert ctx_none.planning_client is None
    assert ctx_none.review_client is None
    assert ctx_none.coding_routing is None


# --- 生产 coding 路由构造受限引擎（离线） -------------------------------------

def test_coding_routing_builds_restricted_gap_engine(monkeypatch, tmp_path: Path) -> None:
    routing = _routing(
        monkeypatch, flags=("AGENTFORGE_CODING_ENABLED",), key="dummy-key",
        factory=object(), work_root=tmp_path / "runs",
    )
    assert routing.coding_routing is not None
    gap_factory = routing.coding_routing.gap_engine_factory(run_id=7, project_id=3)
    engine, file_backend = gap_factory(["backend/"], "gap:AC01")

    assert set(engine.tool_names) <= set(RESTRICTED_TOOLS)
    assert not (set(engine.tool_names) & FORBIDDEN_TOOL_NAMES)
    assert file_backend.run_dir.name == "run-7"  # 绑定到该 run 的隔离目录


# --- “无意外跨厂商调用” --------------------------------------------------------

def test_standard_run_default_routing_zero_dashscope_calls(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed(factory, _spec(TASK))
    context = default_context(
        session_factory=factory, work_root=tmp_path / "runs", runner=_PassRunner(), routing=None
    )
    result = run_generation(context, run_id)

    assert result["ok"] is True, result
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"
        assert session.query(ModelCall).filter(ModelCall.provider == "dashscope").count() == 0
        assert session.query(ModelCall).count() == 0


def test_missing_key_standard_run_zero_calls(platform_app, tmp_path: Path, monkeypatch) -> None:
    _, factory = platform_app
    run_id = _seed(factory, _spec(TASK))
    routing = _routing(
        monkeypatch,
        flags=("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "AGENTFORGE_CODING_ENABLED"),
        key=None,  # 开关已开但缺凭据
    )
    assert routing.planning_client is None and routing.coding_routing is None

    context = default_context(
        session_factory=factory, work_root=tmp_path / "runs", runner=_PassRunner(), routing=routing
    )
    result = run_generation(context, run_id)

    assert result["ok"] is True, result
    with session_scope(factory) as session:
        # 缺凭据 → 请求在 preflight 被拦截：既无 SETTLED 也无 RESERVED，只有 NOT_SENT（预留为 0）
        rows = session.query(ModelCall).all()
        assert all(row.status == "NOT_SENT" and row.reserved_micro == 0 for row in rows)
        assert session.query(ModelCall).filter(
            ModelCall.status.in_(("SETTLED", "RESERVED"))
        ).count() == 0
    plan = json.loads((tmp_path / "runs" / f"run-{run_id}" / "task-plan.json").read_text("utf-8"))
    assert plan["execution_mode"] == "deterministic"
    assert plan["retrieval_reasons"]["technical_planning"] == ["CONFIG_REQUIRED"]
