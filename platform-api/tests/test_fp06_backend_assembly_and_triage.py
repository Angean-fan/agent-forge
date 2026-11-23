"""FP06：后端总装、权威 openapi.json 聚合与总验收归因打回大循环（强制 Docker Runner）测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config

from agentforge_platform.asset_cache import (
    AssetMaterializer,
    make_writable_tree,
    remove_readonly_tree,
)
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
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.factory_assembler import (
    AUTHORITATIVE_OPENAPI_REL,
    RELATIONS_MIGRATION_REL,
    merge_department_deliverables,
)
from agentforge_platform.factory_backend_stage import run_backend_factory_stage
from agentforge_platform.factory_base_builder import build_backend_base
from agentforge_platform.factory_chief import CHIEF_DEPT_ID, CHIEF_ROLE, ChiefArchitect
from agentforge_platform.factory_department import API_FRAGMENT_FILENAME
from agentforge_platform.factory_scheduler import AsyncDepartmentPool
from agentforge_platform.factory_state import DeptStatus, FactoryPhase, FactoryStateStore
from agentforge_platform.factory_tickets import DefectTicket, DepartmentDeliverable, TestEvidence
from agentforge_platform.interface_registry import (
    KIND_HTTP,
    KIND_JAVA_METHOD,
    STATUS_APPROVED_FOR_BUILD,
    PublicCapability,
)
from agentforge_platform.models import Project
from agentforge_platform.module_registry import Module
from agentforge_platform.module_verifier import (
    LocalProcessModuleVerifier,
    ModuleVerificationResult,
    ModuleVerifier,
)
from agentforge_platform.resolver import resolve_assets
from agentforge_platform.runs import create_run
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
        "version": "fp06-test-v1",
        "currency": "CNY",
        "roles": {
            CHIEF_ROLE: {
                "provider": "openai_compatible",
                "model": "test-chief-triage",
                "base_url": "https://api.example.test/v1",
                "api_key_env": "TEST_FP06_KEY",
                "enabled": True,
                "max_output_tokens": 4096,
                "supports_tools": False,
                "price_micro": {
                    "input_micro_per_million": 1_000_000,
                    "output_micro_per_million": 2_000_000,
                },
            }
        },
    }
    p = tmp_path / "models.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("AGENTFORGE_MODELS_CONFIG", str(p))


class _ScriptedChiefClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, *, model: str, prompt: str, **kwargs: Any) -> Completion:
        self.prompts.append(prompt)
        idx = min(len(self.prompts) - 1, len(self._responses) - 1)
        return Completion(
            content=self._responses[idx],
            model=model,
            request_id=f"req-chief-triage-{len(self.prompts)}",
            usage=Usage(input_tokens=500, output_tokens=200),
            finish_reason="stop",
        )


class _DummyDeptVerifier:
    verifier_kind = "local_process"

    def verify_module(
        self,
        *,
        artifact_dir: str | Path,
        module_id: str,
        stack: str = "java",
        test_selector: str = "*Test",
        timeout_seconds: int = 120,
    ) -> ModuleVerificationResult:
        return ModuleVerificationResult(
            artifact_dir=str(artifact_dir),
            module_id=module_id,
            stack="java",
            test_selector=test_selector,
            status="PASS",
            verifier_kind="local_process",
            exit_code=0,
        )


class _FakeDockerRunner:
    """模拟独立 Docker Runner（实现 RunnerClient 协议，区分于 LocalProcessModuleVerifier）。"""

    def __init__(
        self,
        *,
        verify_fn: Any,
        accept_fn: Any | None = None,
    ) -> None:
        self._verify_fn = verify_fn
        self._accept_fn = accept_fn or (
            lambda _artifact_dir: {
                "overall": "PASS",
                "scenarios": [
                    {"name": "ecommerce_checkout_flow", "status": "PASS"},
                    {"name": "payment_sandbox_callback", "status": "PASS"},
                ],
            }
        )
        self.verify_calls: list[str] = []
        self.accept_calls: list[str] = []

    def verify(self, artifact_dir: str) -> dict[str, Any]:
        self.verify_calls.append(artifact_dir)
        return self._verify_fn(artifact_dir)

    def accept(self, artifact_dir: str) -> dict[str, Any]:
        self.accept_calls.append(artifact_dir)
        return self._accept_fn(artifact_dir)


def _setup_5_dept_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_models(tmp_path, monkeypatch)
    url = f"sqlite+pysqlite:///{tmp_path / 'fp06.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)

    with session_scope(factory) as db:
        PgAssetStore(db).sync_from_filesystem()
        project = Project(owner_id=1, name="ecommerce-fp06", requirement_text="电商 5 部门总装")
        db.add(project)
        db.flush()
        run = create_run(db, project, spec_id=1, spec_hash="c" * 64)
        project_id, run_id = project.id, run.id

    base = json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8"))
    base["goal"] = "电商沙箱 5 部门后端总装与集成验收"
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
    base["relations"] = [
        {
            "from_table": "cart_item",
            "from_column": "product_id",
            "to_table": "product",
            "to_column": "id",
        }
    ]
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
            "支付沙箱部",
            "V500__payment.sql",
            (500, 599),
            deps=["order"],
            refs=["order.get_payable"],
        ),
    ]

    dtos = [
        DtoContract(
            name="ProductDto",
            owner_module_id="product",
            fields=[
                DtoField(name="productId", java_type="Long"),
                DtoField(name="title", java_type="String"),
            ],
        ),
        DtoContract(
            name="OrderDto",
            owner_module_id="order",
            fields=[
                DtoField(name="orderId", java_type="Long"),
                DtoField(name="totalAmount", java_type="BigDecimal"),
            ],
        ),
    ]

    contracts = [
        PublicCapability(
            capability_id="auth.get_user",
            owner_module_id="auth",
            interface_kind=KIND_JAVA_METHOD,
            symbol="AuthFacade.getUser",
            signature="String getUser(Long userId)",
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
            signature="List<String> listItems(Long userId)",
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
            signature="Boolean paySandbox(Long orderId)",
            status=STATUS_APPROVED_FOR_BUILD,
            version=1,
        ),
        PublicCapability(
            capability_id="order.create_order",
            owner_module_id="order",
            interface_kind=KIND_HTTP,
            http_method="POST",
            http_path="/api/orders",
            signature="POST /api/orders -> OrderDto",
            status=STATUS_APPROVED_FOR_BUILD,
            version=1,
            auth_constraints=["AUTHENTICATED"],
        ),
    ]

    bp = ArchitectureBlueprint(
        project_id=str(project_id),
        based_on_spec_hash=spec_hash,
        profile_ref=profile_ref,
        base_package=BASE_PKG,
        modules=modules,
        shared_dtos=dtos,
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

    run_dir = tmp_path / "run-fp06"
    lock = resolve_assets(default_registry(), "demo-ts-mp")
    sess = factory()
    try:
        mat = AssetMaterializer(sess, cache_root=tmp_path / "cache")
        base_res = build_backend_base(run_dir, bp, lock, mat)
        base_dir = Path(base_res.base_dir)
    finally:
        sess.close()

    return engine, factory, run_id, spec, bp, run_dir, base_dir


def _materialized_dept_staging(base_dir: Path, run_dir: Path, dept_id: str) -> Path:
    staging = run_dir / "staging" / "backend" / dept_id
    if not staging.exists():
        staging.mkdir(parents=True, exist_ok=True)
        shutil.copytree(base_dir / "backend", staging / "backend")
        make_writable_tree(staging / "backend")
    return staging


def test_cross_module_mysql_fk_mismatch_triaged_only_reworks_cart_and_freezes_openapi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, run_id, spec, bp, run_dir, base_dir = _setup_5_dept_backend(
        tmp_path, monkeypatch
    )
    try:
        store = FactoryStateStore(factory)
        exec_counts: dict[str, int] = {}
        lock = threading.Lock()

        sql_by_dept = {
            "auth": ("V100__auth.sql", "CREATE TABLE auth_profile (id BIGINT PRIMARY KEY);\n"),
            "product": ("V200__product.sql", "CREATE TABLE product (id BIGINT PRIMARY KEY, title VARCHAR(120));\n"),
            "cart": (
                "V300__cart.sql",
                # 故意制造跨模块集成缺陷：cart_item.product_id 类型为 VARCHAR(32)，与 product.id BIGINT 不兼容！
                "CREATE TABLE cart_item (id BIGINT PRIMARY KEY, product_id VARCHAR(32) NOT NULL);\n",
            ),
            "order": ("V400__order.sql", "CREATE TABLE purchase_order (id BIGINT PRIMARY KEY);\n"),
            "payment": ("V500__payment.sql", "CREATE TABLE payment_record (id BIGINT PRIMARY KEY);\n"),
        }

        async def _dept_executor(
            rid: int,
            module: Module,
            _cur_bp: ArchitectureBlueprint,
            _ver: ModuleVerifier,
            ticket: DefectTicket | None,
        ) -> DepartmentDeliverable:
            dept_id = module.module_id
            with lock:
                exec_counts[dept_id] = exec_counts.get(dept_id, 0) + 1
                run_no = exec_counts[dept_id]

            staging = _materialized_dept_staging(base_dir, run_dir, dept_id)
            store.transition_department(rid, FactoryPhase.BACKEND, dept_id, DeptStatus.PLANNING, current_round=run_no)
            store.transition_department(rid, FactoryPhase.BACKEND, dept_id, DeptStatus.CODING)

            sql_name, sql_body = sql_by_dept[dept_id]
            # cart 收到总管归因缺陷单后，修正 product_id 为 BIGINT
            if dept_id == "cart" and ticket is not None:
                assert ticket.error_class == "FLYWAY_MIGRATION_FK_MISMATCH"
                sql_body = "CREATE TABLE cart_item (id BIGINT PRIMARY KEY, product_id BIGINT NOT NULL);\n"

            sql_file = staging / "backend/src/main/resources/db/migration" / sql_name
            sql_file.write_text(sql_body, encoding="utf-8")

            svc_file = (
                staging
                / f"backend/src/main/java/{PKG_PATH}/modules/{dept_id}/internal/{dept_id.title()}Service.java"
            )
            svc_file.parent.mkdir(parents=True, exist_ok=True)
            svc_file.write_text(
                f"package {BASE_PKG}.modules.{dept_id}.internal;\npublic class {dept_id.title()}Service {{}}\n",
                encoding="utf-8",
            )

            frag = {
                "paths": {
                    f"/api/{dept_id}": {
                        "get": {"summary": f"List {dept_id}", "responses": {"200": {"description": "OK"}}}
                    }
                }
            }
            (staging / API_FRAGMENT_FILENAME).write_text(
                json.dumps(frag, ensure_ascii=False), encoding="utf-8"
            )

            store.transition_department(rid, FactoryPhase.BACKEND, dept_id, DeptStatus.TESTING)
            ev = TestEvidence(
                dept_id=dept_id,
                phase="backend",
                round=run_no,
                verifier_kind="local_process",
                stack="java",
                test_selector=f"{dept_id}Test",
                status="PASS",
                executed=True,
                exit_code=0,
            )
            store.transition_department(
                rid,
                FactoryPhase.BACKEND,
                dept_id,
                DeptStatus.PASSED,
                api_fragment_json=json.dumps(frag, ensure_ascii=False),
                evidence_json=ev.model_dump_json(),
            )
            return DepartmentDeliverable(
                run_id=rid,
                phase="backend",
                dept_id=dept_id,
                status="PASSED",
                rounds_used=run_no,
                staging_dir=str(staging),
                api_fragment_path=API_FRAGMENT_FILENAME,
                api_fragment=frag,
                evidence=ev,
            )

        def _docker_verify(artifact_dir: str) -> dict[str, Any]:
            assembled = Path(artifact_dir)
            # 确认自动生成了 V900__relations.sql
            v900 = assembled / RELATIONS_MIGRATION_REL
            assert v900.is_file()
            assert "FOREIGN KEY (product_id) REFERENCES product(id)" in v900.read_text(encoding="utf-8")

            v300 = (assembled / "backend/src/main/resources/db/migration/V300__cart.sql").read_text(
                encoding="utf-8"
            )
            if "VARCHAR(32)" in v300:
                return {
                    "overall": "FAIL",
                    "checks": [
                        {"name": "static_manifest", "status": "PASS"},
                        {"name": "backend_test", "status": "PASS"},
                        {"name": "backend_build", "status": "PASS"},
                        {
                            "name": "mysql_migration",
                            "status": "FAIL",
                            "error_class": "FLYWAY_MIGRATION_FK_MISMATCH",
                            "detail": "V900__relations.sql failed: cart_item.product_id VARCHAR(32) incompatible with product.id BIGINT",
                        },
                    ],
                }
            return {
                "overall": "PASS",
                "checks": [
                    {"name": "static_manifest", "status": "PASS"},
                    {"name": "backend_test", "status": "PASS"},
                    {"name": "backend_build", "status": "PASS"},
                    {"name": "mysql_migration", "status": "PASS"},
                ],
            }

        triage_json = json.dumps(
            {
                "defects": [
                    {
                        "dept_id": "cart",
                        "error_class": "FLYWAY_MIGRATION_FK_MISMATCH",
                        "summary": "cart_item.product_id 定义为 VARCHAR(32)，与 product.id BIGINT 外键类型不匹配",
                        "affected_paths": ["backend/src/main/resources/db/migration/V300__cart.sql"],
                        "failing_tests": ["mysql_migration"],
                        "log_excerpt": "FK constraint fk_v900_1_cart_item_product failed: VARCHAR(32) != BIGINT",
                    }
                ]
            },
            ensure_ascii=False,
        )
        chief_client = _ScriptedChiefClient([triage_json])
        chief = ChiefArchitect(session_factory=factory, client=chief_client, base_package=BASE_PKG)
        docker_runner = _FakeDockerRunner(verify_fn=_docker_verify)
        pool = AsyncDepartmentPool(
            session_factory=factory,
            verifier=_DummyDeptVerifier(),
            dept_executor=_dept_executor,
        )

        result = asyncio.run(
            run_backend_factory_stage(
                run_id=run_id,
                blueprint=bp,
                spec=spec,
                run_dir=run_dir,
                base_dir=base_dir,
                pool=pool,
                chief=chief,
                runner_client=docker_runner,
                state_store=store,
            )
        )

        assert result.ok is True
        assert result.status == "PASSED"
        assert result.integration_rounds_used == 2
        assert result.rework_rounds == 1
        assert result.reworked_depts_history == [["cart"]]

        # 核心断言：仅 cart 被定向打回重跑（2 次），其余 4 个部门原地保留不重跑（各 1 次）
        assert exec_counts == {
            "auth": 1,
            "product": 1,
            "cart": 2,
            "order": 1,
            "payment": 1,
        }

        # 总管归因调用已走 PgBudgetLedger 入账（phase=backend, dept_id=chief）
        cost = PgBudgetLedger(factory).cost_by_department(run_id)
        assert cost["by_phase"]["backend"]["departments"][CHIEF_DEPT_ID]["calls"] == 1

        # 权威 backend/openapi.json 在总验收全 PASS 后正式冻结
        assert result.frozen_openapi_path is not None
        assert result.frozen_openapi_sha256 is not None
        frozen_file = Path(result.frozen_openapi_path)
        assert frozen_file.is_file()
        assert hashlib.sha256(frozen_file.read_bytes()).hexdigest() == result.frozen_openapi_sha256
        frozen_doc = json.loads(frozen_file.read_text(encoding="utf-8"))
        assert frozen_doc["x-agentforge-frozen"] is True
        for expected_path in ("/api/auth", "/api/product", "/api/cart", "/api/order", "/api/payment", "/api/orders"):
            assert expected_path in frozen_doc["paths"]
    finally:
        engine.dispose()
        for sub in ("cache", "run-fp06"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)


def test_negative_infrastructure_failure_stops_with_zero_rework(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, run_id, spec, bp, run_dir, base_dir = _setup_5_dept_backend(
        tmp_path, monkeypatch
    )
    try:
        store = FactoryStateStore(factory)
        deliverables: dict[str, DepartmentDeliverable] = {}
        for mod in bp.modules:
            staging = _materialized_dept_staging(base_dir, run_dir, mod.module_id)
            store.register_department(run_id, FactoryPhase.BACKEND, mod.module_id, staging_dir=str(staging))
            store.transition_department(run_id, FactoryPhase.BACKEND, mod.module_id, DeptStatus.PLANNING, current_round=1)
            store.transition_department(run_id, FactoryPhase.BACKEND, mod.module_id, DeptStatus.CODING)
            store.transition_department(run_id, FactoryPhase.BACKEND, mod.module_id, DeptStatus.TESTING)
            store.transition_department(run_id, FactoryPhase.BACKEND, mod.module_id, DeptStatus.PASSED)
            deliverables[mod.module_id] = DepartmentDeliverable(
                run_id=run_id,
                phase="backend",
                dept_id=mod.module_id,
                status="PASSED",
                rounds_used=1,
                staging_dir=str(staging),
            )

        chief_client = _ScriptedChiefClient(['{"defects":[]}'])
        chief = ChiefArchitect(session_factory=factory, client=chief_client, base_package=BASE_PKG)
        pool = AsyncDepartmentPool(session_factory=factory, verifier=_DummyDeptVerifier())

        # 场景 A：Docker Runner 返回 INFRASTRUCTURE（镜像缺失/Docker 不可达）→ 返工次数必须为 0，绝不调用总管归因
        infra_runner = _FakeDockerRunner(
            verify_fn=lambda _dir: {
                "overall": "INFRASTRUCTURE",
                "error_code": "INFRASTRUCTURE",
                "checks": [
                    {
                        "name": "mysql_migration",
                        "status": "NOT_RUN",
                        "category": "INFRASTRUCTURE",
                        "detail": "Cannot connect to the Docker daemon",
                    }
                ],
            }
        )
        res_a = asyncio.run(
            run_backend_factory_stage(
                run_id=run_id,
                blueprint=bp,
                spec=spec,
                run_dir=run_dir,
                base_dir=base_dir,
                pool=pool,
                chief=chief,
                runner_client=infra_runner,
                state_store=store,
                deliverables=deliverables,
            )
        )
        assert res_a.ok is False
        assert res_a.status == "INFRASTRUCTURE"
        assert res_a.error_code == "INFRASTRUCTURE"
        assert res_a.rework_rounds == 0
        assert res_a.frozen_openapi_path is None
        assert len(chief_client.prompts) == 0

        # 场景 B：Runner 抛出连接异常 → 同样标 INFRASTRUCTURE，返工次数为 0
        def _conn_refused(_dir: str) -> dict[str, Any]:
            raise RuntimeError("Runner 连接失败：ConnectionRefusedError")

        res_b = asyncio.run(
            run_backend_factory_stage(
                run_id=run_id,
                blueprint=bp,
                spec=spec,
                run_dir=run_dir,
                base_dir=base_dir,
                pool=pool,
                chief=chief,
                runner_client=_FakeDockerRunner(verify_fn=_conn_refused),
                state_store=store,
                deliverables=deliverables,
            )
        )
        assert res_b.status == "INFRASTRUCTURE"
        assert res_b.rework_rounds == 0
        assert len(chief_client.prompts) == 0
    finally:
        engine.dispose()
        for sub in ("cache", "run-fp06"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)


def test_negative_local_process_verifier_forbidden_for_backend_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, run_id, spec, bp, run_dir, base_dir = _setup_5_dept_backend(
        tmp_path, monkeypatch
    )
    try:
        store = FactoryStateStore(factory)
        chief = ChiefArchitect(
            session_factory=factory,
            client=_ScriptedChiefClient(["{}"]),
            base_package=BASE_PKG,
        )
        pool = AsyncDepartmentPool(session_factory=factory, verifier=_DummyDeptVerifier())
        local_verifier = LocalProcessModuleVerifier(work_root=tmp_path)

        with pytest.raises(AppException) as exc:
            asyncio.run(
                run_backend_factory_stage(
                    run_id=run_id,
                    blueprint=bp,
                    spec=spec,
                    run_dir=run_dir,
                    base_dir=base_dir,
                    pool=pool,
                    chief=chief,
                    runner_client=local_verifier,  # type: ignore[arg-type]
                    state_store=store,
                )
            )
        assert exc.value.code == ErrorCode.FORBIDDEN
        assert exc.value.status_code == 403
    finally:
        engine.dispose()
        for sub in ("cache", "run-fp06"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)


def test_negative_merge_blocks_path_conflict_facade_tampering_and_sql_range_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _factory, run_id, spec, bp, _run_dir, base_dir = _setup_5_dept_backend(
        tmp_path, monkeypatch
    )
    try:
        def _fresh_deliverables(tag: str) -> dict[str, DepartmentDeliverable]:
            out: dict[str, DepartmentDeliverable] = {}
            sub_root = tmp_path / tag
            for mod in bp.modules:
                st = _materialized_dept_staging(base_dir, sub_root, mod.module_id)
                out[mod.module_id] = DepartmentDeliverable(
                    run_id=run_id,
                    phase="backend",
                    dept_id=mod.module_id,
                    status="PASSED",
                    rounds_used=1,
                    staging_dir=str(st),
                )
            return out

        # 1. 两部门写了同一文件路径 → 拒绝合并并生成 CONTRACT_MISMATCH 缺陷单
        deliv_conflict = _fresh_deliverables("case-conflict")
        shared_rel = f"backend/src/main/java/{PKG_PATH}/modules/shared/DupUtil.java"
        for d in ("cart", "order"):
            f = Path(deliv_conflict[d].staging_dir) / shared_rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("class DupUtil {}\n", encoding="utf-8")
        res_conflict = merge_department_deliverables(
            base_dir,
            deliv_conflict,
            tmp_path / "out-conflict",
            blueprint=bp,
            spec=spec,
            run_id=run_id,
        )
        assert res_conflict.ok is False
        assert any(t.error_class == "CONTRACT_MISMATCH" for t in res_conflict.conflict_tickets)
        assert {"cart", "order"} <= {t.dept_id for t in res_conflict.conflict_tickets}

        # 2. cart 篡改 product 的只读 api/ProductFacade.java 签名 → 拒绝合并并报 CONTRACT_MISMATCH
        deliv_tamper = _fresh_deliverables("case-tamper")
        facade = (
            Path(deliv_tamper["cart"].staging_dir)
            / f"backend/src/main/java/{PKG_PATH}/modules/product/api/ProductFacade.java"
        )
        facade.write_text(facade.read_text(encoding="utf-8") + "\n// tampered\n", encoding="utf-8")
        res_tamper = merge_department_deliverables(
            base_dir,
            deliv_tamper,
            tmp_path / "out-tamper",
            blueprint=bp,
            spec=spec,
            run_id=run_id,
        )
        assert res_tamper.ok is False
        assert any(
            t.dept_id == "cart" and t.error_class == "CONTRACT_MISMATCH"
            for t in res_tamper.conflict_tickets
        )

        # 3. cart（号段 300-399）越界写入 V250__hijack.sql → 拒绝合并并报 CONTRACT_MISMATCH
        deliv_sql = _fresh_deliverables("case-sql")
        bad_sql = (
            Path(deliv_sql["cart"].staging_dir)
            / "backend/src/main/resources/db/migration/V250__hijack.sql"
        )
        bad_sql.write_text("CREATE TABLE bad_tbl (id BIGINT);\n", encoding="utf-8")
        res_sql = merge_department_deliverables(
            base_dir,
            deliv_sql,
            tmp_path / "out-sql",
            blueprint=bp,
            spec=spec,
            run_id=run_id,
        )
        assert res_sql.ok is False
        assert any(
            t.dept_id == "cart"
            and t.error_class == "CONTRACT_MISMATCH"
            and "V250" in t.summary
            for t in res_sql.conflict_tickets
        )
    finally:
        engine.dispose()
        for sub in ("cache", "run-fp06", "case-conflict", "case-tamper", "case-sql"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)


def test_negative_max_integration_rounds_cap_preserves_artifacts_without_freezing_openapi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, run_id, spec, bp, run_dir, base_dir = _setup_5_dept_backend(
        tmp_path, monkeypatch
    )
    try:
        store = FactoryStateStore(factory)
        deliverables: dict[str, DepartmentDeliverable] = {}
        for mod in bp.modules:
            st = _materialized_dept_staging(base_dir, run_dir, mod.module_id)
            store.register_department(run_id, FactoryPhase.BACKEND, mod.module_id, staging_dir=str(st))
            store.transition_department(run_id, FactoryPhase.BACKEND, mod.module_id, DeptStatus.PLANNING, current_round=1)
            store.transition_department(run_id, FactoryPhase.BACKEND, mod.module_id, DeptStatus.CODING)
            store.transition_department(run_id, FactoryPhase.BACKEND, mod.module_id, DeptStatus.TESTING)
            store.transition_department(run_id, FactoryPhase.BACKEND, mod.module_id, DeptStatus.PASSED)
            deliverables[mod.module_id] = DepartmentDeliverable(
                run_id=run_id,
                phase="backend",
                dept_id=mod.module_id,
                status="PASSED",
                rounds_used=1,
                staging_dir=str(st),
            )

        async def _noop_rework(
            rid: int,
            module: Module,
            _cur_bp: ArchitectureBlueprint,
            _ver: ModuleVerifier,
            _ticket: DefectTicket | None,
        ) -> DepartmentDeliverable:
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.PLANNING, current_round=2)
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.CODING)
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.TESTING)
            store.transition_department(rid, FactoryPhase.BACKEND, module.module_id, DeptStatus.PASSED)
            return deliverables[module.module_id]

        triage_json = json.dumps(
            {
                "defects": [
                    {
                        "dept_id": "order",
                        "error_class": "INTEGRATION_HTTP_FAIL",
                        "summary": "POST /api/orders returned 500",
                    }
                ]
            }
        )
        chief = ChiefArchitect(
            session_factory=factory,
            client=_ScriptedChiefClient([triage_json]),
            base_package=BASE_PKG,
        )
        pool = AsyncDepartmentPool(
            session_factory=factory,
            verifier=_DummyDeptVerifier(),
            dept_executor=_noop_rework,
        )
        always_fail_runner = _FakeDockerRunner(
            verify_fn=lambda _dir: {
                "overall": "FAIL",
                "checks": [
                    {"name": "static_manifest", "status": "PASS"},
                    {"name": "backend_test", "status": "FAIL", "detail": "OrderIntegrationTest failed"},
                    {"name": "backend_build", "status": "PASS"},
                ],
            }
        )

        res = asyncio.run(
            run_backend_factory_stage(
                run_id=run_id,
                blueprint=bp,
                spec=spec,
                run_dir=run_dir,
                base_dir=base_dir,
                pool=pool,
                chief=chief,
                runner_client=always_fail_runner,
                state_store=store,
                deliverables=deliverables,
                max_integration_rounds=2,
            )
        )
        assert res.ok is False
        assert res.status == "FAILED"
        assert res.integration_rounds_used == 2
        assert res.rework_rounds == 1
        assert res.frozen_openapi_path is None
        assert res.report_path is not None and Path(res.report_path).is_file()
        # 候选 openapi.json 保留但不标记为冻结
        candidate_oa = json.loads(
            (Path(res.assembled_dir) / AUTHORITATIVE_OPENAPI_REL).read_text(encoding="utf-8")
        )
        assert candidate_oa["x-agentforge-frozen"] is False
    finally:
        engine.dispose()
        for sub in ("cache", "run-fp06"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)
