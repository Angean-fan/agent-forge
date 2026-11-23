"""FP05：后端多部门异步并行调度池（asyncio + 双信号量 + 小循环自愈 + 契约变更升级）测试。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agentforge_platform.agent_files import STAGING_DIRNAME
from agentforge_platform.asset_cache import AssetMaterializer, remove_readonly_tree
from agentforge_platform.asset_registry import REPO_ROOT, default_registry
from agentforge_platform.asset_store_pg import PgAssetStore
from agentforge_platform.blueprint_contracts import (
    ArchitectureBlueprint,
    DtoContract,
    DtoField,
    validate_blueprint,
)
from agentforge_platform.budget_pg import PgBudgetLedger
from agentforge_platform.clients import Completion, Usage
from agentforge_platform.db import make_engine, make_session_factory, session_scope
from agentforge_platform.factory_base_builder import build_backend_base, verify_base_integrity
from agentforge_platform.factory_department import (
    DEPT_CODER_ROLE,
    DEPT_LEAD_ROLE,
    DEPT_TESTER_ROLE,
    DepartmentRunner,
)
from agentforge_platform.factory_scheduler import (
    AsyncDepartmentPool,
    ContractChangeDecision,
    ContractChangeEscalation,
    ContractChangeRequest,
)
from agentforge_platform.factory_state import (
    BlueprintStatus,
    DeptStatus,
    FactoryPhase,
    FactoryStateStore,
)
from agentforge_platform.factory_tickets import DefectTicket, DepartmentDeliverable, TestEvidence
from agentforge_platform.interface_registry import (
    KIND_JAVA_METHOD,
    STATUS_APPROVED_FOR_BUILD,
    PublicCapability,
)
from agentforge_platform.model_gateway import Price
from agentforge_platform.models import BudgetAdjustment, Project, Run
from agentforge_platform.module_registry import Module
from agentforge_platform.module_verifier import ModuleVerificationResult, ModuleVerifier
from agentforge_platform.resolver import resolve_assets
from agentforge_platform.runs import create_run, request_cancel
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import content_hash

PLATFORM_API_DIR = Path(__file__).resolve().parents[1]
CAMPUS_SPEC_PATH = REPO_ROOT / "schemas" / "campus-activity.spec.json"
BASE_PKG = "com.agentforge.template"
PKG_PATH = "com/agentforge/template"


def _alembic_config(url: str) -> Config:
    config = Config(str(PLATFORM_API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_API_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def _configure_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = {
        "version": "fp05-test-v1",
        "currency": "CNY",
        "roles": {
            role: {
                "provider": "openai_compatible",
                "model": f"test-{role}",
                "base_url": "https://api.example.test/v1",
                "api_key_env": "TEST_FP05_KEY",
                "enabled": True,
                "max_output_tokens": 4096,
                "supports_tools": True,
                "price_micro": {
                    "input_micro_per_million": 1_000_000,
                    "output_micro_per_million": 1_000_000,
                },
            }
            for role in (DEPT_LEAD_ROLE, DEPT_CODER_ROLE, DEPT_TESTER_ROLE, "complex_repair")
        },
    }
    p = tmp_path / "models.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("AGENTFORGE_MODELS_CONFIG", str(p))


def _make_5_dept_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_base: bool = False):
    _configure_models(tmp_path, monkeypatch)
    url = f"sqlite+pysqlite:///{tmp_path / 'fp05.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)

    with session_scope(factory) as db:
        if with_base:
            PgAssetStore(db).sync_from_filesystem()
        project = Project(owner_id=1, name="ecommerce-5dept", requirement_text="5 部门电商")
        db.add(project)
        db.flush()
        run = create_run(db, project, spec_id=1, spec_hash="a" * 64)
        project_id, run_id = project.id, run.id

    base = json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8"))
    base["goal"] = "电商沙箱 5 部门并行：auth/product/cart/order/payment"
    entities = ["UserAccount", "Product", "CartItem", "PurchaseOrder", "PaymentRecord"]
    base["entities"] = [
        {
            "name": name,
            "source": "user_confirmed",
            "fields": [{"name": "id", "type": "long", "required": True}],
        }
        for name in entities
    ]
    base["pages"] = [{"name": f"{name}Page", "entity": name} for name in entities]
    base["integrations"] = []
    base["capability_requests"] = []
    spec = SoftwareSpecification.model_validate(base)
    spec_hash = content_hash(spec)
    profile_ref = f"{spec.technical_profile.profile_id}@{spec.technical_profile.profile_version}"

    def _m(
        dept_id: str,
        name: str,
        sql_file: str,
        m_range: tuple[int, int],
        deps: list[str] | None = None,
        refs: list[str] | None = None,
    ) -> Module:
        return Module(
            project_id=str(project_id),
            module_id=dept_id,
            name=name,
            based_on_spec_hash=spec_hash,
            profile_ref=profile_ref,
            owned_paths=[
                f"backend/src/main/java/{PKG_PATH}/modules/{dept_id}/internal/",
                f"backend/src/main/resources/db/migration/{sql_file}",
            ],
            test_paths=[f"backend/src/test/java/{PKG_PATH}/modules/{dept_id}/"],
            owned_package=f"{BASE_PKG}.modules.{dept_id}",
            migration_range=m_range,
            depends_on_modules=deps or [],
            readable_contract_refs=refs or [],
        )

    modules = [
        _m("auth", "认证部", "V100__auth.sql", (100, 199)),
        _m("product", "商品部", "V200__product.sql", (200, 299)),
        _m(
            "cart",
            "购物车部",
            "V300__cart.sql",
            (300, 399),
            deps=["auth", "product"],
            refs=["auth.get_user", "product.get_snapshot"],
        ),
        _m(
            "order",
            "订单部",
            "V400__order.sql",
            (400, 499),
            deps=["auth", "product", "cart"],
            refs=["auth.get_user", "product.get_snapshot", "cart.list_items"],
        ),
        _m(
            "payment",
            "模拟支付部",
            "V500__payment.sql",
            (500, 599),
            deps=["order"],
            refs=["order.get_payable"],
        ),
    ]

    shared_dtos = [
        DtoContract(
            name="UserDto",
            owner_module_id="auth",
            fields=[DtoField(name="userId", java_type="Long")],
        ),
        DtoContract(
            name="ProductDto",
            owner_module_id="product",
            fields=[DtoField(name="productId", java_type="Long")],
        ),
        DtoContract(
            name="CartDto",
            owner_module_id="cart",
            fields=[DtoField(name="itemId", java_type="Long")],
        ),
        DtoContract(
            name="OrderDto",
            owner_module_id="order",
            fields=[DtoField(name="orderId", java_type="Long")],
        ),
        DtoContract(
            name="PaymentDto",
            owner_module_id="payment",
            fields=[DtoField(name="paymentId", java_type="Long")],
        ),
    ]

    contracts = [
        PublicCapability(
            capability_id="auth.get_user",
            owner_module_id="auth",
            interface_kind=KIND_JAVA_METHOD,
            symbol="AuthFacade.getUser",
            signature="UserDto getUser(Long userId)",
            status=STATUS_APPROVED_FOR_BUILD,
            version=1,
        ),
        PublicCapability(
            capability_id="product.get_snapshot",
            owner_module_id="product",
            interface_kind=KIND_JAVA_METHOD,
            symbol="ProductFacade.getSnapshot",
            signature="ProductDto getSnapshot(Long productId)",
            status=STATUS_APPROVED_FOR_BUILD,
            version=1,
        ),
        PublicCapability(
            capability_id="cart.list_items",
            owner_module_id="cart",
            interface_kind=KIND_JAVA_METHOD,
            symbol="CartFacade.listItems",
            signature="List<CartDto> listItems(Long userId)",
            status=STATUS_APPROVED_FOR_BUILD,
            version=1,
        ),
        PublicCapability(
            capability_id="order.get_payable",
            owner_module_id="order",
            interface_kind=KIND_JAVA_METHOD,
            symbol="OrderFacade.getPayable",
            signature="OrderDto getPayable(Long orderId)",
            status=STATUS_APPROVED_FOR_BUILD,
            version=1,
        ),
        PublicCapability(
            capability_id="payment.pay_sandbox",
            owner_module_id="payment",
            interface_kind=KIND_JAVA_METHOD,
            symbol="PaymentFacade.paySandbox",
            signature="PaymentDto paySandbox(Long orderId)",
            status=STATUS_APPROVED_FOR_BUILD,
            version=1,
        ),
    ]

    bp = ArchitectureBlueprint(
        project_id=str(project_id),
        based_on_spec_hash=spec_hash,
        profile_ref=profile_ref,
        base_package=BASE_PKG,
        modules=modules,
        shared_dtos=shared_dtos,
        frozen_contracts=contracts,
        covered_entities={
            "auth": ["UserAccount"],
            "product": ["Product"],
            "cart": ["CartItem"],
            "order": ["PurchaseOrder"],
            "payment": ["PaymentRecord"],
        },
        covered_acceptance_ids={"auth": [ac.id for ac in spec.acceptance_cases]},
    )
    assert validate_blueprint(bp, spec) == []

    run_dir = tmp_path / "run-fp05"
    base_dir: Path | None = None
    if with_base:
        lock = resolve_assets(default_registry(), "demo-ts-mp")
        sess = factory()
        try:
            mat = AssetMaterializer(sess, cache_root=tmp_path / "cache")
            base_res = build_backend_base(run_dir, bp, lock, mat)
            base_dir = Path(base_res.base_dir)
        finally:
            sess.close()

    return engine, factory, project_id, run_id, spec, bp, run_dir, base_dir


class _SlowPassVerifier:
    verifier_kind = "local_process"

    def __init__(self, delay_s: float = 0.06, fail_first_depts: set[str] | None = None) -> None:
        self._delay_s = delay_s
        self._fail_first = set(fail_first_depts or set())
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def verify_module(
        self,
        *,
        artifact_dir: str | Path,
        module_id: str,
        stack: str = "java",
        test_selector: str = "*Test",
        timeout_seconds: int = 120,
    ) -> ModuleVerificationResult:
        time.sleep(self._delay_s)
        with self._lock:
            should_fail = module_id in self._fail_first and module_id not in self._seen
            self._seen.add(module_id)
        if should_fail:
            return ModuleVerificationResult(
                artifact_dir=str(artifact_dir),
                module_id=module_id,
                stack="java",
                test_selector=test_selector,
                status="FAIL",
                verifier_kind="local_process",
                error_code="UNIT_TEST_FAILURE",
                exit_code=1,
                duration_ms=int(self._delay_s * 1000),
                log_excerpt=f"{module_id} round 1 failed",
            )
        return ModuleVerificationResult(
            artifact_dir=str(artifact_dir),
            module_id=module_id,
            stack="java",
            test_selector=test_selector,
            status="PASS",
            verifier_kind="local_process",
            exit_code=0,
            duration_ms=int(self._delay_s * 1000),
            log_excerpt="Tests run: 2, Failures: 0",
        )


class _ScriptedLeadClient:
    def __init__(self, dept_id: str) -> None:
        self._dept_id = dept_id
        self._calls = 0

    def complete(self, *, model: str, prompt: str, **kwargs: Any) -> Completion:
        self._calls += 1
        if self._calls == 1:
            body = json.dumps(
                {"dept_id": self._dept_id, "phase": "backend", "round": 1, "summary": "plan"}
            )
        else:
            body = json.dumps(
                {
                    "dept_id": self._dept_id,
                    "phase": "backend",
                    "round": self._calls,
                    "root_cause": "fix round 1 defect",
                    "target_files": [],
                    "instructions": ["fix it"],
                }
            )
        return Completion(
            content=body,
            model=model,
            request_id=f"req-{self._dept_id}-{self._calls}",
            usage=Usage(input_tokens=200, output_tokens=100),
            finish_reason="stop",
        )


class _NoopToolModel(BaseChatModel):
    model_name: str = "qwen3-coder-flash"

    @property
    def _llm_type(self) -> str:
        return "noop_fp05"

    def _get_ls_params(self, **kwargs: Any) -> dict[str, Any]:
        return {"ls_provider": "openai", "ls_model_name": self.model_name}

    def bind_tools(self, tools: Any, **kwargs: Any) -> _NoopToolModel:
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])


def test_5_departments_true_async_concurrency_and_dual_semaphore_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, _pid, run_id, spec, bp, run_dir, _ = _make_5_dept_fixture(tmp_path, monkeypatch)
    try:
        store = FactoryStateStore(factory)

        async def _executor(
            rid: int,
            module: Module,
            _cur_bp: ArchitectureBlueprint,
            verifier: ModuleVerifier,
            _ticket: DefectTicket | None,
        ) -> DepartmentDeliverable:
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.PLANNING, current_round=1)
            await asyncio.sleep(0.06)
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.CODING)
            await asyncio.sleep(0.04)
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.TESTING)
            ver = await asyncio.to_thread(
                verifier.verify_module,
                artifact_dir=run_dir / "staging" / "backend" / module.module_id,
                module_id=module.module_id,
                stack="java",
                test_selector=f"{module.module_id}Test",
            )
            ev = TestEvidence(
                dept_id=module.module_id,
                phase="backend",
                round=1,
                verifier_kind="local_process",
                stack="java",
                test_selector=ver.test_selector,
                status="PASS",
                executed=True,
                exit_code=0,
            )
            store.transition_department(
                rid,
                FactoryPhase.BACKEND,
                module.module_id,
                DeptStatus.PASSED,
                evidence_json=ev.model_dump_json(),
            )
            return DepartmentDeliverable(
                run_id=rid,
                phase="backend",
                dept_id=module.module_id,
                status="PASSED",
                rounds_used=1,
                staging_dir=str(run_dir / "staging" / "backend" / module.module_id),
                evidence=ev,
            )

        pool = AsyncDepartmentPool(
            session_factory=factory,
            verifier=_SlowPassVerifier(delay_s=0.07),
            dept_executor=_executor,
            max_concurrent_depts_limit=3,
            max_concurrent_verifiers_limit=2,
        )
        t0 = time.monotonic()
        res = asyncio.run(pool.run_phase(run_id=run_id, blueprint=bp, spec=spec, run_dir=run_dir))
        elapsed = time.monotonic() - t0

        assert res.ok is True
        assert res.status == "PASSED"
        assert len(res.deliverables) == 5
        # 证明真正异步并发执行，且严格受双信号量约束（LLM <= 3，Verifier <= 2）
        assert 2 <= res.max_observed_llm_concurrency <= 3
        assert 1 <= res.max_observed_verifier_concurrency <= 2
        # 时间重叠校验：前 3 部门存在重叠区间，总耗时远小于 5 部门串行（5 * 0.17s = 0.85s）
        starts = sorted(t.started_at_monotonic for t in res.telemetry.values())
        finishes = sorted(t.finished_at_monotonic for t in res.telemetry.values())
        assert starts[2] < finishes[0]
        assert elapsed < 0.75
    finally:
        engine.dispose()


def test_scenario_1_round1_self_heal_others_unblocked_and_exception_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, _pid, run_id, spec, bp, run_dir, _ = _make_5_dept_fixture(tmp_path, monkeypatch)
    try:
        # 1A: 真实 DepartmentRunner 驱动 5 部门：cart 第 1 轮失败自愈第 2 轮通过，其余 4 部门首轮即通过不受阻
        verifier = _SlowPassVerifier(delay_s=0.02, fail_first_depts={"cart"})

        def _runner_factory(
            module: Module,
            guarded_verifier: ModuleVerifier,
            round3_role: str | None,
        ) -> DepartmentRunner:
            return DepartmentRunner(
                session_factory=factory,
                verifier=guarded_verifier,
                lead_client=_ScriptedLeadClient(module.module_id),
                coder_model_factory=lambda _b, _r: _NoopToolModel(),
                tester_model_factory=lambda _b, _r: _NoopToolModel(),
                round_3_coder_role=round3_role,
            )

        pool = AsyncDepartmentPool(
            session_factory=factory,
            verifier=verifier,
            runner_factory=_runner_factory,
            max_concurrent_depts_limit=3,
            max_concurrent_verifiers_limit=2,
            max_rounds=3,
        )
        res = asyncio.run(pool.run_phase(run_id=run_id, blueprint=bp, spec=spec, run_dir=run_dir))

        assert res.ok is True
        assert res.deliverables["cart"].rounds_used == 2
        for other in ("auth", "product", "order", "payment"):
            assert res.deliverables[other].rounds_used == 1
            assert res.deliverables[other].status == "PASSED"

        # 1B: 异常隔离与脏暂存清理：payment 抛出未预期 RuntimeError，不能污染其他 4 部门，且清理 payment/.staging
        with session_scope(factory) as db:
            proj = db.query(Project).first()
            assert proj is not None
            run2 = create_run(db, proj, spec_id=1, spec_hash="b" * 64)
            run_id_2 = run2.id

        store = FactoryStateStore(factory)
        run_dir_2 = tmp_path / "run-isolation"

        async def _faulty_executor(
            rid: int,
            module: Module,
            _cur_bp: ArchitectureBlueprint,
            _ver: ModuleVerifier,
            _ticket: DefectTicket | None,
        ) -> DepartmentDeliverable:
            dirty_dir = run_dir_2 / "staging" / "backend" / module.module_id / STAGING_DIRNAME
            dirty_dir.mkdir(parents=True, exist_ok=True)
            (dirty_dir / "partial.java").write_text("// dirty", encoding="utf-8")
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.PLANNING, current_round=1)
            if module.module_id == "payment":
                raise RuntimeError("simulated unexpected crash in payment")
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.CODING)
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.TESTING)
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.PASSED)
            return DepartmentDeliverable(
                run_id=rid,
                phase="backend",
                dept_id=module.module_id,
                status="PASSED",
                rounds_used=1,
                staging_dir=str(run_dir_2 / "staging" / "backend" / module.module_id),
            )

        pool_iso = AsyncDepartmentPool(
            session_factory=factory,
            verifier=_SlowPassVerifier(delay_s=0.01),
            dept_executor=_faulty_executor,
        )
        res_iso = asyncio.run(
            pool_iso.run_phase(run_id=run_id_2, blueprint=bp, spec=spec, run_dir=run_dir_2)
        )
        assert res_iso.ok is False
        assert res_iso.failed_depts == ["payment"]
        for ok_dept in ("auth", "product", "cart", "order"):
            assert res_iso.deliverables[ok_dept].status == "PASSED"
        # payment 脏暂存目录已被 finally 清理
        assert not (run_dir_2 / "staging" / "backend" / "payment" / STAGING_DIRNAME).exists()
    finally:
        engine.dispose()


def test_scenario_2_concurrent_budget_zero_double_charge_and_resume_after_topup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 初始预算 3_000_000 微元（仅够 3 个部门各 1_000_000 预留），5 部门同时抢占
    monkeypatch.setenv("AGENTFORGE_PROJECT_BUDGET_MICRO", "3000000")
    engine, factory, project_id, run_id, spec, bp, run_dir, _ = _make_5_dept_fixture(
        tmp_path, monkeypatch
    )
    try:
        ledger = PgBudgetLedger(factory)
        store = FactoryStateStore(factory)
        price = Price(
            version="fp05-p1",
            input_micro_per_million=1_000_000,
            output_micro_per_million=1_000_000,
        )

        async def _billed_executor(
            rid: int,
            module: Module,
            _cur_bp: ArchitectureBlueprint,
            _ver: ModuleVerifier,
            _ticket: DefectTicket | None,
        ) -> DepartmentDeliverable:
            call_id = f"fp05-budget:{rid}:{module.module_id}"
            await asyncio.to_thread(
                ledger.reserve_atomic,
                call_id=call_id,
                project_id=project_id,
                run_id=rid,
                model_role=DEPT_CODER_ROLE,
                provider="openai_compatible",
                model="test-dept_coder",
                reserve_micro=1_000_000,
                dept_id=module.module_id,
                phase="backend",
                round_num=1,
                price=price,
            )
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.PLANNING, current_round=1)
            await asyncio.sleep(0.03)
            # 结算 800_000 微元（并发幂等不双扣）
            usage = Usage(input_tokens=400_000, output_tokens=400_000)
            await asyncio.to_thread(ledger.settle_idempotent, call_id, usage=usage)
            await asyncio.to_thread(ledger.settle_idempotent, call_id, usage=usage)
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.CODING)
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.TESTING)
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.PASSED)
            return DepartmentDeliverable(
                run_id=rid,
                phase="backend",
                dept_id=module.module_id,
                status="PASSED",
                rounds_used=1,
                staging_dir=str(run_dir / "staging" / "backend" / module.module_id),
            )

        pool = AsyncDepartmentPool(
            session_factory=factory,
            verifier=_SlowPassVerifier(delay_s=0.01),
            dept_executor=_billed_executor,
            max_concurrent_depts_limit=5,
            max_concurrent_verifiers_limit=2,
        )
        res_1 = asyncio.run(pool.run_phase(run_id=run_id, blueprint=bp, spec=spec, run_dir=run_dir))
        assert res_1.ok is False
        assert res_1.status == "PAUSED_BUDGET"
        assert res_1.error_code == "BUDGET_EXCEEDED"

        # 精确放行 3 个部门、零双扣、总额严守预算上限
        passed_after_pause = [r.dept_id for r in store.passed_departments(run_id, FactoryPhase.BACKEND)]
        assert len(passed_after_pause) == 3
        snap = ledger.balance(project_id)
        assert snap.settled_micro == 2_400_000
        assert snap.reserved_micro == 0

        # 加预算 +5_000_000 微元后恢复续跑 → 跳过已 PASSED 的 3 个检查点，仅补跑其余 2 个部门
        with session_scope(factory) as db:
            db.add(
                BudgetAdjustment(
                    project_id=project_id,
                    run_id=run_id,
                    delta_micro=5_000_000,
                    reason="topup_for_remaining_depts",
                    actor_id=1,
                )
            )

        res_2 = asyncio.run(pool.run_phase(run_id=run_id, blueprint=bp, spec=spec, run_dir=run_dir))
        assert res_2.ok is True
        assert res_2.status == "PASSED"
        assert sorted(res_2.reused_depts) == sorted(passed_after_pause)
        assert len(res_2.executed_depts) == 2
        assert ledger.balance(project_id).settled_micro == 4_000_000
    finally:
        engine.dispose()


def test_scenario_3_cancel_stops_new_rounds_and_preserves_passed_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, _pid, run_id, spec, bp, run_dir, _ = _make_5_dept_fixture(tmp_path, monkeypatch)
    try:
        store = FactoryStateStore(factory)
        both_passed = asyncio.Event()
        completed_count = 0
        count_lock = threading.Lock()

        async def _cancellable_executor(
            rid: int,
            module: Module,
            _cur_bp: ArchitectureBlueprint,
            _ver: ModuleVerifier,
            _ticket: DefectTicket | None,
        ) -> DepartmentDeliverable:
            nonlocal completed_count
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.PLANNING, current_round=1)
            await asyncio.sleep(0.03)
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.CODING)
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.TESTING)
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.PASSED)
            with count_lock:
                completed_count += 1
                is_second = completed_count == 2
            if is_second:
                with session_scope(factory) as db:
                    run = db.get(Run, rid)
                    assert run is not None
                    request_cancel(db, run)
                both_passed.set()
            else:
                # 保持并发槽位直到第 2 个部门置位取消，确定性验证排队中的其余 3 部门不再派发
                await both_passed.wait()
            return DepartmentDeliverable(
                run_id=rid,
                phase="backend",
                dept_id=module.module_id,
                status="PASSED",
                rounds_used=1,
                staging_dir=str(run_dir / "staging" / "backend" / module.module_id),
            )

        # 并发上限=2：前 2 个部门完成后置位 cancel_requested，排队中的剩余 3 个部门绝不启动新一轮
        pool = AsyncDepartmentPool(
            session_factory=factory,
            verifier=_SlowPassVerifier(delay_s=0.01),
            dept_executor=_cancellable_executor,
            max_concurrent_depts_limit=2,
            max_concurrent_verifiers_limit=2,
        )
        res = asyncio.run(pool.run_phase(run_id=run_id, blueprint=bp, spec=spec, run_dir=run_dir))

        assert res.ok is False
        assert res.status == "CANCELLED"
        assert res.error_code == "RUN_CANCELLED"
        assert len(res.executed_depts) == 2
        assert len(res.cancelled_depts) == 3

        passed_rows = store.passed_departments(run_id, FactoryPhase.BACKEND)
        assert len(passed_rows) == 2
    finally:
        engine.dispose()


def test_scenario_4_contract_change_escalation_reruns_only_2_affected_depts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, _pid, run_id, spec, bp, run_dir, base_dir = _make_5_dept_fixture(
        tmp_path, monkeypatch, with_base=True
    )
    try:
        assert base_dir is not None
        store = FactoryStateStore(factory)
        # 预先写入 v1 蓝图，验证升级后 v1 变 SUPERSEDED、v2 变 APPROVED_FOR_BUILD
        store.save_blueprint(
            run_id,
            FactoryPhase.BACKEND,
            version=1,
            blueprint_json=bp.model_dump_json(),
            blueprint_hash=bp.blueprint_hash(),
            status=BlueprintStatus.APPROVED_FOR_BUILD,
        )

        exec_counts: dict[str, int] = {}
        count_lock = threading.Lock()
        escalated_once = False

        async def _contract_gap_executor(
            rid: int,
            module: Module,
            cur_bp: ArchitectureBlueprint,
            _ver: ModuleVerifier,
            _ticket: DefectTicket | None,
        ) -> DepartmentDeliverable:
            nonlocal escalated_once
            with count_lock:
                exec_counts[module.module_id] = exec_counts.get(module.module_id, 0) + 1
                run_no = exec_counts[module.module_id]

            store.transition_department(
                rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.PLANNING, current_round=run_no
            )
            await asyncio.sleep(0.02)

            # order 首轮发现需要在 ProductFacade 上新增 checkPromoPrice 方法 → 发契约变更升级请求
            if module.module_id == "order" and not escalated_once and cur_bp.blueprint_version == 1:
                escalated_once = True
                raise ContractChangeEscalation(
                    ContractChangeRequest(
                        request_id="ccr-order-promo-1",
                        requester_dept="order",
                        target_dept="product",
                        capability_id="product.check_promo_price",
                        required_contract="Boolean checkPromoPrice(Long productId)",
                        reason="下单时需核验商品促销资格",
                    )
                )

            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.CODING)
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.TESTING)
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.PASSED)
            return DepartmentDeliverable(
                run_id=rid,
                phase="backend",
                dept_id=module.module_id,
                status="PASSED",
                rounds_used=run_no,
                staging_dir=str(run_dir / "staging" / "backend" / module.module_id),
            )

        def _chief_approver(
            req: ContractChangeRequest,
            cur_bp: ArchitectureBlueprint,
            _rid: int,
        ) -> ContractChangeDecision:
            new_cap = PublicCapability(
                capability_id=req.capability_id,
                owner_module_id=req.target_dept,
                interface_kind=KIND_JAVA_METHOD,
                symbol="ProductFacade.checkPromoPrice",
                signature=req.required_contract,
                status=STATUS_APPROVED_FOR_BUILD,
                version=1,
            )
            updated_mods = []
            for m in cur_bp.modules:
                if m.module_id == req.requester_dept:
                    updated_mods.append(
                        m.model_copy(
                            update={
                                "readable_contract_refs": [*m.readable_contract_refs, req.capability_id]
                            }
                        )
                    )
                else:
                    updated_mods.append(m)
            updated_bp = cur_bp.model_copy(
                update={
                    "blueprint_version": cur_bp.blueprint_version + 1,
                    "modules": updated_mods,
                    "frozen_contracts": [*cur_bp.frozen_contracts, new_cap],
                }
            )
            return ContractChangeDecision(
                approved=True,
                updated_blueprint=updated_bp,
                affected_depts=[req.target_dept, req.requester_dept],
                reason="approved by ChiefArchitect",
            )

        pool = AsyncDepartmentPool(
            session_factory=factory,
            verifier=_SlowPassVerifier(delay_s=0.01),
            dept_executor=_contract_gap_executor,
            contract_change_approver=_chief_approver,
            max_concurrent_depts_limit=3,
            max_concurrent_verifiers_limit=2,
            max_contract_changes=2,
        )
        res = asyncio.run(
            pool.run_phase(
                run_id=run_id,
                blueprint=bp,
                spec=spec,
                run_dir=run_dir,
                base_dir=base_dir,
            )
        )

        assert res.ok is True
        assert res.status == "PASSED"
        assert res.blueprint_version == 2
        assert res.contract_changes_applied == 1

        # 关键断言：仅提供方 product 与消费方 order 共 2 个部门重跑（各执行 2 次）；
        # 其余 3 个部门（auth / cart / payment）仅执行 1 次并直接复用已有成果！
        assert exec_counts["product"] == 2
        assert exec_counts["order"] == 2
        assert exec_counts["auth"] == 1
        assert exec_counts["cart"] == 1
        assert exec_counts["payment"] == 1
        assert sorted(res.reused_depts) == ["auth", "cart", "payment"]

        # 验证数据库蓝图版本已升级，且底座重新生成的 ProductFacade.java 包含新方法并过完整性校验
        bps = store.list_blueprints(run_id, FactoryPhase.BACKEND)
        assert [b.status for b in bps] == [
            BlueprintStatus.SUPERSEDED.value,
            BlueprintStatus.APPROVED_FOR_BUILD.value,
        ]
        facade_src = (
            base_dir / f"backend/src/main/java/{PKG_PATH}/modules/product/api/ProductFacade.java"
        ).read_text(encoding="utf-8")
        assert "Boolean checkPromoPrice(Long productId);" in facade_src
        assert verify_base_integrity(base_dir, base_dir / "readonly-manifest.json") == []
    finally:
        engine.dispose()
        for sub in ("cache", "run-fp05"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)
