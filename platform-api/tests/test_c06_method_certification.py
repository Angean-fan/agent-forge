"""C06：Method Unit 编译级认证（E10）+ E 范围跨厂商路由断言（S4）。

覆盖：

- **编译级认证**：对已发布 Method Unit 做**真实** Maven 构建 + JUnit 运行；工具链/依赖不可用时
  明确 ``NOT_RUN``（阻塞而非 PASS）；**故意损坏**的候选 → ``FAILED``（拒绝案例）；
- **E 范围路由断言**：E 范围的模型缝在**路由禁用**（零调用）、**缺凭据**（`NOT_SENT`，不发请求）、
  **正常**（provider/model_role 与声明一致）三种分支下均无**意外跨厂商调用**。

真实构建依赖本机 Java/Maven 与可用依赖；不可用时相关断言按 ``NOT_RUN`` 处理，不伪造 PASS。
"""

from __future__ import annotations

from pathlib import Path

from agentforge_platform.clients import Completion, DeepSeekClient, Usage
from agentforge_platform.db import session_scope
from agentforge_platform.iteration import classify_change
from agentforge_platform.iteration_service import (
    STATUS_DISABLED,
    STATUS_OK,
    analyze_impact_gateway,
)
from agentforge_platform.method_certification import (
    STATUS_CERTIFIED,
    STATUS_FAILED,
    certify_published_units,
    certify_unit,
    detect_toolchain,
)
from agentforge_platform.method_certification import (
    STATUS_NOT_RUN as CERT_NOT_RUN,
)
from agentforge_platform.models import ModelCall, Project
from agentforge_platform.spec import SoftwareSpecification

REPO = Path(__file__).resolve().parents[2]
METHODS = REPO / "methods"
TASK = REPO / "schemas" / "task-management.spec.json"
PROFILE = "demo-ts-mp"


def _project(factory) -> int:
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="c06", requirement_text="x")
        session.add(project)
        session.flush()
        return project.id


# --- 编译级认证 ---------------------------------------------------------------

def test_toolchain_is_reported() -> None:
    toolchain = detect_toolchain()
    assert isinstance(toolchain.to_dict(), dict)
    assert set(toolchain.to_dict()) == {"java", "maven", "available"}


def test_published_units_certify_or_report_blocked() -> None:
    results = certify_published_units(METHODS, profile=PROFILE)

    assert results, "应至少认证一个已发布 Method Unit"
    assert all(r.status != STATUS_FAILED for r in results)  # 已发布单元不得编译失败
    for result in results:
        evidence = result.evidence()
        assert evidence["command"] == ["mvn", "-q", "-B", "test"]
        if result.status == STATUS_CERTIFIED:
            assert result.tests_run > 0 and result.failures == 0 and result.errors == 0
            assert result.exit_code == 0


def test_broken_candidate_is_rejected(tmp_path: Path) -> None:
    unit = tmp_path / "methods" / "broken-unit" / "1.0.0"
    (unit / "src").mkdir(parents=True)
    (unit / "tests").mkdir(parents=True)
    (unit / "src" / "Broken.java").write_text(
        "package com.agentforge.demo;\npublic class Broken { this is not java }\n", encoding="utf-8")
    (unit / "tests" / "BrokenTest.java").write_text(
        "package com.agentforge.demo;\nclass BrokenTest {}\n", encoding="utf-8")

    result = certify_unit(unit, profile=PROFILE)

    # 真实编译错误 → FAILED；仅当环境受限（依赖不可解析）时才是 NOT_RUN
    assert result.status in (STATUS_FAILED, CERT_NOT_RUN)
    if result.status == STATUS_FAILED:
        assert result.exit_code != 0
        assert not result.certified


# --- E 范围跨厂商路由断言（S4） -----------------------------------------------

def _impact(spec_factory):
    return classify_change(spec_factory("a"), spec_factory("b"))


def _spec(extra: str) -> SoftwareSpecification:
    import json

    payload = json.loads(TASK.read_text(encoding="utf-8"))
    payload["entities"][0]["fields"].append({"name": extra, "type": "string", "required": False})
    return SoftwareSpecification.model_validate(payload)


def test_e_scope_disabled_route_makes_no_calls(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    monkeypatch.delenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", raising=False)
    project_id = _project(factory)

    with session_scope(factory) as session:
        analysis, status = analyze_impact_gateway(
            session, project_id=project_id, impact=_impact(_spec), client=DeepSeekClient())
        rows = session.query(ModelCall).all()

    assert status == STATUS_DISABLED and analysis == {}
    assert rows == []  # 路由禁用 → **零跨厂商调用**


def test_e_scope_missing_key_is_not_run(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    monkeypatch.delenv("AGENTFORGE_CODING_ENABLED", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    _project(factory)

    # 缺凭据下 E 范围的编码路由不构造客户端（安全禁用）
    from agentforge_platform.production_routing import build_production_routing

    routing = build_production_routing(session_factory=factory, work_root="/tmp/c06")
    assert routing.coding_routing is None and routing.planning_client is None
    assert routing.any_enabled() is False
    with session_scope(factory) as session:
        assert session.query(ModelCall).count() == 0  # 未发起任何请求


def test_e_scope_necessary_call_uses_declared_route(platform_app, monkeypatch) -> None:
    _, factory = platform_app
    monkeypatch.setenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "1")
    project_id = _project(factory)

    class _Fake:
        def complete(self, *, model, prompt, thinking_mode=None, json_mode=False,
                     max_output_tokens=None, reasoning_effort=None):
            return Completion(content='{"tasks":[]}', model=model, request_id="r",
                              usage=Usage(3, 1), finish_reason="stop")

    with session_scope(factory) as session:
        _analysis, status = analyze_impact_gateway(
            session, project_id=project_id, impact=_impact(_spec), client=_Fake())
        rows = session.query(ModelCall).all()

    assert status == STATUS_OK and rows
    # 必要调用仍落在**声明**的 (provider, model_role)，无意外跨厂商
    assert {(r.provider, r.model_role) for r in rows} == {("dashscope", "technical_planning")}
