"""FP02：后端项目总管（ChiefArchitect）——模块拆分、SQL 号段与跨模块契约冻结测试。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config

from agentforge_platform.asset_registry import REPO_ROOT
from agentforge_platform.blueprint_contracts import (
    ArchitectureBlueprint,
    DtoContract,
    DtoField,
    validate_blueprint,
)
from agentforge_platform.budget_pg import PgBudgetLedger
from agentforge_platform.clients import Completion, Usage
from agentforge_platform.db import (
    drop_postgres_schema,
    ensure_postgres_database,
    ensure_postgres_schema,
    isolated_schema_url,
    make_engine,
    make_session_factory,
    postgres_reachable,
    read_local_env_var,
    session_scope,
)
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.factory_chief import CHIEF_DEPT_ID, CHIEF_ROLE, ChiefArchitect
from agentforge_platform.factory_state import (
    BlueprintStatus,
    FactoryCancelledError,
    FactoryPhase,
    FactoryStateStore,
)
from agentforge_platform.interface_registry import (
    KIND_HTTP,
    KIND_JAVA_METHOD,
    STATUS_APPROVED_FOR_BUILD,
    PublicCapability,
)
from agentforge_platform.models import Project, Run, RunEvent
from agentforge_platform.module_registry import Module
from agentforge_platform.runs import create_run, request_cancel
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import content_hash

PLATFORM_API_DIR = Path(__file__).resolve().parents[1]
CAMPUS_SPEC_PATH = REPO_ROOT / "schemas" / "campus-activity.spec.json"
BASE_PKG = "com.agentforge.shop"
PKG_PATH = "com/agentforge/shop"


class _ScriptedClient:
    """确定性脚本化模型客户端，记录收到的全部 prompt 便于断言纠偏回喂。"""

    def __init__(self, responses: list[str], *, model_name: str = "custom-chief-reasoner") -> None:
        self._responses = list(responses)
        self._model_name = model_name
        self.prompts: list[str] = []

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        thinking_mode: str | None = None,
        json_mode: bool = False,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> Completion:
        self.prompts.append(prompt)
        idx = min(len(self.prompts) - 1, len(self._responses) - 1)
        return Completion(
            content=self._responses[idx],
            model=model or self._model_name,
            request_id=f"req-fp02-{len(self.prompts)}",
            usage=Usage(input_tokens=1000, output_tokens=500),
            finish_reason="stop",
        )


def _alembic_config(url: str) -> Config:
    config = Config(str(PLATFORM_API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_API_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def _seed_run(factory) -> tuple[int, int]:
    with session_scope(factory) as db:
        project = Project(owner_id=1, name="ecommerce-sandbox", requirement_text="电商沙箱 5 模块")
        db.add(project)
        db.flush()
        run = create_run(db, project, spec_id=1, spec_hash="f" * 64)
        return project.id, run.id


def _write_custom_models_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = {
        "version": "fp02-test-price-v1",
        "currency": "CNY",
        "roles": {
            CHIEF_ROLE: {
                "provider": "openai_compatible",
                "model": "custom-chief-reasoner",
                "base_url": "https://api.example.test/v1",
                "api_key_env": "TEST_CHIEF_API_KEY",
                "enabled": True,
                "max_output_tokens": 8192,
                "supports_tools": False,
                "price_micro": {
                    "input_micro_per_million": 2_000_000,
                    "output_micro_per_million": 6_000_000,
                },
            }
        },
    }
    path = tmp_path / "models.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("AGENTFORGE_MODELS_CONFIG", str(path))
    return path


def _make_ecommerce_spec(*, module_count: int = 5, goal_suffix: str = "") -> SoftwareSpecification:
    base = json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8"))
    base["goal"] = (
        "电商沙箱项目：登录注册、首页展示、加入购物车、创建订单、模拟购买支付" + goal_suffix
    )
    default_entities = [
        "UserAccount",
        "Product",
        "CartItem",
        "PurchaseOrder",
        "PaymentRecord",
    ]
    entities = list(default_entities[:module_count])
    while len(entities) < module_count:
        entities.append(f"ExtraModule{len(entities) + 1}")

    base["entities"] = [
        {
            "name": name,
            "source": "user_confirmed",
            "fields": [
                {"name": "id", "type": "long", "required": True},
                {"name": "title", "type": "string", "required": True},
                {"name": "status", "type": "string", "required": True},
            ],
        }
        for name in entities
    ]
    base["pages"] = [
        {"name": f"{name}Page", "entity": name}
        for name in entities
    ]
    base["integrations"] = []
    base["external_requirements"] = []
    base["capability_requests"] = []
    return SoftwareSpecification.model_validate(base)


def _valid_ecommerce_blueprint(
    spec: SoftwareSpecification,
    *,
    project_id: int,
    cart_range: tuple[int, int] = (300, 399),
    sandbox_integrations: list[dict[str, Any]] | None = None,
) -> ArchitectureBlueprint:
    spec_hash = content_hash(spec)
    profile_ref = f"{spec.technical_profile.profile_id}@{spec.technical_profile.profile_version}"
    pid = str(project_id)

    def _m(
        module_id: str,
        name: str,
        sql_file: str,
        m_range: tuple[int, int],
        deps: list[str] | None = None,
        refs: list[str] | None = None,
    ) -> Module:
        return Module(
            project_id=pid,
            module_id=module_id,
            name=name,
            based_on_spec_hash=spec_hash,
            profile_ref=profile_ref,
            owned_paths=[
                f"backend/src/main/java/{PKG_PATH}/modules/{module_id}/internal/",
                f"backend/src/main/resources/db/migration/{sql_file}",
            ],
            test_paths=[f"backend/src/test/java/{PKG_PATH}/modules/{module_id}/"],
            owned_package=f"{BASE_PKG}.modules.{module_id}",
            migration_range=m_range,
            depends_on_modules=deps or [],
            readable_contract_refs=refs or [],
        )

    modules = [
        _m("auth", "登录注册部", "V100__auth_user.sql", (100, 199)),
        _m("product", "首页商品部", "V200__product_catalog.sql", (200, 299)),
        _m(
            "cart",
            "购物车部",
            "V300__cart_item.sql",
            cart_range,
            deps=["auth", "product"],
            refs=["auth.get_user_summary", "product.get_product_snapshot"],
        ),
        _m(
            "order",
            "订单履约部",
            "V400__purchase_order.sql",
            (400, 499),
            deps=["auth", "product", "cart"],
            refs=[
                "auth.get_user_summary",
                "product.get_product_snapshot",
                "cart.list_cart_items",
            ],
        ),
        _m(
            "payment",
            "模拟支付部",
            "V500__payment_record.sql",
            (500, 599),
            deps=["order"],
            refs=["order.get_order_payable"],
        ),
    ]

    shared_dtos = [
        DtoContract(
            name="UserSummaryDto",
            owner_module_id="auth",
            fields=[
                DtoField(name="userId", java_type="Long"),
                DtoField(name="username", java_type="String"),
            ],
        ),
        DtoContract(
            name="ProductSnapshotDto",
            owner_module_id="product",
            fields=[
                DtoField(name="productId", java_type="Long"),
                DtoField(name="title", java_type="String"),
                DtoField(name="unitPrice", java_type="BigDecimal"),
            ],
        ),
        DtoContract(
            name="CartItemDto",
            owner_module_id="cart",
            fields=[
                DtoField(name="itemId", java_type="Long"),
                DtoField(name="productId", java_type="Long"),
                DtoField(name="quantity", java_type="Integer"),
            ],
        ),
        DtoContract(
            name="OrderPayableDto",
            owner_module_id="order",
            fields=[
                DtoField(name="orderId", java_type="Long"),
                DtoField(name="totalAmount", java_type="BigDecimal"),
            ],
        ),
        DtoContract(
            name="PaymentReceiptDto",
            owner_module_id="payment",
            fields=[
                DtoField(name="paymentId", java_type="Long"),
                DtoField(name="orderId", java_type="Long"),
                DtoField(name="sandboxMode", java_type="Boolean"),
            ],
        ),
    ]

    frozen_contracts = [
        PublicCapability(
            capability_id="auth.get_user_summary",
            owner_module_id="auth",
            interface_kind=KIND_JAVA_METHOD,
            symbol="AuthFacade.getUserSummary",
            signature="UserSummaryDto getUserSummary(Long userId)",
            status=STATUS_APPROVED_FOR_BUILD,
            version=1,
            errors=["NOT_FOUND"],
            auth_constraints=["AUTHENTICATED"],
        ),
        PublicCapability(
            capability_id="product.get_product_snapshot",
            owner_module_id="product",
            interface_kind=KIND_JAVA_METHOD,
            symbol="ProductFacade.getProductSnapshot",
            signature="ProductSnapshotDto getProductSnapshot(Long productId)",
            status=STATUS_APPROVED_FOR_BUILD,
            version=1,
            errors=["NOT_FOUND"],
            auth_constraints=["PUBLIC"],
        ),
        PublicCapability(
            capability_id="cart.list_cart_items",
            owner_module_id="cart",
            interface_kind=KIND_JAVA_METHOD,
            symbol="CartFacade.listCartItems",
            signature="List<CartItemDto> listCartItems(Long userId)",
            status=STATUS_APPROVED_FOR_BUILD,
            version=1,
            errors=[],
            auth_constraints=["AUTHENTICATED"],
        ),
        PublicCapability(
            capability_id="order.get_order_payable",
            owner_module_id="order",
            interface_kind=KIND_JAVA_METHOD,
            symbol="OrderFacade.getOrderPayable",
            signature="OrderPayableDto getOrderPayable(Long orderId)",
            status=STATUS_APPROVED_FOR_BUILD,
            version=1,
            errors=["NOT_FOUND"],
            auth_constraints=["AUTHENTICATED"],
        ),
        PublicCapability(
            capability_id="payment.execute_sandbox_pay",
            owner_module_id="payment",
            interface_kind=KIND_JAVA_METHOD,
            symbol="PaymentFacade.executeSandboxPay",
            signature="PaymentReceiptDto executeSandboxPay(Long orderId, BigDecimal amount)",
            status=STATUS_APPROVED_FOR_BUILD,
            version=1,
            errors=["ORDER_NOT_FOUND"],
            auth_constraints=["AUTHENTICATED"],
        ),
        PublicCapability(
            capability_id="order.create_order_http",
            owner_module_id="order",
            interface_kind=KIND_HTTP,
            http_method="POST",
            http_path="/api/orders",
            signature="POST /api/orders -> OrderPayableDto",
            status=STATUS_APPROVED_FOR_BUILD,
            version=1,
            errors=["VALIDATION_ERROR", "OUT_OF_STOCK"],
            auth_constraints=["AUTHENTICATED"],
        ),
    ]

    default_sandbox = [
        {
            "id": "payment-sandbox",
            "kind": "payment",
            "sandbox_only": True,
            "mode": "sandbox_mock",
            "pattern": "Port+Adapter+Mock",
            "real_payment_allowed": False,
        }
    ]

    return ArchitectureBlueprint(
        project_id=pid,
        based_on_spec_hash=spec_hash,
        profile_ref=profile_ref,
        base_package=BASE_PKG,
        modules=modules,
        frozen_contracts=frozen_contracts,
        shared_dtos=shared_dtos,
        covered_entities={
            "auth": ["UserAccount"],
            "product": ["Product"],
            "cart": ["CartItem"],
            "order": ["PurchaseOrder"],
            "payment": ["PaymentRecord"],
        },
        covered_acceptance_ids={
            "auth": [ac.id for ac in spec.acceptance_cases],
        },
        sandbox_integrations=(
            default_sandbox if sandbox_integrations is None else sandbox_integrations
        ),
    )


def test_ecommerce_5_module_blueprint_frozen_and_billed_via_custom_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_custom_models_config(tmp_path, monkeypatch)
    url = f"sqlite+pysqlite:///{tmp_path / 'fp02_happy.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        project_id, run_id = _seed_run(factory)
        spec = _make_ecommerce_spec(module_count=5)
        expected_bp = _valid_ecommerce_blueprint(spec, project_id=project_id)
        scripted = _ScriptedClient([expected_bp.model_dump_json()])

        chief = ChiefArchitect(
            session_factory=factory,
            client=scripted,
            base_package=BASE_PKG,
        )
        frozen = chief.plan_backend_blueprint(spec, spec.technical_profile, run_id)

        assert validate_blueprint(frozen, spec) == []
        assert len(frozen.modules) == 5
        assert [m.module_id for m in frozen.modules] == [
            "auth",
            "product",
            "cart",
            "order",
            "payment",
        ]
        assert len(scripted.prompts) == 1

        # 验证已持久化至 factory_blueprint 表并冻结为 APPROVED_FOR_BUILD
        store = FactoryStateStore(factory)
        saved = store.get_latest_blueprint(run_id, FactoryPhase.BACKEND, approved_only=True)
        assert saved is not None
        assert saved.status == BlueprintStatus.APPROVED_FOR_BUILD.value
        assert saved.version == 1
        assert saved.blueprint_hash == frozen.blueprint_hash()

        # 验证经 PgBudgetLedger 按 phase=backend, dept_id=chief, round=1 记账
        ledger = PgBudgetLedger(factory)
        cost = ledger.cost_by_department(run_id)
        chief_bucket = cost["by_phase"]["backend"]["departments"][CHIEF_DEPT_ID]
        assert chief_bucket["calls"] == 1
        assert chief_bucket["settled_calls"] == 1
        # 1000 * 2_000_000 / 1_000_000 + 500 * 6_000_000 / 1_000_000 = 2_000 + 3_000 = 5_000
        assert chief_bucket["cost_micro"] == 5_000
        assert chief_bucket["by_round"]["1"]["calls"] == 1
    finally:
        engine.dispose()


def test_negative_first_round_sql_range_conflict_corrected_on_round_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_custom_models_config(tmp_path, monkeypatch)
    url = f"sqlite+pysqlite:///{tmp_path / 'fp02_retry_ok.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        project_id, run_id = _seed_run(factory)
        spec = _make_ecommerce_spec(module_count=5)

        # 第 1 轮故意制造 SQL 号段冲突：cart 与 product 同占 200-299；第 2 轮纠正为 300-399
        bad_bp = _valid_ecommerce_blueprint(spec, project_id=project_id, cart_range=(200, 299))
        good_bp = _valid_ecommerce_blueprint(spec, project_id=project_id, cart_range=(300, 399))
        scripted = _ScriptedClient([bad_bp.model_dump_json(), good_bp.model_dump_json()])

        chief = ChiefArchitect(
            session_factory=factory,
            client=scripted,
            base_package=BASE_PKG,
        )
        frozen = chief.plan_backend_blueprint(spec, "java-spring-vue@1.0.0", run_id)

        assert len(scripted.prompts) == 2
        # 第 2 轮 Prompt 必须收到第 1 轮的确定性号段重叠错误回喂
        assert "号段重叠" in scripted.prompts[1] or "重叠" in scripted.prompts[1]
        assert validate_blueprint(frozen, spec) == []

        store = FactoryStateStore(factory)
        saved = store.get_latest_blueprint(run_id, FactoryPhase.BACKEND, approved_only=True)
        assert saved is not None and saved.blueprint_hash == frozen.blueprint_hash()

        # 两轮调用均按 phase=backend, dept_id=chief 入账
        cost = PgBudgetLedger(factory).cost_by_department(run_id)
        chief_bucket = cost["by_phase"]["backend"]["departments"][CHIEF_DEPT_ID]
        assert chief_bucket["calls"] == 2
        assert chief_bucket["cost_micro"] == 10_000
        assert set(chief_bucket["by_round"].keys()) == {"1", "2"}

        with session_scope(factory) as db:
            event_types = [
                row.type
                for row in db.query(RunEvent).filter(RunEvent.run_id == run_id).order_by(RunEvent.sequence).all()
            ]
        assert "factory_blueprint_retry" in event_types
        assert "factory_blueprint" in event_types
    finally:
        engine.dispose()


def test_negative_consecutive_invalid_rounds_stop_with_blueprint_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_custom_models_config(tmp_path, monkeypatch)
    url = f"sqlite+pysqlite:///{tmp_path / 'fp02_invalid.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        project_id, run_id = _seed_run(factory)
        spec = _make_ecommerce_spec(module_count=5)
        # 故意持续输出 SQL 号段冲突的非法蓝图
        bad_bp = _valid_ecommerce_blueprint(spec, project_id=project_id, cart_range=(200, 299))

        # 1. max_retries=1 → 连续 2 轮非法立即报 BLUEPRINT_INVALID 停止
        scripted_2 = _ScriptedClient([bad_bp.model_dump_json(), bad_bp.model_dump_json()])
        chief_2 = ChiefArchitect(
            session_factory=factory,
            client=scripted_2,
            base_package=BASE_PKG,
            max_retries=1,
        )
        with pytest.raises(AppException) as exc:
            chief_2.plan_backend_blueprint(spec, spec.technical_profile, run_id)
        assert exc.value.code == ErrorCode.BLUEPRINT_INVALID
        assert len(scripted_2.prompts) == 2
        assert (
            FactoryStateStore(factory).get_latest_blueprint(
                run_id, FactoryPhase.BACKEND, approved_only=True
            )
            is None
        )

        # 2. 默认 max_retries=2（首次 + 最多重试 2 轮 = 3 轮）全部非法 → 报 BLUEPRINT_INVALID
        _, run_id_2 = _seed_run(factory)
        scripted_3 = _ScriptedClient(
            [bad_bp.model_dump_json(), bad_bp.model_dump_json(), bad_bp.model_dump_json()]
        )
        chief_3 = ChiefArchitect(
            session_factory=factory,
            client=scripted_3,
            base_package=BASE_PKG,
        )
        with pytest.raises(AppException) as exc3:
            chief_3.plan_backend_blueprint(spec, spec.technical_profile, run_id_2)
        assert exc3.value.code == ErrorCode.BLUEPRINT_INVALID
        assert len(scripted_3.prompts) == 3
        cost_2 = PgBudgetLedger(factory).cost_by_department(run_id_2)
        assert cost_2["by_phase"]["backend"]["departments"][CHIEF_DEPT_ID]["calls"] == 3
    finally:
        engine.dispose()


def test_negative_over_8_modules_hard_rejected_for_spec_and_model_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_custom_models_config(tmp_path, monkeypatch)
    url = f"sqlite+pysqlite:///{tmp_path / 'fp02_over8.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        project_id, run_id = _seed_run(factory)

        # 场景 A：输入冻结规格本身超过 8 个模块（9 模块）→ 模型调用前直接硬拒绝，0 Token 消耗
        spec_9 = _make_ecommerce_spec(module_count=9)
        scripted_unused = _ScriptedClient(["{}"])
        chief = ChiefArchitect(
            session_factory=factory,
            client=scripted_unused,
            base_package=BASE_PKG,
        )
        with pytest.raises(AppException) as exc:
            chief.plan_backend_blueprint(spec_9, spec_9.technical_profile, run_id)
        assert exc.value.code == ErrorCode.UNSUPPORTED_SCOPE
        assert len(scripted_unused.prompts) == 0

        # 场景 B：规格合法（5 模块），但模型强行拆出 9 个模块 → 硬校验拒绝，报 BLUEPRINT_INVALID
        spec_5 = _make_ecommerce_spec(module_count=5)
        bp_5 = _valid_ecommerce_blueprint(spec_5, project_id=project_id)
        extra_mods = [
            Module(
                project_id=str(project_id),
                module_id=f"extra{i}",
                name=f"扩展部{i}",
                based_on_spec_hash=content_hash(spec_5),
                profile_ref=bp_5.profile_ref,
                owned_paths=[
                    f"backend/src/main/java/{PKG_PATH}/modules/extra{i}/internal/",
                    f"backend/src/main/resources/db/migration/V{550 + i * 20}__extra{i}.sql",
                ],
                test_paths=[f"backend/src/test/java/{PKG_PATH}/modules/extra{i}/"],
                owned_package=f"{BASE_PKG}.modules.extra{i}",
                migration_range=(550 + i * 20, 569 + i * 20),
            )
            for i in range(1, 5)
        ]
        bp_9 = bp_5.model_copy(update={"modules": [*bp_5.modules, *extra_mods]})
        scripted_9 = _ScriptedClient([bp_9.model_dump_json()])
        chief_9 = ChiefArchitect(
            session_factory=factory,
            client=scripted_9,
            base_package=BASE_PKG,
            max_retries=1,
        )
        with pytest.raises(AppException) as exc_b:
            chief_9.plan_backend_blueprint(spec_5, spec_5.technical_profile, run_id)
        assert exc_b.value.code == ErrorCode.BLUEPRINT_INVALID
        assert "上限" in exc_b.value.message
    finally:
        engine.dispose()


def test_negative_real_production_payment_hard_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_custom_models_config(tmp_path, monkeypatch)
    url = f"sqlite+pysqlite:///{tmp_path / 'fp02_prod_pay.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        project_id, run_id = _seed_run(factory)

        # 场景 A：规格要求真实扣款 → 前置硬拦截，0 模型调用
        bad_spec = _make_ecommerce_spec(module_count=5, goal_suffix="，需要接入真实扣款与生产商户私钥")
        scripted_unused = _ScriptedClient(["{}"])
        chief = ChiefArchitect(
            session_factory=factory,
            client=scripted_unused,
            base_package=BASE_PKG,
        )
        with pytest.raises(AppException) as exc:
            chief.plan_backend_blueprint(bad_spec, bad_spec.technical_profile, run_id)
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert len(scripted_unused.prompts) == 0

        # 场景 B：规格合法，但模型输出 sandbox_only=False（生产支付模式）→ 硬校验拒绝
        spec = _make_ecommerce_spec(module_count=5)
        live_bp = _valid_ecommerce_blueprint(
            spec,
            project_id=project_id,
            sandbox_integrations=[
                {
                    "id": "payment-sandbox",
                    "kind": "payment",
                    "sandbox_only": False,
                    "mode": "live",
                    "real_payment_allowed": True,
                }
            ],
        )
        chief_live = ChiefArchitect(
            session_factory=factory,
            client=_ScriptedClient([live_bp.model_dump_json()]),
            base_package=BASE_PKG,
            max_retries=1,
        )
        with pytest.raises(AppException) as exc_b:
            chief_live.plan_backend_blueprint(spec, spec.technical_profile, run_id)
        assert exc_b.value.code == ErrorCode.BLUEPRINT_INVALID
        assert "sandbox_only" in exc_b.value.message
    finally:
        engine.dispose()


def test_triage_placeholder_and_cancel_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_custom_models_config(tmp_path, monkeypatch)
    url = f"sqlite+pysqlite:///{tmp_path / 'fp02_misc.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        project_id, run_id = _seed_run(factory)
        spec = _make_ecommerce_spec(module_count=5)
        bp = _valid_ecommerce_blueprint(spec, project_id=project_id)
        scripted = _ScriptedClient([bp.model_dump_json()])
        chief = ChiefArchitect(session_factory=factory, client=scripted, base_package=BASE_PKG)

        # 取消信号已置位 → plan_backend_blueprint 与 triage_integration_failures 均不发起任何模型调用
        with session_scope(factory) as db:
            run = db.get(Run, run_id)
            assert run is not None
            request_cancel(db, run)
        with pytest.raises(FactoryCancelledError):
            chief.plan_backend_blueprint(spec, spec.technical_profile, run_id)
        with pytest.raises(FactoryCancelledError):
            chief.triage_integration_failures(
                run_id=run_id,
                phase=FactoryPhase.BACKEND,
                blueprint=bp,
                failure_report={"checks": []},
            )
        assert len(scripted.prompts) == 0
    finally:
        engine.dispose()


def test_live_postgres_chief_architect_blueprint_freeze_and_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实连本地/隧道 PostgreSQL：在隔离 Schema 验证总管冻结蓝图入 factory_blueprint 与 PG 账本记账。"""
    pg_url = (
        read_local_env_var("AGENTFORGE_TEST_DATABASE_URL")
        or read_local_env_var("AGENTFORGE_DATABASE_URL")
    )
    if not pg_url or not postgres_reachable(pg_url):
        pytest.skip("NOT_RUN: 本地 PostgreSQL 或云端隧道未连接，跳过实库 PG FP02 验证")

    _write_custom_models_config(tmp_path, monkeypatch)
    ensure_postgres_database(pg_url)
    schema = f"test_fp02_{uuid.uuid4().hex[:8]}"
    ensure_postgres_schema(pg_url, schema)
    scoped_url = isolated_schema_url(pg_url, schema)

    try:
        command.upgrade(_alembic_config(scoped_url), "head")
        engine = make_engine(scoped_url)
        factory = make_session_factory(engine)
        try:
            project_id, run_id = _seed_run(factory)
            spec = _make_ecommerce_spec(module_count=5)
            bad_bp = _valid_ecommerce_blueprint(spec, project_id=project_id, cart_range=(200, 299))
            good_bp = _valid_ecommerce_blueprint(spec, project_id=project_id, cart_range=(300, 399))

            chief = ChiefArchitect(
                session_factory=factory,
                client=_ScriptedClient([bad_bp.model_dump_json(), good_bp.model_dump_json()]),
                base_package=BASE_PKG,
            )
            frozen = chief.plan_backend_blueprint(spec, spec.technical_profile, run_id)
            assert validate_blueprint(frozen, spec) == []

            saved = FactoryStateStore(factory).get_latest_blueprint(
                run_id, FactoryPhase.BACKEND, approved_only=True
            )
            assert saved is not None
            assert saved.status == BlueprintStatus.APPROVED_FOR_BUILD.value
            assert saved.blueprint_hash == frozen.blueprint_hash()

            cost = PgBudgetLedger(factory).cost_by_department(run_id)
            assert cost["by_phase"]["backend"]["departments"][CHIEF_DEPT_ID]["calls"] == 2
        finally:
            engine.dispose()
    finally:
        drop_postgres_schema(pg_url, schema)
