"""FP04：部门三角色最小职责引擎（DeptLead + DeptCoder + DeptTester）与 DefectTicket 测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agentforge_platform.asset_cache import AssetMaterializer, remove_readonly_tree
from agentforge_platform.asset_registry import REPO_ROOT, default_registry
from agentforge_platform.asset_store_pg import PgAssetStore
from agentforge_platform.blueprint_contracts import (
    ArchitectureBlueprint,
    DtoContract,
    DtoField,
)
from agentforge_platform.budget_pg import PgBudgetLedger
from agentforge_platform.clients import Completion, Usage
from agentforge_platform.db import make_engine, make_session_factory, session_scope
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.factory_base_builder import (
    build_backend_base,
    load_base_manifest,
    verify_base_integrity,
)
from agentforge_platform.factory_department import (
    API_FRAGMENT_FILENAME,
    DEPT_CODER_ROLE,
    DEPT_LEAD_ROLE,
    DEPT_TESTER_ROLE,
    CoderFileBackend,
    DepartmentRunner,
    TesterFileBackend,
    _audit_engine_tools,
)
from agentforge_platform.factory_state import DeptStatus, FactoryPhase, FactoryStateStore
from agentforge_platform.factory_tickets import MAX_DEFECT_LOG_BYTES, DefectTicket
from agentforge_platform.interface_registry import (
    KIND_JAVA_METHOD,
    STATUS_APPROVED_FOR_BUILD,
    PublicCapability,
)
from agentforge_platform.models import AssetRelease, Project
from agentforge_platform.module_registry import Module
from agentforge_platform.module_verifier import ModuleVerificationResult
from agentforge_platform.patch_engine import (
    ASSET_READONLY_TOOLS,
    FORBIDDEN_TOOL_NAMES,
    RESTRICTED_TOOLS,
    LedgerBinding,
)
from agentforge_platform.resolver import resolve_assets
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import content_hash

PLATFORM_API_DIR = Path(__file__).resolve().parents[1]
CAMPUS_SPEC_PATH = REPO_ROOT / "schemas" / "campus-activity.spec.json"
BASE_PKG = "com.agentforge.template"
PKG_PATH = "com/agentforge/template"


class _ScriptedLeadClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
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
            model=model,
            request_id=f"req-lead-{len(self.prompts)}",
            usage=Usage(input_tokens=600, output_tokens=300),
            finish_reason="stop",
        )


class _ScriptedToolModel(BaseChatModel):
    """脚本化工具调用模型：真实驱动 DeepAgent ToolNode（零外部网络请求）。"""

    responses: list[AIMessage]
    model_name: str = "qwen3-coder-flash"

    @property
    def _llm_type(self) -> str:
        return "scripted_fp04"

    def _get_ls_params(self, **kwargs: Any) -> dict[str, Any]:
        return {"ls_provider": "openai", "ls_model_name": self.model_name}

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedToolModel:
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any):
        msg = self.responses.pop(0) if self.responses else AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=msg)])


class _ScriptedModuleVerifier:
    verifier_kind = "local_process"

    def __init__(
        self,
        outcomes: list[tuple[str, int | None, str | None, str | None]],
    ) -> None:
        # 每项: (status, exit_code, error_code, log_excerpt)
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def verify_module(
        self,
        *,
        artifact_dir: str | Path,
        module_id: str,
        stack: str = "java",
        test_selector: str = "*Test",
        timeout_seconds: int = 120,
    ) -> ModuleVerificationResult:
        idx = min(len(self.calls), len(self._outcomes) - 1)
        status, exit_code, error_code, log_excerpt = self._outcomes[idx]
        self.calls.append(
            {
                "artifact_dir": str(artifact_dir),
                "module_id": module_id,
                "stack": stack,
                "test_selector": test_selector,
            }
        )
        return ModuleVerificationResult(
            artifact_dir=str(artifact_dir),
            module_id=module_id,
            stack="java" if stack == "java" else "frontend",
            test_selector=test_selector,
            status=status,  # type: ignore[arg-type]
            verifier_kind="local_process",
            error_code=error_code,
            exit_code=exit_code,
            duration_ms=42,
            command=["mvn", "-B", "test", f"-Dtest={test_selector}"],
            detail="scripted verifier",
            log_excerpt=log_excerpt,
        )


def _alembic_config(url: str) -> Config:
    config = Config(str(PLATFORM_API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_API_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def _configure_custom_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = {
        "version": "fp04-test-v1",
        "currency": "CNY",
        "roles": {
            role: {
                "provider": "openai_compatible",
                "model": f"test-{role}",
                "base_url": "https://api.example.test/v1",
                "api_key_env": "TEST_FP04_KEY",
                "enabled": True,
                "max_output_tokens": 4096,
                "supports_tools": True,
                "price_micro": {
                    "input_micro_per_million": 1_000_000,
                    "output_micro_per_million": 2_000_000,
                },
            }
            for role in (DEPT_LEAD_ROLE, DEPT_CODER_ROLE, DEPT_TESTER_ROLE)
        },
    }
    path = tmp_path / "models.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("AGENTFORGE_MODELS_CONFIG", str(path))


def _setup_order_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_custom_models(tmp_path, monkeypatch)
    url = f"sqlite+pysqlite:///{tmp_path / 'fp04.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)

    with session_scope(factory) as db:
        PgAssetStore(db).sync_from_filesystem()
        project = Project(owner_id=1, name="ecommerce-order", requirement_text="订单模块")
        db.add(project)
        db.flush()
        run = create_run(db, project, spec_id=1, spec_hash="e" * 64)
        project_id, run_id = project.id, run.id

    base_spec = json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8"))
    base_spec["goal"] = "电商订单模块：消费商品与购物车 Facade 创建订单"
    base_spec["entities"] = [
        {
            "name": "Product",
            "source": "user_confirmed",
            "fields": [{"name": "id", "type": "long", "required": True}],
        },
        {
            "name": "PurchaseOrder",
            "source": "user_confirmed",
            "fields": [{"name": "id", "type": "long", "required": True}],
        },
    ]
    base_spec["pages"] = [
        {"name": "ProductPage", "entity": "Product"},
        {"name": "OrderPage", "entity": "PurchaseOrder"},
    ]
    spec = SoftwareSpecification.model_validate(base_spec)
    spec_hash = content_hash(spec)
    profile_ref = f"{spec.technical_profile.profile_id}@{spec.technical_profile.profile_version}"

    product_mod = Module(
        project_id=str(project_id),
        module_id="product",
        name="商品部",
        based_on_spec_hash=spec_hash,
        profile_ref=profile_ref,
        owned_paths=[
            f"backend/src/main/java/{PKG_PATH}/modules/product/internal/",
            "backend/src/main/resources/db/migration/V200__product.sql",
        ],
        test_paths=[f"backend/src/test/java/{PKG_PATH}/modules/product/"],
        owned_package=f"{BASE_PKG}.modules.product",
        migration_range=(200, 299),
    )
    order_mod = Module(
        project_id=str(project_id),
        module_id="order",
        name="订单履约部",
        based_on_spec_hash=spec_hash,
        profile_ref=profile_ref,
        owned_paths=[
            f"backend/src/main/java/{PKG_PATH}/modules/order/internal/",
            "backend/src/main/resources/db/migration/V400__purchase_order.sql",
        ],
        test_paths=[f"backend/src/test/java/{PKG_PATH}/modules/order/"],
        owned_package=f"{BASE_PKG}.modules.order",
        migration_range=(400, 499),
        depends_on_modules=["product"],
        readable_contract_refs=["product.get_product_snapshot"],
    )

    blueprint = ArchitectureBlueprint(
        project_id=str(project_id),
        based_on_spec_hash=spec_hash,
        profile_ref=profile_ref,
        base_package=BASE_PKG,
        modules=[product_mod, order_mod],
        shared_dtos=[
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
                name="OrderPayableDto",
                owner_module_id="order",
                fields=[
                    DtoField(name="orderId", java_type="Long"),
                    DtoField(name="totalAmount", java_type="BigDecimal"),
                ],
            ),
        ],
        frozen_contracts=[
            PublicCapability(
                capability_id="product.get_product_snapshot",
                owner_module_id="product",
                interface_kind=KIND_JAVA_METHOD,
                symbol="ProductFacade.getProductSnapshot",
                signature="ProductSnapshotDto getProductSnapshot(Long productId)",
                status=STATUS_APPROVED_FOR_BUILD,
                version=1,
            ),
            PublicCapability(
                capability_id="order.get_order_payable",
                owner_module_id="order",
                interface_kind=KIND_JAVA_METHOD,
                symbol="OrderFacade.getOrderPayable",
                signature="OrderPayableDto getOrderPayable(Long orderId)",
                status=STATUS_APPROVED_FOR_BUILD,
                version=1,
            ),
        ],
        covered_entities={"product": ["Product"], "order": ["PurchaseOrder"]},
        covered_acceptance_ids={"order": [ac.id for ac in spec.acceptance_cases]},
    )

    run_dir = tmp_path / "run-order"
    lock = resolve_assets(default_registry(), "demo-ts-mp")
    sess = factory()
    try:
        mat = AssetMaterializer(sess, cache_root=tmp_path / "cache")
        base_result = build_backend_base(run_dir, blueprint, lock, mat)
    finally:
        sess.close()

    return engine, factory, run_id, spec, blueprint, order_mod, run_dir, base_result


def test_order_department_first_round_pass_with_asset_search_and_deliverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, run_id, spec, blueprint, order_mod, run_dir, base_result = (
        _setup_order_fixture(tmp_path, monkeypatch)
    )
    try:
        with session_scope(factory) as db:
            asset_count_before = db.query(AssetRelease).count()

        lead_plan_json = json.dumps(
            {
                "dept_id": "order",
                "phase": "backend",
                "round": 1,
                "summary": "实现订单服务、建表 SQL 与 OpenAPI 片段",
                "files_to_create": [
                    {
                        "path": f"backend/src/main/java/{PKG_PATH}/modules/order/internal/OrderService.java",
                        "purpose": "订单领域服务",
                    },
                    {
                        "path": "backend/src/main/resources/db/migration/V400__purchase_order.sql",
                        "purpose": "订单建表迁移",
                    },
                ],
                "consumed_contracts": ["product.get_product_snapshot"],
                "exposed_endpoints": ["POST /api/orders"],
                "test_focus": ["Mockito mock ProductFacade 验证下单总额计算"],
            },
            ensure_ascii=False,
        )
        lead_client = _ScriptedLeadClient([lead_plan_json])

        service_path = f"/backend/src/main/java/{PKG_PATH}/modules/order/internal/OrderService.java"
        sql_path = "/backend/src/main/resources/db/migration/V400__purchase_order.sql"
        test_path = f"/backend/src/test/java/{PKG_PATH}/modules/order/OrderServiceTest.java"

        def _coder_factory(_binding: LedgerBinding, _round: int) -> BaseChatModel:
            return _ScriptedToolModel(
                responses=[
                    # 编码子 Agent 自主查 PG 资产库，再写出业务源码、SQL 与 api-fragment.json
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "tc-search",
                                "name": "search_assets",
                                "args": {"query": "crud", "limit": 3},
                            },
                            {
                                "id": "tc-svc",
                                "name": "write_file",
                                "args": {
                                    "file_path": service_path,
                                    "content": (
                                        f"package {BASE_PKG}.modules.order.internal;\n"
                                        "public class OrderService { public int calc(int qty, int price) { return qty * price; } }\n"
                                    ),
                                },
                            },
                            {
                                "id": "tc-sql",
                                "name": "write_file",
                                "args": {
                                    "file_path": sql_path,
                                    "content": "CREATE TABLE purchase_order (id BIGINT PRIMARY KEY);\n",
                                },
                            },
                            {
                                "id": "tc-frag",
                                "name": "write_file",
                                "args": {
                                    "file_path": f"/{API_FRAGMENT_FILENAME}",
                                    "content": json.dumps(
                                        {"paths": {"/api/orders": {"post": {"summary": "创建订单"}}}},
                                        ensure_ascii=False,
                                    ),
                                },
                            },
                        ],
                    ),
                    AIMessage(content="order 部门编码已完成"),
                ]
            )

        def _tester_factory(_binding: LedgerBinding, _round: int) -> BaseChatModel:
            return _ScriptedToolModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "tc-test",
                                "name": "write_file",
                                "args": {
                                    "file_path": test_path,
                                    "content": (
                                        f"package {BASE_PKG}.modules.order;\n"
                                        "// Uses Mockito to mock ProductFacade\n"
                                        "class OrderServiceTest {}\n"
                                    ),
                                },
                            }
                        ],
                    ),
                    AIMessage(content="order 部门单测已编写"),
                ]
            )

        verifier = _ScriptedModuleVerifier([("PASS", 0, None, "Tests run: 3, Failures: 0")])
        runner = DepartmentRunner(
            session_factory=factory,
            verifier=verifier,
            lead_client=lead_client,
            coder_model_factory=_coder_factory,
            tester_model_factory=_tester_factory,
        )

        deliverable = runner.run_department(
            run_id,
            order_mod,
            blueprint=blueprint,
            spec=spec,
            run_dir=run_dir,
            base_dir=base_result.base_dir,
            phase=FactoryPhase.BACKEND,
        )

        assert deliverable.status == "PASSED"
        assert deliverable.rounds_used == 1
        assert deliverable.api_fragment_path == API_FRAGMENT_FILENAME
        assert "/api/orders" in deliverable.api_fragment["paths"]
        assert deliverable.evidence is not None and deliverable.evidence.passed is True
        assert deliverable.evidence.exit_code == 0
        assert len(deliverable.coder_changes) == 3
        assert len(deliverable.tester_changes) == 1
        assert len(deliverable.change_set["changes"]) == 4
        assert len(verifier.calls) == 1
        assert verifier.calls[0]["test_selector"] == "OrderServiceTest"

        # 数据库状态与部门维度账本验证
        store = FactoryStateStore(factory)
        dept_row = store.get_department(run_id, FactoryPhase.BACKEND, "order")
        assert dept_row is not None
        assert dept_row.status == DeptStatus.PASSED.value
        assert dept_row.api_fragment_json is not None
        assert dept_row.evidence_json is not None

        cost = PgBudgetLedger(factory).cost_by_department(run_id)
        assert cost["by_phase"]["backend"]["departments"]["order"]["calls"] == 1

        # 确认模型写出的代码绝对不会自动写入公共资产表 asset_release
        with session_scope(factory) as db:
            assert db.query(AssetRelease).count() == asset_count_before

        # 原只读底座目录零改动
        assert verify_base_integrity(base_result.base_dir, base_result.manifest_path) == []
    finally:
        engine.dispose()
        for sub in ("cache", "run-order"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)


def test_order_department_round_1_fail_rework_with_redacted_ticket_and_round_2_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, run_id, spec, blueprint, order_mod, run_dir, base_result = (
        _setup_order_fixture(tmp_path, monkeypatch)
    )
    try:
        plan_json = json.dumps(
            {
                "dept_id": "order",
                "phase": "backend",
                "round": 1,
                "summary": "首次实现订单服务",
                "test_focus": ["测试订单金额计算"],
            },
            ensure_ascii=False,
        )
        fix_json = json.dumps(
            {
                "dept_id": "order",
                "phase": "backend",
                "round": 2,
                "root_cause": "OrderService.calc 误用加法而非乘法",
                "target_files": [
                    f"backend/src/main/java/{PKG_PATH}/modules/order/internal/OrderService.java"
                ],
                "instructions": ["将 qty + price 修正为 qty * price"],
                "do_not_touch": [f"backend/src/main/java/{PKG_PATH}/modules/product/api/ProductFacade.java"],
            },
            ensure_ascii=False,
        )
        lead_client = _ScriptedLeadClient([plan_json, fix_json])

        service_path = f"/backend/src/main/java/{PKG_PATH}/modules/order/internal/OrderService.java"
        test_path = f"/backend/src/test/java/{PKG_PATH}/modules/order/OrderServiceTest.java"

        def _coder_factory(_binding: LedgerBinding, round_num: int) -> BaseChatModel:
            if round_num == 1:
                return _ScriptedToolModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "id": "r1-svc",
                                    "name": "write_file",
                                    "args": {
                                        "file_path": service_path,
                                        "content": (
                                            f"package {BASE_PKG}.modules.order.internal;\n"
                                            "public class OrderService { public int calc(int q, int p) { return q + p; } }\n"
                                        ),
                                    },
                                }
                            ],
                        ),
                        AIMessage(content="round 1 done"),
                    ]
                )
            return _ScriptedToolModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "r2-fix",
                                "name": "edit_file",
                                "args": {
                                    "file_path": service_path,
                                    "old_string": "return q + p;",
                                    "new_string": "return q * p;",
                                },
                            }
                        ],
                    ),
                    AIMessage(content="round 2 fixed"),
                ]
            )

        def _tester_factory(_binding: LedgerBinding, round_num: int) -> BaseChatModel:
            if round_num == 1:
                return _ScriptedToolModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "id": "r1-test",
                                    "name": "write_file",
                                    "args": {
                                        "file_path": test_path,
                                        "content": f"package {BASE_PKG}.modules.order;\nclass OrderServiceTest {{}}\n",
                                    },
                                }
                            ],
                        ),
                        AIMessage(content="test written"),
                    ]
                )
            return _ScriptedToolModel(responses=[AIMessage(content="reuse existing unit tests")])

        # 故意构造 > 6 KB 且含疑似密钥/DB密码的失败日志，验证脱敏与 <= 4 KB 截断
        noisy_secret_log = (
            "OrderServiceTest.calcFailed: expected 200 but was 30\n"
            "api_key=sk-secretvalue1234567890abcdef\n"
            "db=postgresql+psycopg://admin:SuperSecretPass@127.0.0.1:15432/agentforge\n"
            + ("stack line detail\n" * 400)
        )
        verifier = _ScriptedModuleVerifier(
            [
                ("FAIL", 1, "BUILD_FAILED", noisy_secret_log),
                ("PASS", 0, None, "Tests run: 3, Failures: 0"),
            ]
        )
        runner = DepartmentRunner(
            session_factory=factory,
            verifier=verifier,
            lead_client=lead_client,
            coder_model_factory=_coder_factory,
            tester_model_factory=_tester_factory,
        )

        deliverable = runner.run_department(
            run_id,
            order_mod,
            blueprint=blueprint,
            spec=spec,
            run_dir=run_dir,
            base_dir=base_result.base_dir,
            phase=FactoryPhase.BACKEND,
            max_rounds=3,
        )

        assert deliverable.status == "PASSED"
        assert deliverable.rounds_used == 2
        assert len(deliverable.fix_directives) == 1
        assert len(deliverable.defect_tickets) == 1
        assert len(lead_client.prompts) == 2

        # 核验 DefectTicket 与传给 DeptLead 第 2 轮的日志已脱敏且 <= 4 KB
        ticket = deliverable.defect_tickets[0]
        assert len(ticket.log_excerpt.encode("utf-8")) <= MAX_DEFECT_LOG_BYTES
        assert "sk-secretvalue1234567890abcdef" not in ticket.log_excerpt
        assert "SuperSecretPass" not in ticket.log_excerpt
        assert "sk-secretvalue1234567890abcdef" not in lead_client.prompts[1]
        assert "SuperSecretPass" not in lead_client.prompts[1]
        assert "OrderServiceTest" in lead_client.prompts[1]

        # 源码已局部修复且 DB 缺陷单已标 resolved=True
        fixed_src = (Path(deliverable.staging_dir) / service_path.lstrip("/")).read_text(encoding="utf-8")
        assert "return q * p;" in fixed_src
        store = FactoryStateStore(factory)
        assert store.list_defect_tickets(run_id, dept_id="order", resolved=False) == []
        assert len(store.list_defect_tickets(run_id, dept_id="order", resolved=True)) == 1
    finally:
        engine.dispose()
        for sub in ("cache", "run-order"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)


def test_negative_role_isolation_coder_and_tester_403_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _factory, _run_id, _spec, _blueprint, order_mod, _run_dir, base_result = (
        _setup_order_fixture(tmp_path, monkeypatch)
    )
    try:
        manifest = load_base_manifest(base_result.manifest_path)
        staging = tmp_path / "dept-staging"
        staging.mkdir()

        coder_fb = CoderFileBackend(
            staging,
            owned_paths=list(order_mod.owned_paths),
            test_paths=list(order_mod.test_paths),
            base_manifest=manifest,
        )
        tester_fb = TesterFileBackend(
            staging,
            test_paths=list(order_mod.test_paths),
            owned_paths=list(order_mod.owned_paths),
            base_manifest=manifest,
        )

        # DeptCoder 负例：改 test_paths、改他部门目录、改 api/*Facade.java、改底座 pom.xml/security → 全报 403 FORBIDDEN
        for bad_coder_path in (
            f"backend/src/test/java/{PKG_PATH}/modules/order/OrderServiceTest.java",
            f"backend/src/main/java/{PKG_PATH}/modules/product/internal/ProductService.java",
            f"backend/src/main/java/{PKG_PATH}/modules/product/api/ProductFacade.java",
            f"backend/src/main/java/{PKG_PATH}/modules/order/api/OrderFacade.java",
            "backend/pom.xml",
            f"backend/src/main/java/{PKG_PATH}/security/JwtService.java",
        ):
            with pytest.raises(AppException) as exc:
                coder_fb.write(bad_coder_path, b"// forbidden")
            assert exc.value.code == ErrorCode.FORBIDDEN
            assert exc.value.status_code == 403

        # DeptCoder 正例：本部门 owned_paths 与 api-fragment.json 允许写入
        coder_fb.write(
            f"backend/src/main/java/{PKG_PATH}/modules/order/internal/OrderService.java",
            b"class OrderService {}\n",
        )
        coder_fb.write(API_FRAGMENT_FILENAME, b"{}\n")

        # DeptTester 负例：改业务源码、改 api-fragment.json、改底座固定测试、改他部门测试、改 Facade → 全报 403 FORBIDDEN
        for bad_tester_path in (
            f"backend/src/main/java/{PKG_PATH}/modules/order/internal/OrderService.java",
            "backend/src/main/resources/db/migration/V400__purchase_order.sql",
            API_FRAGMENT_FILENAME,
            f"backend/src/test/java/{PKG_PATH}/HealthTest.java",
            f"backend/src/test/java/{PKG_PATH}/modules/product/ProductTest.java",
            f"backend/src/main/java/{PKG_PATH}/modules/order/api/OrderFacade.java",
        ):
            with pytest.raises(AppException) as exc:
                tester_fb.write(bad_tester_path, b"// forbidden")
            assert exc.value.code == ErrorCode.FORBIDDEN
            assert exc.value.status_code == 403

        # DeptTester 正例：本部门 test_paths 允许写入
        tester_fb.write(
            f"backend/src/test/java/{PKG_PATH}/modules/order/OrderServiceTest.java",
            b"class OrderServiceTest {}\n",
        )
    finally:
        engine.dispose()
        for sub in ("cache", "run-order"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)


def test_negative_unexecuted_or_nonzero_verifier_refuses_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, run_id, spec, blueprint, order_mod, run_dir, base_result = (
        _setup_order_fixture(tmp_path, monkeypatch)
    )
    try:
        plan_json = json.dumps(
            {"dept_id": "order", "phase": "backend", "round": 1, "summary": "plan"},
            ensure_ascii=False,
        )

        # 场景 A：测试工具返回 NOT_RUN（未真正执行）→ 严禁标 PASSED，落 FAILED
        verifier_not_run = _ScriptedModuleVerifier([("NOT_RUN", None, "RUNNER_MISSING", "未执行")])
        runner_a = DepartmentRunner(
            session_factory=factory,
            verifier=verifier_not_run,
            lead_client=_ScriptedLeadClient([plan_json]),
            coder_model_factory=lambda _b, _r: _ScriptedToolModel(responses=[AIMessage(content="ok")]),
            tester_model_factory=lambda _b, _r: _ScriptedToolModel(responses=[AIMessage(content="ok")]),
        )
        res_a = runner_a.run_department(
            run_id,
            order_mod,
            blueprint=blueprint,
            spec=spec,
            run_dir=run_dir,
            base_dir=base_result.base_dir,
            max_rounds=1,
        )
        assert res_a.status == "FAILED"
        assert res_a.evidence is not None and res_a.evidence.passed is False
        assert FactoryStateStore(factory).get_department(run_id, FactoryPhase.BACKEND, "order").status == "FAILED"

        # 场景 B：测试工具返回非零退出码（即使 status 伪报 PASS）→ 严禁标 PASSED
        with session_scope(factory) as db:
            project = db.query(Project).first()
            assert project is not None
            run2 = create_run(db, project, spec_id=1, spec_hash="e" * 64)
            run_id_2 = run2.id

        verifier_nonzero = _ScriptedModuleVerifier([("PASS", 1, None, "exit 1")])
        runner_b = DepartmentRunner(
            session_factory=factory,
            verifier=verifier_nonzero,
            lead_client=_ScriptedLeadClient([plan_json]),
            coder_model_factory=lambda _b, _r: _ScriptedToolModel(responses=[AIMessage(content="ok")]),
            tester_model_factory=lambda _b, _r: _ScriptedToolModel(responses=[AIMessage(content="ok")]),
        )
        res_b = runner_b.run_department(
            run_id_2,
            order_mod,
            blueprint=blueprint,
            spec=spec,
            run_dir=tmp_path / "run-order-2",
            base_dir=base_result.base_dir,
            max_rounds=1,
        )
        assert res_b.status == "FAILED"
        assert res_b.evidence is not None and res_b.evidence.passed is False
    finally:
        engine.dispose()
        for sub in ("cache", "run-order", "run-order-2"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)


def test_tool_table_audit_blocks_execute_and_task_for_coder_and_tester(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, run_id, _spec, _blueprint, order_mod, _run_dir, base_result = (
        _setup_order_fixture(tmp_path, monkeypatch)
    )
    try:
        manifest = load_base_manifest(base_result.manifest_path)
        staging = tmp_path / "audit-staging"
        staging.mkdir()

        runner = DepartmentRunner(
            session_factory=factory,
            verifier=_ScriptedModuleVerifier([("PASS", 0, None, "ok")]),
            coder_model_factory=lambda _b, _r: _ScriptedToolModel(responses=[]),
            tester_model_factory=lambda _b, _r: _ScriptedToolModel(responses=[]),
        )
        coder_fb = CoderFileBackend(
            staging,
            owned_paths=list(order_mod.owned_paths),
            test_paths=list(order_mod.test_paths),
            base_manifest=manifest,
        )
        tester_fb = TesterFileBackend(
            staging,
            test_paths=list(order_mod.test_paths),
            owned_paths=list(order_mod.owned_paths),
            base_manifest=manifest,
        )

        coder_engine = runner._build_coder_engine(
            project_id=1,
            run_id=run_id,
            phase=FactoryPhase.BACKEND,
            dept_id="order",
            round_num=1,
            coder_fb=coder_fb,
        )
        tester_engine = runner._build_tester_engine(
            project_id=1,
            run_id=run_id,
            phase=FactoryPhase.BACKEND,
            dept_id="order",
            round_num=1,
            tester_fb=tester_fb,
        )

        assert coder_engine.tool_names <= set(RESTRICTED_TOOLS) | set(ASSET_READONLY_TOOLS)
        assert not (coder_engine.tool_names & FORBIDDEN_TOOL_NAMES)
        assert set(ASSET_READONLY_TOOLS) <= coder_engine.tool_names

        assert tester_engine.tool_names <= set(RESTRICTED_TOOLS)
        assert not (tester_engine.tool_names & FORBIDDEN_TOOL_NAMES)
        assert not (tester_engine.tool_names & set(ASSET_READONLY_TOOLS))

        # 伪造暴露 execute / task 工具的 Agent → 前置审计直接抛 403 FORBIDDEN
        for bad_tool in ("execute", "task"):
            fake_agent = SimpleNamespace(
                nodes={"tools": SimpleNamespace(bound=SimpleNamespace(tools_by_name={"ls": object(), bad_tool: object()}))}
            )
            fake_engine = SimpleNamespace(agent=fake_agent)
            with pytest.raises(AppException) as exc:
                _audit_engine_tools(fake_engine, allow_asset_tools=False)  # type: ignore[arg-type]
            assert exc.value.code == ErrorCode.FORBIDDEN

        # DefectTicket 脱敏与 4 KB 上限单测
        t = DefectTicket(
            ticket_id="t1",
            run_id=run_id,
            phase="backend",
            dept_id="order",
            round=1,
            error_class="UNIT_TEST_FAILURE",
            summary="fail",
            log_excerpt="token=ghp_12345678901234567890 " + ("中" * 3000),
        )
        assert len(t.log_excerpt.encode("utf-8")) <= MAX_DEFECT_LOG_BYTES
        assert "ghp_12345678901234567890" not in t.log_excerpt
    finally:
        engine.dispose()
        for sub in ("cache", "run-order"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)
