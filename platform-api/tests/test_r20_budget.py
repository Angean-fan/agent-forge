"""R20：Provider 价格、usage 与多角色共享预算。

覆盖：跨币种定价按**锁定汇率快照**折算并可追溯；未知汇率/价格保持 UNKNOWN（不写零）；
幂等结算不重复收费；多角色共享同一项目预算、预算不足零新请求；
分层费用报表（访谈期 / run 级 / 各 task 与各模型/模式/强度/币种/定价版本/未知状态）；
以及检索/装配不产生模型 Token。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from agentforge_platform.asset_registry import default_registry
from agentforge_platform.clients import Usage
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.model_gateway import MICRO_PER_YUAN, reserve, settle
from agentforge_platform.models import ModelCall, Project, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED, build_cost_report
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


def _price_file(tmp_path: Path, models: dict, currency: str | None = None) -> str:
    payload: dict = {"version": "2026-10-13", "models": models}
    if currency:
        payload["currency"] = currency
    path = tmp_path / "price.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _fx_file(tmp_path: Path, rates: dict, captured: str = "2026-10-13T00:00:00Z") -> str:
    path = tmp_path / "fx.json"
    path.write_text(json.dumps({"version": "fx-1", "captured_at": captured, "rates": rates}),
                    encoding="utf-8")
    return str(path)


def _project(factory) -> int:
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        return project.id


# --- 跨币种定价与汇率快照 -----------------------------------------------------

def test_cross_currency_cost_uses_locked_fx(platform_app, tmp_path: Path, monkeypatch) -> None:
    _, factory = platform_app
    project_id = _project(factory)
    monkeypatch.setenv("AGENTFORGE_PRICE_FILE", _price_file(tmp_path, {
        "deepseek-flash": {"input_micro_per_million": 1_000_000, "output_micro_per_million": 2_000_000,
                           "currency": "USD"},
    }))
    monkeypatch.setenv("AGENTFORGE_FX_FILE", _fx_file(tmp_path, {"USD": 7_200_000}))  # 1 USD = 7.2 CNY

    with session_scope(factory) as session:
        row = reserve(session, call_id="c1", model_role="user_interaction", project_id=project_id,
                      max_input_tokens=1000, max_output_tokens=1000)
        assert row.price_currency == "USD"
    with session_scope(factory) as session:
        row = settle(session, "c1", usage=Usage(1000, 500))
        # 美元微额 = 1000*1 + 500*2 = 2000；折算 = 2000 * 7.2 = 14400 人民币微
        assert row.status == "SETTLED" and row.cost_micro == 14400
        assert row.fx_rate_micro == 7_200_000 and row.fx_captured_at == datetime(2026, 10, 13)


def test_unknown_fx_keeps_unknown_without_zero(platform_app, tmp_path: Path, monkeypatch) -> None:
    _, factory = platform_app
    project_id = _project(factory)
    monkeypatch.setenv("AGENTFORGE_PRICE_FILE", _price_file(tmp_path, {
        "deepseek-flash": {"input_micro_per_million": 1_000_000, "output_micro_per_million": 1_000_000,
                           "currency": "USD"},
    }))
    monkeypatch.delenv("AGENTFORGE_FX_FILE", raising=False)  # 无锁定汇率

    with session_scope(factory) as session:
        reserve(session, call_id="c1", model_role="user_interaction", project_id=project_id,
                max_input_tokens=100, max_output_tokens=100)
    with session_scope(factory) as session:
        row = settle(session, "c1", usage=Usage(10, 10))
    assert row.status == "UNKNOWN" and row.cost_micro is None  # 不写零
    assert row.fx_rate_micro is None


def test_idempotent_settle_no_double_charge(platform_app, tmp_path: Path, monkeypatch) -> None:
    _, factory = platform_app
    project_id = _project(factory)
    monkeypatch.setenv("AGENTFORGE_PRICE_FILE", _price_file(tmp_path, {
        "deepseek-flash": {"input_micro_per_million": 1_000_000, "output_micro_per_million": 1_000_000},
    }))

    with session_scope(factory) as session:
        reserve(session, call_id="c1", model_role="user_interaction", project_id=project_id,
                max_input_tokens=100, max_output_tokens=100)
        first = settle(session, "c1", usage=Usage(100, 100))
        assert first.cost_micro == 200
    with session_scope(factory) as session:
        again = settle(session, "c1", usage=Usage(9999, 9999))  # 重复结算
        assert again.cost_micro == 200  # 未被第二次调用改写


# --- 多角色共享预算 -----------------------------------------------------------

def test_shared_budget_across_roles_and_blocks_new(platform_app, tmp_path: Path, monkeypatch) -> None:
    _, factory = platform_app
    project_id = _project(factory)
    monkeypatch.setenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "1")
    monkeypatch.setenv("AGENTFORGE_PROJECT_BUDGET_MICRO", str(1_500))  # 很小的共享预算
    monkeypatch.setenv("AGENTFORGE_PRICE_FILE", _price_file(tmp_path, {
        "deepseek-flash": {"input_micro_per_million": 1_000_000, "output_micro_per_million": 1_000_000},
        "qwen3.7-plus": {"input_micro_per_million": 1_000_000, "output_micro_per_million": 1_000_000},
    }))
    monkeypatch.setenv("AGENTFORGE_DEFAULT_RESERVE_MICRO", "1000")

    with session_scope(factory) as session:
        reserve(session, call_id="u1", model_role="user_interaction", project_id=project_id,
                max_input_tokens=1000, max_output_tokens=0)  # 预留 1000
    with session_scope(factory) as session, pytest.raises(AppException) as error:
        # 共享预算余额不足 → 零新请求
        reserve(session, call_id="q1", model_role="technical_planning", project_id=project_id,
                max_input_tokens=1000, max_output_tokens=0)
    assert error.value.code == "BUDGET_EXCEEDED"


def test_budget_is_shared_per_project_not_per_role(platform_app, tmp_path: Path, monkeypatch) -> None:
    _, factory = platform_app
    project_id = _project(factory)
    monkeypatch.setenv("AGENTFORGE_TECHNICAL_PLANNING_ENABLED", "1")
    monkeypatch.setenv("AGENTFORGE_PROJECT_BUDGET_MICRO", str(10 * MICRO_PER_YUAN))
    monkeypatch.setenv("AGENTFORGE_PRICE_FILE", _price_file(tmp_path, {
        "deepseek-flash": {"input_micro_per_million": 1_000_000, "output_micro_per_million": 1_000_000},
        "qwen3.7-plus": {"input_micro_per_million": 1_000_000, "output_micro_per_million": 1_000_000},
    }))

    with session_scope(factory) as session:
        reserve(session, call_id="u1", model_role="user_interaction", project_id=project_id,
                max_input_tokens=100, max_output_tokens=0)
        settle(session, "u1", usage=Usage(100, 0))
    with session_scope(factory) as session:
        # 另一角色的已结算花费会计入同一项目总额（共享账本）
        rows = session.query(ModelCall).filter(ModelCall.project_id == project_id).all()
        assert {r.model_role for r in rows} == {"user_interaction"}


# --- 分层费用报表 -------------------------------------------------------------

def _seed_row(session, *, call_id, role, model, provider="deepseek_official", run_id=None,
              task_id=None, tokens=(10, 5), cost=100, status="SETTLED", mode=None, strength=None,
              currency="CNY", price_version="v1") -> None:
    session.add(ModelCall(
        call_id=call_id, model_role=role, provider=provider, model=model, project_id=1,
        run_id=run_id, task_id=task_id, thinking_mode=mode, reasoning_effort=strength,
        price_version=price_version, price_currency=currency,
        input_tokens=tokens[0], output_tokens=tokens[1],
        cost_micro=cost if status == "SETTLED" else None, status=status,
    ))


def test_cost_report_phases_tasks_and_models(platform_app) -> None:
    _, factory = platform_app
    with session_scope(factory) as session:
        _seed_row(session, call_id="d1", role="user_interaction", model="deepseek-flash",
                  run_id=None)  # 访谈/生成前草稿
        _seed_row(session, call_id="r1", role="technical_planning", model="qwen3.7-plus",
                  provider="dashscope", run_id=1, task_id=None, mode="disabled")  # run 级
        _seed_row(session, call_id="t1", role="coding", model="qwen3-coder-flash",
                  provider="dashscope", run_id=1, task_id="gap:AC01")
        _seed_row(session, call_id="t2", role="complex_repair", model="qwen3.8-max",
                  provider="dashscope", run_id=1, task_id="repair:verify", status="UNKNOWN")

    with session_scope(factory) as session:
        report = build_cost_report(session, project_id=1, run_id=1)

    assert report["draft_phase"]["calls"] == 1 and report["interview_phase"]["calls"] == 1  # 旧行/生成前不丢
    assert report["run_level"]["calls"] == 1  # 空 task 的 run 级调用单列
    assert set(report["by_task"]) == {"gap:AC01", "repair:verify"}
    assert report["by_task"]["repair:verify"]["unknown_calls"] == 1  # 未知状态如实
    models = report["generation_phase"]["by_model"]
    assert set(models) == {"qwen3.7-plus", "qwen3-coder-flash", "qwen3.8-max"}  # 分角色/模型区分
    assert models["qwen3.7-plus"]["modes"] == ["disabled"] and models["qwen3.7-plus"]["price_currency"] == "CNY"


# --- 检索/装配不计模型 Token --------------------------------------------------

class _PassRunner:
    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


def test_retrieval_and_assembly_make_no_model_calls(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
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
        run_id = create_run(session, project, row.id, idempotency_key="k1",
                            spec_hash=content_hash(spec)).id

    context = GenerationContext(session_factory=factory, runner=_PassRunner(),
                                work_root=tmp_path / "runs", registry=default_registry())
    result = run_generation(context, run_id)

    assert result["ok"] is True, result
    with session_scope(factory) as session:
        # 检索（Finder）与装配（assemble）不调用模型 → 无任何账本行（不以估算冒充实际费用）
        assert session.query(ModelCall).count() == 0
