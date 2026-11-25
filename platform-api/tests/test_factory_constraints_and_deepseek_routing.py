"""工厂流水线全 DeepSeek 路由、护栏步数上限、角色级超时、账本细粒度可观测性与契约提示词回归测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentforge_platform.blueprint_contracts import (
    ArchitectureBlueprint,
    blueprint_prompt_contract,
    department_plan_contract,
    fix_directive_contract,
)
from agentforge_platform.budget_pg import PgBudgetLedger, cost_by_department
from agentforge_platform.clients import Usage
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.factory_department import (
    _build_coder_prompt,
    _build_lead_fix_prompt,
    _build_lead_plan_prompt,
    _collect_staging_context,
)
from agentforge_platform.factory_state import FactoryPhase
from agentforge_platform.factory_tickets import DefectTicket
from agentforge_platform.limits import (
    DEFAULT_FACTORY_MAX_TOOL_OPS,
    factory_agent_recursion_limit,
    factory_max_tool_ops,
    runtime_limits,
)
from agentforge_platform.model_gateway import (
    FACTORY_ROLES,
    ROUTES,
    load_custom_routes,
)
from agentforge_platform.models import Project
from agentforge_platform.module_registry import Module
from agentforge_platform.patch_engine import LedgerBinding
from agentforge_platform.production_routing import build_client_for_role, build_ledger_model
from agentforge_platform.spec import SoftwareSpecification

PLATFORM_API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLATFORM_API_DIR.parent
DEEPSEEK_MODELS_EXAMPLE = PLATFORM_API_DIR / "config" / "models.deepseek.example.json"
LOCAL_MODELS_CONFIG = PLATFORM_API_DIR / "config" / "models.json"
CAMPUS_SPEC_PATH = REPO_ROOT / "schemas" / "campus-activity.spec.json"


def _assert_all_roles_deepseek_config(
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes = load_custom_routes(config_path)
    expected_roles = set(ROUTES) | set(FACTORY_ROLES)
    assert expected_roles <= set(routes)

    monkeypatch.setenv("AGENTFORGE_MODELS_CONFIG", str(config_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-deepseek-key")

    for role in sorted(expected_roles):
        route = routes[role]
        assert route.enabled is True
        assert route.provider == "deepseek_official"
        assert route.model == "deepseek-flash"
        assert route.base_url == "https://api.deepseek.com/v1"
        assert route.api_key_env == "DEEPSEEK_API_KEY"
        assert route.supports_tools is True
        assert route.max_output_tokens is not None and route.max_output_tokens <= 8192
        assert route.timeout_seconds is not None and route.timeout_seconds >= 120
        assert route.price is not None
        assert route.price.currency == "CNY"
        assert route.price.input_micro_per_million > 0
        assert route.price.output_micro_per_million > 0

        client = build_client_for_role(role)
        assert client.timeout_seconds == route.timeout_seconds
        assert client.default_max_output_tokens == route.max_output_tokens

        binding = LedgerBinding(
            session_factory=None,  # type: ignore[arg-type]
            project_id=1,
            run_id=1,
            task_id=f"t-{role}",
            model_role="coding",
        )
        ledger_model = build_ledger_model(role, binding)
        assert ledger_model.model_name == "deepseek-flash"
        assert ledger_model._binding.model_role == role
        assert ledger_model._binding.max_output_tokens == route.max_output_tokens
        assert ledger_model.request_timeout == route.timeout_seconds


def test_models_json_routes_all_roles_to_deepseek_with_pricing_and_safe_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认 DeepSeek 全角色配置模板（及本地 models.json 若存在）全部 8 个角色均指向 DeepSeek 且配置了计价与合理上限。"""
    assert DEEPSEEK_MODELS_EXAMPLE.is_file()
    _assert_all_roles_deepseek_config(DEEPSEEK_MODELS_EXAMPLE, monkeypatch)
    if LOCAL_MODELS_CONFIG.is_file():
        _assert_all_roles_deepseek_config(LOCAL_MODELS_CONFIG, monkeypatch)


