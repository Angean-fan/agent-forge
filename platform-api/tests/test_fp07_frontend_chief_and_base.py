"""FP07：前端项目总管、只读前端底座与基于权威 openapi.json 的 TS 契约类型生成测试。"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
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
from agentforge_platform.asset_registry import REPO_ROOT
from agentforge_platform.asset_store_pg import PgAssetStore
from agentforge_platform.blueprint_contracts import ArchitectureBlueprint
from agentforge_platform.budget_pg import PgBudgetLedger
from agentforge_platform.clients import Completion, Usage
from agentforge_platform.db import make_engine, make_session_factory, session_scope
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.factory_base_builder import assert_base_integrity, verify_base_integrity
from agentforge_platform.factory_chief import CHIEF_DEPT_ID, CHIEF_ROLE, ChiefArchitect
from agentforge_platform.factory_frontend_base import (
    GENERATED_ENDPOINTS_REL,
    GENERATED_ROUTES_REL,
    GENERATED_TYPES_REL,
    build_frontend_base,
    canonical_openapi_sha256,
    generate_ts_api_endpoints,
    generate_ts_api_types,
    validate_frontend_blueprint,
)
from agentforge_platform.factory_state import (
    BlueprintStatus,
    DeptStatus,
    FactoryPhase,
    FactoryStateStore,
)
from agentforge_platform.interface_registry import (
    KIND_HTTP,
    STATUS_APPROVED_FOR_BUILD,
    PublicCapability,
)
from agentforge_platform.models import Project
from agentforge_platform.module_registry import Module
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import content_hash

PLATFORM_API_DIR = Path(__file__).resolve().parents[1]
CAMPUS_SPEC_PATH = REPO_ROOT / "schemas" / "campus-activity.spec.json"


def _alembic_config(url: str) -> Config:
    config = Config(str(PLATFORM_API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_API_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def _configure_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = {
        "version": "fp07-test-v1",
        "currency": "CNY",
        "roles": {
            CHIEF_ROLE: {
                "provider": "openai_compatible",
                "model": "test-fe-chief",
                "base_url": "https://api.example.test/v1",
                "api_key_env": "TEST_FP07_KEY",
                "enabled": True,
                "max_output_tokens": 8192,
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


class _ScriptedClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, *, model: str, prompt: str, **kwargs: Any) -> Completion:
        self.prompts.append(prompt)
        idx = min(len(self.prompts) - 1, len(self._responses) - 1)
        return Completion(
            content=self._responses[idx],
            model=model,
            request_id=f"req-fe-chief-{len(self.prompts)}",
            usage=Usage(input_tokens=800, output_tokens=400),
            finish_reason="stop",
        )


def _make_ecommerce_spec() -> SoftwareSpecification:
    base = json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8"))
    base["goal"] = "电商沙箱 5 部门前端：登录注册、首页展示、购物车、订单与模拟支付"
    entities = ["UserAccount", "Product", "CartItem", "PurchaseOrder", "PaymentRecord"]
    base["entities"] = [
        {
            "name": name,
            "source": "user_confirmed",
            "fields": [{"name": "id", "type": "long", "required": True}],
        }
        for name in entities
    ]
    base["pages"] = [
        {"name": "LoginPage", "entity": "UserAccount", "path": "/auth"},
        {"name": "ProductHomePage", "entity": "Product", "path": "/products"},
        {"name": "CartPage", "entity": "CartItem", "path": "/cart"},
        {"name": "OrderCheckoutPage", "entity": "PurchaseOrder", "path": "/orders"},
        {"name": "SandboxPayPage", "entity": "PaymentRecord", "path": "/payments"},
    ]
    base["integrations"] = []
    base["capability_requests"] = []
    return SoftwareSpecification.model_validate(base)


def _make_authoritative_ecommerce_openapi(*, frozen: bool = True) -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "AgentForge E-Commerce Sandbox Backend",
            "version": "1.0.1",
        },
        "x-agentforge-blueprint-version": 1,
        "x-agentforge-blueprint-hash": "a" * 64,
        "x-agentforge-frozen": frozen,
        "paths": {
            "/api/health": {
                "get": {
                    "operationId": "getHealth",
                    "x-department": "base",
                    "x-auth": ["PUBLIC"],
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"status": {"type": "string"}},
                                        "required": ["status"],
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/auth/register": {
                "post": {
                    "operationId": "registerUser",
                    "x-department": "auth",
                    "x-auth": ["PUBLIC"],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/RegisterRequest"}
                            }
                        }
                    },
                    "responses": {
                        "201": {
                            "description": "Created",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/UserSummaryDto"}
                                }
                            },
                        }
                    },
                }
            },
            "/api/products": {
                "get": {
                    "operationId": "listProducts",
                    "x-department": "product",
                    "x-auth": ["PUBLIC"],
                    "parameters": [
                        {
                            "name": "keyword",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "page",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer"},
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/ProductDto"},
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/products/{productId}": {
                "get": {
                    "operationId": "getProductById",
                    "x-department": "product",
                    "x-auth": ["PUBLIC"],
                    "parameters": [
                        {
                            "name": "productId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ProductDto"}
                                }
                            },
                        }
                    },
                }
            },
            "/api/cart/items": {
                "post": {
                    "operationId": "addCartItem",
                    "x-department": "cart",
                    "x-auth": ["AUTHENTICATED"],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/AddCartItemRequest"}
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/CartItemDto"}
                                }
                            },
                        }
                    },
                }
            },
            "/api/orders": {
                "post": {
                    "operationId": "createOrder",
                    "x-department": "order",
                    "x-auth": ["AUTHENTICATED"],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CreateOrderRequest"}
                            }
                        }
                    },
                    "responses": {
                        "201": {
                            "description": "Created",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/OrderDto"}
                                }
                            },
                        }
                    },
                }
            },
            "/api/payments/sandbox": {
                "post": {
                    "operationId": "executeSandboxPay",
                    "x-department": "payment",
                    "x-auth": ["AUTHENTICATED"],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SandboxPayRequest"}
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/PaymentReceiptDto"}
                                }
                            },
                        }
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "RegisterRequest": {
                    "type": "object",
                    "properties": {
                        "username": {"type": "string"},
                        "password": {"type": "string"},
                    },
                    "required": ["username", "password"],
                },
                "UserSummaryDto": {
                    "type": "object",
                    "properties": {
                        "userId": {"type": "integer"},
                        "username": {"type": "string"},
                        "role": {"type": "string", "enum": ["USER", "ADMIN"]},
                    },
                    "required": ["userId", "username", "role"],
                },
                "ProductDto": {
                    "type": "object",
                    "properties": {
                        "productId": {"type": "integer"},
                        "title": {"type": "string"},
                        "unitPrice": {"type": "number"},
                        "stock": {"type": "integer"},
                        "description": {"type": "string", "nullable": True},
                    },
                    "required": ["productId", "title", "unitPrice", "stock"],
                },
                "AddCartItemRequest": {
                    "type": "object",
                    "properties": {
                        "productId": {"type": "integer"},
                        "quantity": {"type": "integer"},
                    },
                    "required": ["productId", "quantity"],
                },
                "CartItemDto": {
                    "type": "object",
                    "properties": {
                        "itemId": {"type": "integer"},
                        "product": {"$ref": "#/components/schemas/ProductDto"},
                        "quantity": {"type": "integer"},
                    },
                    "required": ["itemId", "product", "quantity"],
                },
                "CreateOrderRequest": {
                    "type": "object",
                    "properties": {
                        "cartItemIds": {"type": "array", "items": {"type": "integer"}},
                    },
                    "required": ["cartItemIds"],
                },
                "OrderDto": {
                    "type": "object",
                    "properties": {
                        "orderId": {"type": "integer"},
                        "totalAmount": {"type": "number"},
                        "status": {"type": "string"},
                    },
                    "required": ["orderId", "totalAmount", "status"],
                },
                "SandboxPayRequest": {
                    "type": "object",
                    "properties": {
                        "orderId": {"type": "integer"},
                        "amount": {"type": "number"},
                    },
                    "required": ["orderId", "amount"],
                },
                "PaymentReceiptDto": {
                    "type": "object",
                    "properties": {
                        "paymentId": {"type": "integer"},
                        "orderId": {"type": "integer"},
                        "sandboxMode": {"type": "boolean"},
                    },
                    "required": ["paymentId", "orderId", "sandboxMode"],
                },
            }
        },
    }


def _make_5_dept_frontend_blueprint(
    spec: SoftwareSpecification,
    *,
    project_id: int,
) -> ArchitectureBlueprint:
    spec_hash = content_hash(spec)
    profile_ref = f"{spec.technical_profile.profile_id}@{spec.technical_profile.profile_version}"
    pid = str(project_id)

    depts = [
        ("auth-ui", "登录注册前端部", "/auth", "/api/auth/register", "POST", "LoginPage"),
        ("home-ui", "首页商品前端部", "/products", "/api/products", "GET", "ProductHomePage"),
        ("cart-ui", "购物车前端部", "/cart", "/api/cart/items", "POST", "CartPage"),
        ("order-ui", "订单结算前端部", "/orders", "/api/orders", "POST", "OrderCheckoutPage"),
        ("payment-ui", "模拟支付前端部", "/payments", "/api/payments/sandbox", "POST", "SandboxPayPage"),
    ]

    modules = [
        Module(
            project_id=pid,
            module_id=dept_id,
            name=name,
            based_on_spec_hash=spec_hash,
            profile_ref=profile_ref,
            owned_paths=[f"frontend/src/modules/{dept_id}/"],
            test_paths=[f"frontend/test/{dept_id}/"],
        )
        for dept_id, name, *_ in depts
    ]

    contracts: list[PublicCapability] = []
    for dept_id, _name, route_prefix, api_path, api_method, _page in depts:
        cap_prefix = dept_id.replace("-", "_")
        contracts.append(
            PublicCapability(
                capability_id=f"{cap_prefix}.route_mount",
                owner_module_id=dept_id,
                interface_kind=KIND_HTTP,
                http_method="GET",
                http_path=route_prefix,
                signature=f"ROUTE {route_prefix}",
                status=STATUS_APPROVED_FOR_BUILD,
                version=1,
            )
        )
        contracts.append(
            PublicCapability(
                capability_id=f"{cap_prefix}.backend_api",
                owner_module_id=dept_id,
                interface_kind=KIND_HTTP,
                http_method=api_method,
                http_path=api_path,
                signature=f"{api_method} {api_path}",
                status=STATUS_APPROVED_FOR_BUILD,
                version=1,
            )
        )

    return ArchitectureBlueprint(
        phase="frontend",
        project_id=pid,
        based_on_spec_hash=spec_hash,
        profile_ref=profile_ref,
        modules=modules,
        frozen_contracts=contracts,
        covered_pages={dept_id: [page] for dept_id, *_, page in depts},
        sandbox_integrations=[
            {
                "id": "payment-sandbox",
                "kind": "payment",
                "sandbox_only": True,
                "mode": "sandbox_mock",
                "pattern": "Port+Adapter+Mock",
                "real_payment_allowed": False,
            }
        ],
    )


def _setup_db_with_passed_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, backend_passed: bool = True):
    _configure_models(tmp_path, monkeypatch)
    url = f"sqlite+pysqlite:///{tmp_path / 'fp07.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)

    with session_scope(factory) as db:
        PgAssetStore(db).sync_from_filesystem()
        project = Project(owner_id=1, name="ecommerce-fe", requirement_text="电商前端")
        db.add(project)
        db.flush()
        run = create_run(db, project, spec_id=1, spec_hash="d" * 64)
        project_id, run_id = project.id, run.id

    store = FactoryStateStore(factory)
    for dept in ("auth", "product", "cart", "order", "payment"):
        store.register_department(run_id, FactoryPhase.BACKEND, dept)
        store.transition_department(run_id, FactoryPhase.BACKEND, dept, DeptStatus.PLANNING, current_round=1)
        store.transition_department(run_id, FactoryPhase.BACKEND, dept, DeptStatus.CODING)
        if backend_passed or dept != "payment":
            store.transition_department(run_id, FactoryPhase.BACKEND, dept, DeptStatus.TESTING)
            store.transition_department(run_id, FactoryPhase.BACKEND, dept, DeptStatus.PASSED)

    return engine, factory, project_id, run_id, store


def test_generate_ts_api_types_and_endpoints_from_ecommerce_openapi() -> None:
    openapi_doc = _make_authoritative_ecommerce_openapi(frozen=True)

    types_ts_1 = generate_ts_api_types(openapi_doc)
    types_ts_2 = generate_ts_api_types(openapi_doc)
    endpoints_ts_1 = generate_ts_api_endpoints(openapi_doc)
    endpoints_ts_2 = generate_ts_api_endpoints(openapi_doc)

    # 100% 确定性输出
    assert types_ts_1 == types_ts_2
    assert endpoints_ts_1 == endpoints_ts_2

    # 覆盖全部 DTO、枚举、可空字段、路径参数、查询参数与请求/响应类型
    for symbol in (
        "export interface RegisterRequest",
        "export interface UserSummaryDto",
        '"USER" | "ADMIN"',
        "export interface ProductDto",
        "description?: (string) | null;",
        "export interface CartItemDto",
        "product: ProductDto;",
        "export interface OrderDto",
        "export interface PaymentReceiptDto",
        "export interface GetProductByIdPathParams",
        "export interface ListProductsQueryParams",
        "export interface CreateOrderRequest",
        "request: CreateOrderRequest;",
        "export type CreateOrderResponse = OrderDto;",
        "export interface ApiOperationContracts",
    ):
        assert symbol in types_ts_1

    # 覆盖全部端点常量与类型化请求函数
    for fn_sig in (
        "export const API_ENDPOINTS =",
        "export function registerUser(",
        "export function listProducts(",
        "export function getProductById(",
        "export function addCartItem(",
        "export function createOrder(",
        "export function executeSandboxPay(",
        "frozen: true,",
    ):
        assert fn_sig in endpoints_ts_1


def test_frontend_chief_plans_5_dept_blueprint_and_builds_readonly_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, project_id, run_id, store = _setup_db_with_passed_backend(
        tmp_path, monkeypatch, backend_passed=True
    )
    try:
        spec = _make_ecommerce_spec()
        openapi_doc = _make_authoritative_ecommerce_openapi(frozen=True)
        oa_sha = canonical_openapi_sha256(openapi_doc)
        expected_fe_bp = _make_5_dept_frontend_blueprint(spec, project_id=project_id)

        client = _ScriptedClient([expected_fe_bp.model_dump_json()])
        chief = ChiefArchitect(session_factory=factory, client=client)
        frozen_fe = chief.plan_frontend_blueprint(
            spec,
            openapi_doc,
            spec.technical_profile,
            run_id,
            openapi_sha256=oa_sha,
        )

        assert validate_frontend_blueprint(frozen_fe, spec, openapi_doc) == []
        assert [m.module_id for m in frozen_fe.modules] == [
            "auth-ui",
            "home-ui",
            "cart-ui",
            "order-ui",
            "payment-ui",
        ]

        # 前端蓝图持久化为 APPROVED_FOR_BUILD，费用记在 phase=frontend, dept_id=chief
        saved = store.get_latest_blueprint(run_id, FactoryPhase.FRONTEND, approved_only=True)
        assert saved is not None
        assert saved.status == BlueprintStatus.APPROVED_FOR_BUILD.value
        assert saved.blueprint_hash == frozen_fe.blueprint_hash()

        cost = PgBudgetLedger(factory).cost_by_department(run_id)
        assert cost["by_phase"]["frontend"]["departments"][CHIEF_DEPT_ID]["calls"] == 1

        # 按 FP07 4 参签名构建只读前端底座
        sess = factory()
        try:
            materializer = AssetMaterializer(sess, cache_root=tmp_path / "cache")
            res = build_frontend_base(
                tmp_path / "run-fe",
                frozen_fe,
                openapi_doc,
                materializer,
            )
        finally:
            sess.close()

        base_dir = Path(res.base_dir)
        assert (base_dir / "frontend/package.json").is_file()
        assert (base_dir / "frontend/package-lock.json").is_file()
        assert (base_dir / "frontend/src/core/http.ts").is_file()
        assert (base_dir / "frontend/src/core/layout.ts").is_file()
        assert (base_dir / "frontend/src/core/styles.css").is_file()
        assert (base_dir / GENERATED_ROUTES_REL).is_file()
        assert (base_dir / GENERATED_TYPES_REL).is_file()
        assert (base_dir / GENERATED_ENDPOINTS_REL).is_file()

        # 受保护路径标记与 OS 只读权限校验
        for must_protect in (
            "frontend/src/generated/",
            "frontend/src/core/",
            "frontend/package.json",
            "frontend/package-lock.json",
        ):
            assert must_protect in res.protected_prefixes
        assert not ((base_dir / GENERATED_TYPES_REL).stat().st_mode & stat.S_IWUSR)
        assert not ((base_dir / "frontend/package.json").stat().st_mode & stat.S_IWUSR)

        # 完整性校验与篡改拦截
        assert verify_base_integrity(base_dir, res.manifest_path) == []
        copy_dir = tmp_path / "fe-tamper-check"
        shutil.copytree(base_dir, copy_dir)
        make_writable_tree(copy_dir)
        assert_base_integrity(copy_dir, res.manifest_path)
        (copy_dir / GENERATED_TYPES_REL).write_text("// tampered\n", encoding="utf-8")
        with pytest.raises(AppException) as exc:
            assert_base_integrity(copy_dir, res.manifest_path)
        assert exc.value.code == ErrorCode.CONTRACT_CONFLICT
    finally:
        engine.dispose()
        for sub in ("cache", "run-fe", "fe-tamper-check"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)


def test_negative_frontend_refuses_start_without_passed_backend_and_frozen_openapi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. 后端存在未 PASSED 的部门 → 拒绝启动前端总管（0 模型调用）
    engine, factory, project_id, run_id, store = _setup_db_with_passed_backend(
        tmp_path, monkeypatch, backend_passed=False
    )
    try:
        spec = _make_ecommerce_spec()
        openapi_doc = _make_authoritative_ecommerce_openapi(frozen=True)
        fe_bp = _make_5_dept_frontend_blueprint(spec, project_id=project_id)
        unused_client = _ScriptedClient(["{}"])
        chief = ChiefArchitect(session_factory=factory, client=unused_client)

        with pytest.raises(AppException) as exc_be:
            chief.plan_frontend_blueprint(spec, openapi_doc, spec.technical_profile, run_id)
        assert exc_be.value.code == ErrorCode.VALIDATION_ERROR
        assert len(unused_client.prompts) == 0

        # 补齐 payment 为 PASSED，继续验证未冻结 openapi 与篡改哈希拦截
        store.transition_department(run_id, FactoryPhase.BACKEND, "payment", DeptStatus.TESTING)
        store.transition_department(run_id, FactoryPhase.BACKEND, "payment", DeptStatus.PASSED)

        # 2. openapi.json 未冻结（x-agentforge-frozen=False）→ 拒绝
        unfrozen_oa = _make_authoritative_ecommerce_openapi(frozen=False)
        with pytest.raises(AppException) as exc_unfrozen:
            chief.plan_frontend_blueprint(spec, unfrozen_oa, spec.technical_profile, run_id)
        assert exc_unfrozen.value.code == ErrorCode.OPENAPI_REJECTED
        assert len(unused_client.prompts) == 0

        # 3. openapi_sha256 不匹配 → 拒绝
        with pytest.raises(AppException) as exc_hash:
            chief.plan_frontend_blueprint(
                spec,
                openapi_doc,
                spec.technical_profile,
                run_id,
                openapi_sha256="0" * 64,
            )
        assert exc_hash.value.code == ErrorCode.ASSET_CHECKSUM_MISMATCH
        assert len(unused_client.prompts) == 0

        # 4. build_frontend_base 同样拒绝未冻结 openapi
        sess = factory()
        try:
            mat = AssetMaterializer(sess, cache_root=tmp_path / "cache")
            with pytest.raises(AppException) as exc_base:
                build_frontend_base(tmp_path / "run-bad", fe_bp, unfrozen_oa, mat)
            assert exc_base.value.code == ErrorCode.OPENAPI_REJECTED
        finally:
            sess.close()
    finally:
        engine.dispose()
        if (tmp_path / "cache").exists():
            remove_readonly_tree(tmp_path / "cache")


def test_negative_frontend_blueprint_hard_validation_rules() -> None:
    spec = _make_ecommerce_spec()
    openapi_doc = _make_authoritative_ecommerce_openapi(frozen=True)
    good_bp = _make_5_dept_frontend_blueprint(spec, project_id=1)
    assert validate_frontend_blueprint(good_bp, spec, openapi_doc) == []

    # 1. 路由前缀冲突（home-ui 与 cart-ui 同占 /cart）
    bad_contracts = list(good_bp.frozen_contracts)
    bad_contracts[2] = bad_contracts[2].model_copy(update={"http_path": "/cart"})
    issues_route = validate_frontend_blueprint(
        good_bp.model_copy(update={"frozen_contracts": bad_contracts}),
        spec,
        openapi_doc,
    )
    assert any("路由前缀冲突" in i for i in issues_route)

    # 2. 企图将只读受保护路径 src/generated/api-types.ts 纳入 owned_paths
    bad_mods = list(good_bp.modules)
    bad_mods[0] = bad_mods[0].model_copy(
        update={"owned_paths": ["frontend/src/modules/auth-ui/", GENERATED_TYPES_REL]}
    )
    issues_prot = validate_frontend_blueprint(
        good_bp.model_copy(update={"modules": bad_mods}),
        spec,
        openapi_doc,
    )
    assert any("受保护底座/契约路径" in i for i in issues_prot)

    # 3. 漏覆盖业务页面 SandboxPayPage
    bad_pages = dict(good_bp.covered_pages)
    del bad_pages["payment-ui"]
    issues_pages = validate_frontend_blueprint(
        good_bp.model_copy(update={"covered_pages": bad_pages}),
        spec,
        openapi_doc,
    )
    assert any("SandboxPayPage" in i for i in issues_pages)

    # 4. 前端契约引用权威 openapi.json 中不存在的后端路径 /api/nonexistent
    ghost_contracts = list(good_bp.frozen_contracts)
    ghost_contracts[1] = ghost_contracts[1].model_copy(update={"http_path": "/api/nonexistent"})
    issues_oa = validate_frontend_blueprint(
        good_bp.model_copy(update={"frozen_contracts": ghost_contracts}),
        spec,
        openapi_doc,
    )
    assert any("/api/nonexistent" in i for i in issues_oa)


def test_node_type_check_passes_on_generated_readonly_frontend_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本机存在 Node/npm 时，在生成的只读前端底座副本上真实执行 ``npm run type-check``（无 Node 标 NOT_RUN）。"""
    npm = shutil.which("npm")
    node = shutil.which("node")
    if not npm or not node:
        pytest.skip("NOT_RUN: 本机未安装 Node 或 npm，跳过前端只读底座 npm run type-check 验证")

    engine, factory, project_id, _run_id, _store = _setup_db_with_passed_backend(
        tmp_path, monkeypatch, backend_passed=True
    )
    try:
        spec = _make_ecommerce_spec()
        openapi_doc = _make_authoritative_ecommerce_openapi(frozen=True)
        fe_bp = _make_5_dept_frontend_blueprint(spec, project_id=project_id)

        sess = factory()
        try:
            mat = AssetMaterializer(sess, cache_root=tmp_path / "cache")
            res = build_frontend_base(tmp_path / "run-tc", fe_bp, openapi_doc, mat)
        finally:
            sess.close()

        # 复制前端底座到可写目录以便安装 node_modules 并跑 vue-tsc --noEmit（原只读底座零改动）
        fe_work = tmp_path / "fe-typecheck-work"
        shutil.copytree(res.frontend_project_dir, fe_work)
        make_writable_tree(fe_work)

        # 优先离线 npm ci --offline（利用本机 npm 缓存），缺缓存再回退 npm ci
        install = subprocess.run(
            [npm, "ci", "--offline", "--no-audit", "--no-fund"],
            cwd=fe_work,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if install.returncode != 0:
            install = subprocess.run(
                [npm, "ci", "--no-audit", "--no-fund"],
                cwd=fe_work,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if install.returncode != 0:
                pytest.skip(f"NOT_RUN: npm ci 无法安装前端依赖（{install.stderr[-300:]}）")

        tc = subprocess.run(
            [npm, "run", "type-check"],
            cwd=fe_work,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert tc.returncode == 0, f"vue-tsc --noEmit 失败：\n{tc.stdout}\n{tc.stderr}"
        # 原只读底座完整性保持 100% 不变
        assert verify_base_integrity(res.base_dir, res.manifest_path) == []
    finally:
        engine.dispose()
        for sub in ("cache", "run-tc", "fe-typecheck-work"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)