def test_custom_route_timeout_seconds_validation(tmp_path: Path) -> None:
    bad_cfg = {
        "version": "bad-timeout-1",
        "roles": {
            "chief_architect": {
                "provider": "deepseek_official",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-flash",
                "api_key_env": "DEEPSEEK_API_KEY",
                "timeout_seconds": 0,
            }
        },
    }
    p = tmp_path / "models.json"
    p.write_text(json.dumps(bad_cfg), encoding="utf-8")
    with pytest.raises(AppException) as exc_info:
        load_custom_routes(p)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


def test_factory_tool_ops_limit_independent_from_r21_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工厂步数上限默认 120（recursion_limit=245），不修改 R21 既有默认 40（85）。"""
    monkeypatch.delenv("AGENTFORGE_FACTORY_MAX_TOOL_OPS", raising=False)
    monkeypatch.delenv("AGENTFORGE_MAX_TOOL_OPS", raising=False)

    assert runtime_limits().max_tool_ops == 40
    assert runtime_limits().agent_recursion_limit == 85
    assert factory_max_tool_ops() == DEFAULT_FACTORY_MAX_TOOL_OPS == 120
    assert factory_agent_recursion_limit() == 245

    monkeypatch.setenv("AGENTFORGE_MAX_TOOL_OPS", "60")
    assert factory_max_tool_ops() == 60
    assert factory_agent_recursion_limit() == 125

    monkeypatch.setenv("AGENTFORGE_FACTORY_MAX_TOOL_OPS", "150")
    assert factory_max_tool_ops() == 150
    assert factory_agent_recursion_limit() == 305


def test_ledger_distinguishes_settled_failed_and_unpriced_calls(
    platform_app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """账本汇总应精确区分已结算、异常失败（failed_calls）与成功但缺单价（unpriced_calls）。"""
    _, factory = platform_app
    cfg = {
        "version": "ledger-obs-v1",
        "currency": "CNY",
        "roles": {
            "chief_architect": {
                "provider": "deepseek_official",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-flash",
                "api_key_env": "DEEPSEEK_API_KEY",
                "enabled": True,
                "price_micro": {
                    "input_micro_per_million": 1_000_000,
                    "output_micro_per_million": 2_000_000,
                },
            },
            "dept_lead": {
                "provider": "deepseek_official",
                "base_url": "https://api.deepseek.com/v1",
                "model": "unpriced-model",
                "api_key_env": "DEEPSEEK_API_KEY",
                "enabled": True,
            },
        },
    }
    cfg_path = tmp_path / "models.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("AGENTFORGE_MODELS_CONFIG", str(cfg_path))
    monkeypatch.delenv("AGENTFORGE_PRICE_FILE", raising=False)

    with session_scope(factory) as db:
        proj = Project(owner_id=1, name="obs-test", requirement_text="test")
        db.add(proj)
        db.flush()
        project_id = proj.id

    ledger = PgBudgetLedger(factory)
    # 1. 有定价且成功 → SETTLED
    ledger.reserve_atomic(
        call_id="c-settled",
        project_id=project_id,
        run_id=901,
        model_role="chief_architect",
        phase="backend",
        dept_id="chief",
        round_num=1,
        max_input_tokens=1000,
        max_output_tokens=500,
    )
    ledger.settle_idempotent("c-settled", usage=Usage(input_tokens=500, output_tokens=200))

    # 2. 异常失败（usage=None, error_class=TimeoutError）→ UNKNOWN + failed_calls=1
    ledger.reserve_atomic(
        call_id="c-failed",
        project_id=project_id,
        run_id=901,
        model_role="chief_architect",
        phase="backend",
        dept_id="chief",
        round_num=2,
        max_input_tokens=1000,
        max_output_tokens=500,
    )
    ledger.settle_idempotent("c-failed", usage=None, error_class="TimeoutError")

    # 3. 缺单价但成功返回 usage → UNKNOWN + unpriced_calls=1
    ledger.reserve_atomic(
        call_id="c-unpriced",
        project_id=project_id,
        run_id=901,
        model_role="dept_lead",
        phase="backend",
        dept_id="clubs",
        round_num=1,
        max_input_tokens=1000,
        max_output_tokens=500,
    )
    ledger.settle_idempotent("c-unpriced", usage=Usage(input_tokens=300, output_tokens=100))

    with session_scope(factory) as db:
        report = cost_by_department(db, 901)

    totals = report["totals"]
    assert totals["calls"] == 3
    assert totals["settled_calls"] == 1
    assert totals["unknown_calls"] == 2
    assert totals["failed_calls"] == 1
    assert totals["unpriced_calls"] == 1
    assert totals["cost_micro"] == 900  # 500*1 + 200*2 = 900 micro CNY


def test_prompts_enforce_contracts_task_markers_and_staging_context(tmp_path: Path) -> None:
    """契约提示词包含关键硬规则；规划提示词不含 FixDirective 污染；上下文直注底座与 Facade。"""
    bp_contract = blueprint_prompt_contract()
    assert "test_paths" in bp_contract
    assert "JAVA_METHOD" in bp_contract and "HTTP" in bp_contract
    assert "migration_range" in bp_contract
    assert "depends_on_modules" in bp_contract

    plan_contract = department_plan_contract()
    assert "FixDirective" not in plan_contract
    assert "summary" in plan_contract
    assert "root_cause" in fix_directive_contract()

    spec = SoftwareSpecification.model_validate(
        json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8"))
    )
    mod = Module(
        project_id="1",
        module_id="clubs",
        name="社团管理",
        based_on_spec_hash="a" * 64,
        profile_ref="java-vue@1",
        owned_paths=["backend/src/main/java/com/agentforge/template/modules/clubs/internal/"],
        test_paths=["backend/src/test/java/com/agentforge/template/modules/clubs/"],
        migration_range=(100, 199),
    )
    bp = ArchitectureBlueprint(
        project_id="1",
        based_on_spec_hash="a" * 64,
        profile_ref="java-vue@1",
        modules=[mod],
    )

    plan_prompt = _build_lead_plan_prompt(
        module=mod, blueprint=bp, spec=spec, phase=FactoryPhase.BACKEND, round_num=1
    )
    assert "[TASK_TYPE: DEPARTMENT_PLAN]" in plan_prompt
    assert "FixDirective" not in plan_prompt

    ticket = DefectTicket(
        ticket_id="t-1",
        run_id=1,
        phase="backend",
        dept_id="clubs",
        round=1,
        error_class="UNIT_TEST_FAILURE",
        summary="编译失败",
    )
    fix_prompt = _build_lead_fix_prompt(
        module=mod, phase=FactoryPhase.BACKEND, round_num=2, ticket=ticket
    )
    assert "[TASK_TYPE: FIX_DIRECTIVE]" in fix_prompt
    assert "FixDirective" in fix_prompt

    # 后端上下文直注 Facade 内容
    facade_file = (
        tmp_path
        / "backend/src/main/java/com/agentforge/template/modules/clubs/api/ClubFacade.java"
    )
    facade_file.parent.mkdir(parents=True, exist_ok=True)
    facade_file.write_text("public interface ClubFacade { boolean exists(Long id); }\n", encoding="utf-8")

    be_ctx = _collect_staging_context(
        tmp_path, module=mod, blueprint=bp, phase=FactoryPhase.BACKEND
    )
    assert "ClubFacade.java" in be_ctx
    assert "boolean exists(Long id)" in be_ctx
    assert "api-fragment.json" in be_ctx

    coder_prompt = _build_coder_prompt(
        module=mod, plan=None, directive=None, round_num=1, staging_context=be_ctx
    )
    assert "并行" in coder_prompt or "同时发起多个 write_file" in coder_prompt
    assert "ClubFacade" in coder_prompt

    # 前端上下文直注 generated/api-endpoints.ts 与路由/安全红线
    fe_ep = tmp_path / "frontend/src/generated/api-endpoints.ts"
    fe_ep.parent.mkdir(parents=True, exist_ok=True)
    fe_ep.write_text("export const API_ENDPOINTS = { listClubs: '/api/clubs' } as const;\n", encoding="utf-8")
    fe_mod = Module(
        project_id="1",
        module_id="clubs-ui",
        name="社团前端",
        based_on_spec_hash="a" * 64,
        profile_ref="java-vue@1",
        owned_paths=["frontend/src/modules/clubs-ui/"],
        test_paths=["frontend/test/clubs-ui/"],
    )
    fe_ctx = _collect_staging_context(
        tmp_path, module=fe_mod, blueprint=bp, phase=FactoryPhase.FRONTEND
    )
    assert "API_ENDPOINTS" in fe_ctx
    assert "requiresAuth: true" in fe_ctx
    assert "v-html" in fe_ctx
