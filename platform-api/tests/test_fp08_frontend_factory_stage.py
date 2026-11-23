"""FP08：前端多部门异步并行流水线、声明式路由自动聚合与前端总装大循环测试。

覆盖 WhenDone 全部要求：
1. 5 个前端部门（``auth-ui / catalog-ui / cart-ui / order-ui / payment-ui``）异步并行开发各自页面、
   ``routes.ts`` 与 Vitest 单测，自动聚合生成 ``frontend/src/router/generated-routes.ts`` 且无路由冲突；
2. 故意制造 TypeScript 类型不匹配（``catalog-ui`` 引用后端权威 ``openapi.json`` 中不存在的字段
   ``product.nonExistentGhostField``）→ 真实 ``npx vue-tsc --noEmit`` 失败 → 生成脱敏
   ``DefectTicket`` 打回 ``catalog-ui`` 修复并通过 → 全量前端 ``vue-tsc`` + ``vitest`` 通过；
3. ``aggregate_frontend_routes`` 确定性与路由路径冲突（跨模块重复路径 / 冲突底座保留路由 ``/login``）拦截；
4. 总装前确定性契约与安全门禁：假端点 ``/api/ghost/*``、未知 ``API_ENDPOINTS.*``、XSS ``v-html``、
   硬编码密钥、受保护路由漏写 ``requiresAuth: true``、未真实执行测试（``NOT_RUN``）一律拒绝放行；
5. 前端总装大循环：总管归因定向打回责任部门（其余 4 部门绝不重跑）与 ``INFRASTRUCTURE`` 零返工即停。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import select

from agentforge_platform.asset_cache import (
    AssetMaterializer,
    make_writable_tree,
    remove_readonly_tree,
)
from agentforge_platform.asset_registry import REPO_ROOT
from agentforge_platform.asset_store_pg import PgAssetStore
from agentforge_platform.blueprint_contracts import ArchitectureBlueprint
from agentforge_platform.clients import Completion, Usage
from agentforge_platform.db import make_engine, make_session_factory, session_scope
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.factory_base_builder import verify_base_integrity
from agentforge_platform.factory_chief import ChiefArchitect
from agentforge_platform.factory_department import DepartmentRunner
from agentforge_platform.factory_frontend_base import (
    build_frontend_base,
    canonical_openapi_sha256,
)
from agentforge_platform.factory_frontend_stage import (
    FRONTEND_INTEGRATION_REPORT_NAME,
    aggregate_frontend_routes,
    merge_frontend_deliverables,
    run_frontend_factory_stage,
)
from agentforge_platform.factory_scheduler import AsyncDepartmentPool
from agentforge_platform.factory_state import DeptStatus, FactoryPhase, FactoryStateStore
from agentforge_platform.factory_tickets import DepartmentDeliverable, TestEvidence
from agentforge_platform.interface_registry import (
    KIND_HTTP,
    STATUS_APPROVED_FOR_BUILD,
    PublicCapability,
)
from agentforge_platform.models import DefectTicketRow, DepartmentRun, ModelCall, Project
from agentforge_platform.module_registry import Module
from agentforge_platform.module_verifier import (
    LOCAL_DEPT_TEST_RUNNER_ENV,
    LocalProcessModuleVerifier,
    ModuleVerificationResult,
)
from agentforge_platform.patch_engine import LedgerBinding
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import content_hash

PLATFORM_API_DIR = Path(__file__).resolve().parents[1]
CAMPUS_SPEC_PATH = REPO_ROOT / "schemas" / "campus-activity.spec.json"

FIVE_FE_DEPTS = ("auth-ui", "catalog-ui", "cart-ui", "order-ui", "payment-ui")


def _alembic_config(url: str) -> Config:
    config = Config(str(PLATFORM_API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_API_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def _configure_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = {
        "version": "fp08-test-v1",
        "currency": "CNY",
        "roles": {
            role: {
                "provider": "openai_compatible",
                "model": f"test-{role}",
                "base_url": "https://api.fake-llm.example/v1",
                "api_key_env": "FAKE_FP08_KEY",
                "enabled": True,
                "max_output_tokens": 4096,
                "supports_tools": role in ("dept_coder", "dept_tester"),
                "price_micro": {
                    "input_micro_per_million": 1_000_000,
                    "output_micro_per_million": 2_000_000,
                },
            }
            for role in ("chief_architect", "dept_lead", "dept_coder", "dept_tester")
        },
    }
    p = tmp_path / "models.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("AGENTFORGE_MODELS_CONFIG", str(p))


def _make_ecommerce_spec() -> SoftwareSpecification:
    base = json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8"))
    base["goal"] = "电商沙箱 5 部门前端：登录注册、商品展示、购物车、订单与模拟支付"
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
        {"name": "AuthPage", "entity": "UserAccount", "path": "/auth"},
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
                        }
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
            "/api/cart/items": {
                "post": {
                    "operationId": "addCartItem",
                    "x-department": "cart",
                    "x-auth": ["USER"],
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
                    "x-auth": ["USER"],
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
            "/api/payments/sandbox-pay": {
                "post": {
                    "operationId": "executeSandboxPay",
                    "x-department": "payment",
                    "x-auth": ["USER"],
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
                    "required": ["username", "password"],
                    "properties": {
                        "username": {"type": "string"},
                        "password": {"type": "string"},
                    },
                },
                "UserSummaryDto": {
                    "type": "object",
                    "required": ["id", "username", "role"],
                    "properties": {
                        "id": {"type": "integer", "format": "int64"},
                        "username": {"type": "string"},
                        "role": {"type": "string", "enum": ["USER", "ADMIN"]},
                    },
                },
                "ProductDto": {
                    "type": "object",
                    "required": ["id", "title", "priceCents", "stock"],
                    "properties": {
                        "id": {"type": "integer", "format": "int64"},
                        "title": {"type": "string"},
                        "priceCents": {"type": "integer", "format": "int64"},
                        "stock": {"type": "integer"},
                    },
                },
                "AddCartItemRequest": {
                    "type": "object",
                    "required": ["productId", "quantity"],
                    "properties": {
                        "productId": {"type": "integer", "format": "int64"},
                        "quantity": {"type": "integer"},
                    },
                },
                "CartItemDto": {
                    "type": "object",
                    "required": ["id", "productId", "quantity", "subtotalCents"],
                    "properties": {
                        "id": {"type": "integer", "format": "int64"},
                        "productId": {"type": "integer", "format": "int64"},
                        "quantity": {"type": "integer"},
                        "subtotalCents": {"type": "integer", "format": "int64"},
                    },
                },
                "CreateOrderRequest": {
                    "type": "object",
                    "required": ["cartItemIds"],
                    "properties": {
                        "cartItemIds": {"type": "array", "items": {"type": "integer"}},
                    },
                },
                "OrderDto": {
                    "type": "object",
                    "required": ["id", "orderNo", "totalCents", "status"],
                    "properties": {
                        "id": {"type": "integer", "format": "int64"},
                        "orderNo": {"type": "string"},
                        "totalCents": {"type": "integer", "format": "int64"},
                        "status": {"type": "string", "enum": ["PENDING", "PAID", "CANCELLED"]},
                    },
                },
                "SandboxPayRequest": {
                    "type": "object",
                    "required": ["orderNo"],
                    "properties": {"orderNo": {"type": "string"}},
                },
                "PaymentReceiptDto": {
                    "type": "object",
                    "required": ["receiptNo", "orderNo", "sandboxMode", "paid"],
                    "properties": {
                        "receiptNo": {"type": "string"},
                        "orderNo": {"type": "string"},
                        "sandboxMode": {"type": "boolean"},
                        "paid": {"type": "boolean"},
                    },
                },
            }
        },
    }


def _make_5_dept_frontend_blueprint(
    spec: SoftwareSpecification,
    *,
    project_id: int = 1,
) -> ArchitectureBlueprint:
    spec_hash = content_hash(spec)
    profile_ref = f"{spec.technical_profile.profile_id}@{spec.technical_profile.profile_version}"
    pid = str(project_id)
    depts = [
        ("auth-ui", "前端认证模块", "/auth", "/api/auth/register", "POST", "AuthPage"),
        ("catalog-ui", "前端商品模块", "/products", "/api/products", "GET", "ProductHomePage"),
        ("cart-ui", "前端购物车模块", "/cart", "/api/cart/items", "POST", "CartPage"),
        ("order-ui", "前端订单模块", "/orders", "/api/orders", "POST", "OrderCheckoutPage"),
        ("payment-ui", "前端沙箱支付模块", "/payments", "/api/payments/sandbox-pay", "POST", "SandboxPayPage"),
    ]
    modules: list[Module] = []
    contracts: list[PublicCapability] = []
    for dept_id, name, route_prefix, api_path, method, _page in depts:
        cap_prefix = dept_id.replace("-", "_")
        modules.append(
            Module(
                project_id=pid,
                module_id=dept_id,
                name=name,
                based_on_spec_hash=spec_hash,
                profile_ref=profile_ref,
                owned_paths=[f"frontend/src/modules/{dept_id}/"],
                test_paths=[f"frontend/test/{dept_id}/"],
                readable_contract_refs=[f"{cap_prefix}.route_mount", f"{cap_prefix}.backend_api"],
            )
        )
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
                http_method=method,
                http_path=api_path,
                signature=f"{method} {api_path}",
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


def _setup_db_with_passed_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_models(tmp_path, monkeypatch)
    url = f"sqlite+pysqlite:///{tmp_path / 'fp08.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)

    with session_scope(factory) as db:
        PgAssetStore(db).sync_from_filesystem()
        project = Project(owner_id=1, name="ecommerce-fe-fp08", requirement_text="电商前端流水线")
        db.add(project)
        db.flush()
        run = create_run(db, project, spec_id=1, spec_hash="e" * 64)
        project_id, run_id = project.id, run.id

    store = FactoryStateStore(factory)
    for dept in ("auth", "product", "cart", "order", "payment"):
        store.register_department(run_id, FactoryPhase.BACKEND, dept)
        store.transition_department(run_id, FactoryPhase.BACKEND, dept, DeptStatus.PLANNING, current_round=1)
        store.transition_department(run_id, FactoryPhase.BACKEND, dept, DeptStatus.CODING)
        store.transition_department(run_id, FactoryPhase.BACKEND, dept, DeptStatus.TESTING)
        store.transition_department(run_id, FactoryPhase.BACKEND, dept, DeptStatus.PASSED)

    return engine, factory, project_id, run_id, store


class _ScriptedTextClient:
    def __init__(self, resolver) -> None:
        self._resolver = resolver
        self.prompts: list[str] = []

    def complete(self, *, model: str, prompt: str, **kwargs: Any) -> Completion:
        self.prompts.append(prompt)
        content = self._resolver(prompt, len(self.prompts))
        return Completion(
            content=content,
            model=model,
            request_id=f"req-fp08-{len(self.prompts)}",
            usage=Usage(input_tokens=400, output_tokens=200),
            finish_reason="stop",
        )


class _ScriptedToolModel(BaseChatModel):
    responses: list[AIMessage]
    model_name: str = "qwen3-coder-fp08"

    @property
    def _llm_type(self) -> str:
        return "scripted_fp08"

    def _get_ls_params(self, **kwargs: Any) -> dict[str, Any]:
        return {"ls_provider": "openai", "ls_model_name": self.model_name}

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedToolModel:
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any):
        msg = self.responses.pop(0) if self.responses else AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _write_calls(file_map: dict[str, str], *, prefix: str) -> list[AIMessage]:
    tool_calls = [
        {
            "name": "write_file",
            "args": {"file_path": rel, "content": content},
            "id": f"call-{prefix}-{idx}",
            "type": "tool_call",
        }
        for idx, (rel, content) in enumerate(file_map.items(), start=1)
    ]
    return [
        AIMessage(content="", tool_calls=tool_calls),
        AIMessage(content="done"),
    ]


def _dept_valid_frontend_files(dept_id: str, *, catalog_type_bug: bool = False) -> tuple[dict[str, str], dict[str, str]]:
    """构造真实可编译、可通过 vue-tsc --noEmit 与 vitest run 的前端业务代码与单测。"""
    if dept_id == "auth-ui":
        code = {
            "frontend/src/modules/auth-ui/service.ts": (
                "import { API_ENDPOINTS, registerUser } from '../../generated/api-endpoints';\n"
                "import type { RegisterRequest, UserSummaryDto } from '../../generated/api-types';\n\n"
                "export const AUTH_REGISTER_ENDPOINT = API_ENDPOINTS.registerUser.path;\n\n"
                "export async function submitRegister(payload: RegisterRequest): Promise<UserSummaryDto> {\n"
                "  return registerUser(payload);\n"
                "}\n"
            ),
            "frontend/src/modules/auth-ui/AuthView.vue": (
                "<script setup lang=\"ts\">\n"
                "import { ref } from 'vue';\n"
                "import { submitRegister } from './service';\n\n"
                "const username = ref('');\n"
                "const password = ref('');\n"
                "async function onSubmit(): Promise<void> {\n"
                "  await submitRegister({ username: username.value, password: password.value });\n"
                "}\n"
                "</script>\n\n"
                "<template>\n"
                "  <section class=\"auth-view\">\n"
                "    <h2>账号注册</h2>\n"
                "    <button type=\"button\" @click=\"onSubmit\">注册</button>\n"
                "  </section>\n"
                "</template>\n"
            ),
            "frontend/src/modules/auth-ui/routes.ts": (
                "import type { RouteRecordRaw } from 'vue-router';\n\n"
                "const authRoutes: RouteRecordRaw[] = [\n"
                "  { path: '/auth', name: 'auth-register', component: () => import('./AuthView.vue'), meta: { requiresAuth: false } },\n"
                "];\n\n"
                "export default authRoutes;\n"
            ),
        }
        tests = {
            "frontend/test/auth-ui/auth.spec.ts": (
                "import { describe, expect, it } from 'vitest';\n"
                "import { AUTH_REGISTER_ENDPOINT } from '../../src/modules/auth-ui/service';\n"
                "import authRoutes from '../../src/modules/auth-ui/routes';\n\n"
                "describe('auth-ui', () => {\n"
                "  it('binds authoritative register endpoint and /auth route', () => {\n"
                "    expect(AUTH_REGISTER_ENDPOINT).toBe('/api/auth/register');\n"
                "    expect(authRoutes[0]?.path).toBe('/auth');\n"
                "  });\n"
                "});\n"
            )
        }
        return code, tests

    if dept_id == "catalog-ui":
        price_expr = "product.nonExistentGhostField" if catalog_type_bug else "product.priceCents"
        code = {
            "frontend/src/modules/catalog-ui/service.ts": (
                "import { API_ENDPOINTS, listProducts } from '../../generated/api-endpoints';\n"
                "import type { ProductDto } from '../../generated/api-types';\n\n"
                "export const CATALOG_ENDPOINT = API_ENDPOINTS.listProducts.path;\n\n"
                "export function formatProductPriceYuan(product: ProductDto): string {\n"
                f"  return `¥${{({price_expr} / 100).toFixed(2)}}`;\n"
                "}\n\n"
                "export async function fetchCatalog(keyword?: string): Promise<ProductDto[]> {\n"
                "  return listProducts(keyword ? { keyword } : undefined);\n"
                "}\n"
            ),
            "frontend/src/modules/catalog-ui/CatalogView.vue": (
                "<script setup lang=\"ts\">\n"
                "import { ref } from 'vue';\n"
                "import type { ProductDto } from '../../generated/api-types';\n"
                "import { formatProductPriceYuan } from './service';\n\n"
                "const items = ref<ProductDto[]>([]);\n"
                "</script>\n\n"
                "<template>\n"
                "  <section class=\"catalog-view\">\n"
                "    <article v-for=\"item in items\" :key=\"item.id\">\n"
                "      <h3>{{ item.title }}</h3>\n"
                "      <span>{{ formatProductPriceYuan(item) }}</span>\n"
                "    </article>\n"
                "  </section>\n"
                "</template>\n"
            ),
            "frontend/src/modules/catalog-ui/routes.ts": (
                "import type { RouteRecordRaw } from 'vue-router';\n\n"
                "const catalogRoutes: RouteRecordRaw[] = [\n"
                "  { path: '/products', name: 'catalog-home', component: () => import('./CatalogView.vue'), meta: { requiresAuth: false } },\n"
                "];\n\n"
                "export default catalogRoutes;\n"
            ),
        }
        tests = {
            "frontend/test/catalog-ui/catalog.spec.ts": (
                "import { describe, expect, it } from 'vitest';\n"
                "import { CATALOG_ENDPOINT, formatProductPriceYuan } from '../../src/modules/catalog-ui/service';\n"
                "import catalogRoutes from '../../src/modules/catalog-ui/routes';\n\n"
                "describe('catalog-ui', () => {\n"
                "  it('formats product price from ProductDto and declares /products route', () => {\n"
                "    expect(CATALOG_ENDPOINT).toBe('/api/products');\n"
                "    expect(formatProductPriceYuan({ id: 1, title: 'Book', priceCents: 2990, stock: 5 })).toBe('¥29.90');\n"
                "    expect(catalogRoutes[0]?.path).toBe('/products');\n"
                "  });\n"
                "});\n"
            )
        }
        return code, tests

    if dept_id == "cart-ui":
        code = {
            "frontend/src/modules/cart-ui/service.ts": (
                "import { API_ENDPOINTS, addCartItem } from '../../generated/api-endpoints';\n"
                "import type { AddCartItemRequest, CartItemDto } from '../../generated/api-types';\n\n"
                "export const CART_ENDPOINT = API_ENDPOINTS.addCartItem.path;\n\n"
                "export async function appendCartItem(payload: AddCartItemRequest): Promise<CartItemDto> {\n"
                "  return addCartItem(payload);\n"
                "}\n"
            ),
            "frontend/src/modules/cart-ui/CartView.vue": (
                "<script setup lang=\"ts\">\n"
                "import { ref } from 'vue';\n"
                "import type { CartItemDto } from '../../generated/api-types';\n\n"
                "const cartItems = ref<CartItemDto[]>([]);\n"
                "</script>\n\n"
                "<template>\n"
                "  <section class=\"cart-view\">\n"
                "    <p>共 {{ cartItems.length }} 件商品</p>\n"
                "  </section>\n"
                "</template>\n"
            ),
            "frontend/src/modules/cart-ui/routes.ts": (
                "import type { RouteRecordRaw } from 'vue-router';\n\n"
                "const cartRoutes: RouteRecordRaw[] = [\n"
                "  { path: '/cart', name: 'cart-page', component: () => import('./CartView.vue'), meta: { requiresAuth: true } },\n"
                "];\n\n"
                "export default cartRoutes;\n"
            ),
        }
        tests = {
            "frontend/test/cart-ui/cart.spec.ts": (
                "import { describe, expect, it } from 'vitest';\n"
                "import { CART_ENDPOINT } from '../../src/modules/cart-ui/service';\n"
                "import cartRoutes from '../../src/modules/cart-ui/routes';\n\n"
                "describe('cart-ui', () => {\n"
                "  it('protects /cart route and aligns with /api/cart/items', () => {\n"
                "    expect(CART_ENDPOINT).toBe('/api/cart/items');\n"
                "    expect(cartRoutes[0]?.meta?.requiresAuth).toBe(true);\n"
                "  });\n"
                "});\n"
            )
        }
        return code, tests

    if dept_id == "order-ui":
        code = {
            "frontend/src/modules/order-ui/service.ts": (
                "import { API_ENDPOINTS, createOrder } from '../../generated/api-endpoints';\n"
                "import type { CreateOrderRequest, OrderDto } from '../../generated/api-types';\n\n"
                "export const ORDER_ENDPOINT = API_ENDPOINTS.createOrder.path;\n\n"
                "export async function submitOrder(payload: CreateOrderRequest): Promise<OrderDto> {\n"
                "  return createOrder(payload);\n"
                "}\n"
            ),
            "frontend/src/modules/order-ui/OrderView.vue": (
                "<script setup lang=\"ts\">\n"
                "import { ref } from 'vue';\n"
                "import type { OrderDto } from '../../generated/api-types';\n\n"
                "const latestOrder = ref<OrderDto | null>(null);\n"
                "</script>\n\n"
                "<template>\n"
                "  <section class=\"order-view\">\n"
                "    <p v-if=\"latestOrder\">订单号：{{ latestOrder.orderNo }}</p>\n"
                "  </section>\n"
                "</template>\n"
            ),
            "frontend/src/modules/order-ui/routes.ts": (
                "import type { RouteRecordRaw } from 'vue-router';\n\n"
                "const orderRoutes: RouteRecordRaw[] = [\n"
                "  { path: '/orders', name: 'order-checkout', component: () => import('./OrderView.vue'), meta: { requiresAuth: true } },\n"
                "];\n\n"
                "export default orderRoutes;\n"
            ),
        }
        tests = {
            "frontend/test/order-ui/order.spec.ts": (
                "import { describe, expect, it } from 'vitest';\n"
                "import { ORDER_ENDPOINT } from '../../src/modules/order-ui/service';\n"
                "import orderRoutes from '../../src/modules/order-ui/routes';\n\n"
                "describe('order-ui', () => {\n"
                "  it('protects /orders route and aligns with /api/orders', () => {\n"
                "    expect(ORDER_ENDPOINT).toBe('/api/orders');\n"
                "    expect(orderRoutes[0]?.meta?.requiresAuth).toBe(true);\n"
                "  });\n"
                "});\n"
            )
        }
        return code, tests

    # payment-ui
    code = {
        "frontend/src/modules/payment-ui/service.ts": (
            "import { API_ENDPOINTS, executeSandboxPay } from '../../generated/api-endpoints';\n"
            "import type { PaymentReceiptDto, SandboxPayRequest } from '../../generated/api-types';\n\n"
            "export const SANDBOX_PAY_ENDPOINT = API_ENDPOINTS.executeSandboxPay.path;\n\n"
            "export async function triggerSandboxPay(payload: SandboxPayRequest): Promise<PaymentReceiptDto> {\n"
            "  return executeSandboxPay(payload);\n"
            "}\n"
        ),
        "frontend/src/modules/payment-ui/PaymentView.vue": (
            "<script setup lang=\"ts\">\n"
            "import { ref } from 'vue';\n"
            "import type { PaymentReceiptDto } from '../../generated/api-types';\n\n"
            "const receipt = ref<PaymentReceiptDto | null>(null);\n"
            "</script>\n\n"
            "<template>\n"
            "  <section class=\"payment-view\">\n"
            "    <p v-if=\"receipt\">沙箱回执：{{ receipt.receiptNo }}</p>\n"
            "  </section>\n"
            "</template>\n"
        ),
        "frontend/src/modules/payment-ui/routes.ts": (
            "import type { RouteRecordRaw } from 'vue-router';\n\n"
            "const paymentRoutes: RouteRecordRaw[] = [\n"
            "  { path: '/payments', name: 'sandbox-pay', component: () => import('./PaymentView.vue'), meta: { requiresAuth: true } },\n"
            "];\n\n"
            "export default paymentRoutes;\n"
        ),
    }
    tests = {
        "frontend/test/payment-ui/payment.spec.ts": (
            "import { describe, expect, it } from 'vitest';\n"
            "import { SANDBOX_PAY_ENDPOINT } from '../../src/modules/payment-ui/service';\n"
            "import paymentRoutes from '../../src/modules/payment-ui/routes';\n\n"
            "describe('payment-ui', () => {\n"
            "  it('protects /payments route and uses sandbox pay endpoint', () => {\n"
            "    expect(SANDBOX_PAY_ENDPOINT).toBe('/api/payments/sandbox-pay');\n"
            "    expect(paymentRoutes[0]?.meta?.requiresAuth).toBe(true);\n"
            "  });\n"
            "});\n"
        )
    }
    return code, tests


def _prepare_shared_node_modules(base_fe_dir: Path, tmp_path: Path) -> Path:
    """用离线 npm ci 在独立缓存目录安装一次前端依赖，并软链给底座供各部门并发快检复用。"""
    npm = shutil.which("npm")
    node = shutil.which("node")
    if not npm or not node:
        pytest.skip("NOT_RUN: 本机未安装 Node 或 npm，跳过真实 vue-tsc/vitest 前端流水线测试")

    cache_fe = tmp_path / "shared-fe-deps"
    shutil.copytree(base_fe_dir, cache_fe)
    make_writable_tree(cache_fe)

    install = subprocess.run(
        [npm, "ci", "--offline", "--no-audit", "--no-fund"],
        cwd=cache_fe,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if install.returncode != 0:
        install = subprocess.run(
            [npm, "ci", "--no-audit", "--no-fund"],
            cwd=cache_fe,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if install.returncode != 0:
            pytest.skip(f"NOT_RUN: npm ci 无法安装前端依赖（{install.stderr[-300:]}）")

    nm_src = (cache_fe / "node_modules").resolve()
    nm_link = base_fe_dir / "node_modules"
    if not nm_link.exists():
        os.symlink(nm_src, nm_link, target_is_directory=True)
    return cache_fe


def _make_lead_json_resolver():
    def _resolve(prompt: str, _call_idx: int) -> str:
        # 区分首次 DepartmentPlan 与返工 FixDirective
        if "FixDirective" in prompt:
            return json.dumps(
                {
                    "root_cause": "引用了后端 openapi.json ProductDto 中不存在的字段，导致 vue-tsc 报 TS2339",
                    "target_files": ["frontend/src/modules/catalog-ui/service.ts"],
                    "instructions": ["将 product.nonExistentGhostField 更正为 product.priceCents"],
                    "do_not_touch": ["frontend/src/generated/api-types.ts"],
                    "test_adjustments": ["保持 catalog.spec.ts 断言价格格式化结果"],
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "summary": "基于冻结 openapi.json 实现本前端部门页面、路由与契约测试",
                "files_to_create": [],
                "reused_assets": [],
                "consumed_contracts": [],
                "exposed_endpoints": [],
                "test_focus": ["校验权威 API_ENDPOINTS 路径、DTO 字段类型与路由权限守卫"],
            },
            ensure_ascii=False,
        )

    return _resolve


def test_5_frontend_departments_parallel_type_mismatch_rework_and_route_aggregation_real_vue_tsc_vitest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """核心端到端实测（真实 vue-tsc --noEmit + 真实 vitest run）：

    1. 5 个前端部门异步并行开发并经双信号量限流（并发上限 3 / 验证器并发上限 2）；
    2. ``catalog-ui`` 第 1 轮故意引用 ``openapi.json`` 中不存在的字段 ``nonExistentGhostField``
       → 真实 ``vue-tsc`` 报 TS2339 失败 → 持久化 ``DefectTicket`` 打回 ``catalog-ui`` 第 2 轮修复通过；
    3. 确定性自动聚合 5 部门 ``src/modules/*/routes.ts`` 生成 ``generated-routes.ts`` 且无路由冲突；
    4. 聚合后的全量前端再次通过真实 ``npx vue-tsc --noEmit`` + ``npx vitest run test``。
    """
    monkeypatch.setenv(LOCAL_DEPT_TEST_RUNNER_ENV, "1")
    engine, factory, project_id, run_id, store = _setup_db_with_passed_backend(tmp_path, monkeypatch)
    try:
        spec = _make_ecommerce_spec()
        openapi_doc = _make_authoritative_ecommerce_openapi(frozen=True)
        openapi_sha = canonical_openapi_sha256(openapi_doc)
        fe_bp = _make_5_dept_frontend_blueprint(spec, project_id=project_id)

        run_dir = tmp_path / "run-fp08"
        sess = factory()
        try:
            mat = AssetMaterializer(sess, cache_root=tmp_path / "asset-cache")
            fe_base = build_frontend_base(
                run_dir,
                fe_bp,
                openapi_doc,
                mat,
                openapi_sha256=openapi_sha,
                state_store=store,
                run_id=run_id,
            )
        finally:
            sess.close()

        _prepare_shared_node_modules(Path(fe_base.frontend_project_dir), tmp_path)
        # 软链 node_modules 绝不破坏只读底座完整性
        assert verify_base_integrity(fe_base.base_dir, fe_base.manifest_path) == []

        lead_client = _ScriptedTextClient(_make_lead_json_resolver())

        def _coder_factory(binding: LedgerBinding, round_num: int) -> BaseChatModel:
            # catalog-ui 第 1 轮故意注入类型不匹配字段，第 2 轮依据 DefectTicket/FixDirective 修复
            inject_bug = binding.dept_id == "catalog-ui" and round_num == 1
            code_files, _ = _dept_valid_frontend_files(
                binding.dept_id,
                catalog_type_bug=inject_bug,
            )
            return _ScriptedToolModel(
                responses=_write_calls(code_files, prefix=f"c-{binding.dept_id}-r{round_num}")
            )

        def _tester_factory(binding: LedgerBinding, round_num: int) -> BaseChatModel:
            _, test_files = _dept_valid_frontend_files(binding.dept_id)
            return _ScriptedToolModel(
                responses=_write_calls(test_files, prefix=f"t-{binding.dept_id}-r{round_num}")
            )

        local_verifier = LocalProcessModuleVerifier(work_root=run_dir)

        def _runner_factory(_mod: Module, scoped_verifier: Any, _esc_role: str | None) -> DepartmentRunner:
            return DepartmentRunner(
                session_factory=factory,
                state_store=store,
                verifier=scoped_verifier,
                lead_client=lead_client,
                coder_model_factory=_coder_factory,
                tester_model_factory=_tester_factory,
            )

        pool = AsyncDepartmentPool(
            session_factory=factory,
            verifier=local_verifier,
            runner_factory=_runner_factory,
            state_store=store,
            max_concurrent_depts_limit=3,
            max_concurrent_verifiers_limit=2,
            max_rounds=3,
        )
        def _unexpected_triage(prompt: str, _idx: int) -> str:
            raise AssertionError(f"全量前端验证意外失败，触发总管归因：{prompt}")

        chief = ChiefArchitect(
            session_factory=factory,
            state_store=store,
            client=_ScriptedTextClient(_unexpected_triage),
        )

        result = asyncio.run(
            run_frontend_factory_stage(
                run_id=run_id,
                blueprint=fe_bp,
                spec=spec,
                openapi_doc=openapi_doc,
                openapi_sha256=openapi_sha,
                run_dir=run_dir,
                base_dir=fe_base.base_dir,
                pool=pool,
                chief=chief,
                state_store=store,
                module_verifier=local_verifier,
            )
        )

        assert result.ok is True, f"前端流水线应全绿，实际：{result.model_dump()}"
        assert result.status == "PASSED"
        assert result.integration_rounds_used == 1
        assert result.rework_rounds == 0

        # 1. catalog-ui 因真实 vue-tsc 类型错误自动触发 1 次小循环返工并在第 2 轮通过；其余 4 部门均 1 轮通过
        assert result.deliverables["catalog-ui"].rounds_used == 2
        assert len(result.deliverables["catalog-ui"].defect_tickets) == 1
        catalog_ticket = result.deliverables["catalog-ui"].defect_tickets[0]
        assert "nonExistentGhostField" in catalog_ticket.log_excerpt
        assert catalog_ticket.exit_code not in (None, 0)

        for dept_id in ("auth-ui", "cart-ui", "order-ui", "payment-ui"):
            assert result.deliverables[dept_id].rounds_used == 1
            assert result.deliverables[dept_id].evidence is not None
            assert result.deliverables[dept_id].evidence.passed is True

        # 2. 自动聚合 generated-routes.ts 包含全部 5 个部门路由导入，且通过全量 vue-tsc + vitest
        assert result.generated_routes_path is not None
        routes_text = Path(result.generated_routes_path).read_text(encoding="utf-8")
        for mod in FIVE_FE_DEPTS:
            assert f"../modules/{mod}/routes" in routes_text
        assert "export const generatedModuleRoutes: RouteRecordRaw[]" in routes_text

        # 3. 核验报告与数据库状态（所有 5 部门 PASSED、catalog-ui 缺陷单已 RESOLVED、A07 账本有记录）
        assert result.report_path is not None
        report_file = Path(result.report_path)
        assert report_file.name == FRONTEND_INTEGRATION_REPORT_NAME
        report_data = json.loads(report_file.read_text(encoding="utf-8"))
        assert report_data["status"] == "PASSED"
        assert sorted(report_data["route_modules"]) == sorted(FIVE_FE_DEPTS)

        with session_scope(factory) as db:
            fe_rows = list(
                db.scalars(
                    select(DepartmentRun).where(
                        DepartmentRun.run_id == run_id,
                        DepartmentRun.phase == FactoryPhase.FRONTEND.value,
                    )
                )
            )
            assert len(fe_rows) == 5
            assert all(r.status == DeptStatus.PASSED.value for r in fe_rows)

            ticket_rows = list(
                db.scalars(
                    select(DefectTicketRow).where(
                        DefectTicketRow.run_id == run_id,
                        DefectTicketRow.phase == FactoryPhase.FRONTEND.value,
                        DefectTicketRow.dept_id == "catalog-ui",
                    )
                )
            )
            assert len(ticket_rows) == 1
            assert ticket_rows[0].resolved is True

            fe_calls = list(
                db.scalars(
                    select(ModelCall).where(
                        ModelCall.run_id == run_id,
                        ModelCall.phase == FactoryPhase.FRONTEND.value,
                    )
                )
            )
            assert "dept_lead" in {row.model_role for row in fe_calls}
            assert {row.dept_id for row in fe_calls} == set(FIVE_FE_DEPTS)
    finally:
        engine.dispose()
        for sub in ("asset-cache", "run-fp08", "shared-fe-deps"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)


def _create_stub_deliverables(
    base_dir: Path,
    run_dir: Path,
    *,
    overrides: dict[str, dict[str, str]] | None = None,
    not_run_dept: str | None = None,
) -> dict[str, DepartmentDeliverable]:
    """快速构造 5 个前端部门的暂存目录与 Deliverable（供确定性门禁与大循环负例单测使用）。"""
    deliverables: dict[str, DepartmentDeliverable] = {}
    for dept_id in FIVE_FE_DEPTS:
        staging = run_dir / "staging" / "frontend" / dept_id
        shutil.copytree(base_dir / "frontend", staging / "frontend")
        make_writable_tree(staging / "frontend")
        code_files, test_files = _dept_valid_frontend_files(dept_id)
        merged = {**code_files, **test_files}
        if overrides and dept_id in overrides:
            merged.update(overrides[dept_id])
        for rel, content in merged.items():
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        if dept_id == not_run_dept:
            evidence = TestEvidence(
                dept_id=dept_id,
                phase="frontend",
                round=1,
                verifier_kind="local_process",
                stack="frontend",
                test_selector=f"test/{dept_id}",
                status="NOT_RUN",
                executed=False,
                exit_code=None,
            )
        else:
            evidence = TestEvidence(
                dept_id=dept_id,
                phase="frontend",
                round=1,
                verifier_kind="local_process",
                stack="frontend",
                test_selector=f"test/{dept_id}",
                status="PASS",
                executed=True,
                exit_code=0,
            )

        deliverables[dept_id] = DepartmentDeliverable(
            run_id=1,
            phase="frontend",
            dept_id=dept_id,
            status="PASSED",
            rounds_used=1,
            staging_dir=str(staging),
            evidence=evidence,
        )
    return deliverables


def test_aggregate_frontend_routes_deterministic_and_blocks_route_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确定性路由聚合：正常 5 部门两次生成字节一致；跨模块路由冲突与底座保留路由冲突一律拒绝。"""
    engine, factory, project_id, _run_id, _store = _setup_db_with_passed_backend(tmp_path, monkeypatch)
    try:
        spec = _make_ecommerce_spec()
        openapi_doc = _make_authoritative_ecommerce_openapi(frozen=True)
        fe_bp = _make_5_dept_frontend_blueprint(spec, project_id=project_id)

        run_dir = tmp_path / "run-routes"
        sess = factory()
        try:
            mat = AssetMaterializer(sess, cache_root=tmp_path / "cache-routes")
            fe_base = build_frontend_base(run_dir, fe_bp, openapi_doc, mat)
        finally:
            sess.close()

        # 1. 正常 5 部门确定性聚合
        delivs = _create_stub_deliverables(Path(fe_base.base_dir), run_dir / "ok")
        out_1 = aggregate_frontend_routes(delivs)
        out_2 = aggregate_frontend_routes(list(delivs.values()))
        assert out_1 == out_2
        for mod in FIVE_FE_DEPTS:
            assert f"../modules/{mod}/routes" in out_1

        # 2. 负例：order-ui 声明与 cart-ui 冲突的 /cart 路由
        dup_delivs = _create_stub_deliverables(
            Path(fe_base.base_dir),
            run_dir / "dup-route",
            overrides={
                "order-ui": {
                    "frontend/src/modules/order-ui/routes.ts": (
                        "import type { RouteRecordRaw } from 'vue-router';\n"
                        "import OrderView from './OrderView.vue';\n"
                        "export default [{ path: '/cart', name: 'dup', component: OrderView, meta: { requiresAuth: true } }] as RouteRecordRaw[];\n"
                    )
                }
            },
        )
        with pytest.raises(AppException) as exc_dup:
            aggregate_frontend_routes(dup_delivs)
        assert exc_dup.value.code is ErrorCode.ASSET_CONFLICT
        assert "/cart" in exc_dup.value.message

        # 3. 负例：auth-ui 侵入底座保留路由 /login
        reserved_delivs = _create_stub_deliverables(
            Path(fe_base.base_dir),
            run_dir / "reserved-route",
            overrides={
                "auth-ui": {
                    "frontend/src/modules/auth-ui/routes.ts": (
                        "import type { RouteRecordRaw } from 'vue-router';\n"
                        "import AuthView from './AuthView.vue';\n"
                        "export default [{ path: '/login', name: 'hijack', component: AuthView }] as RouteRecordRaw[];\n"
                    )
                }
            },
        )
        with pytest.raises(AppException) as exc_res:
            aggregate_frontend_routes(reserved_delivs)
        assert exc_res.value.code is ErrorCode.ASSET_CONFLICT
        assert "/login" in exc_res.value.message
    finally:
        engine.dispose()
        for sub in ("cache-routes", "run-routes"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)


def test_negative_pre_assembly_blocks_ghost_api_xss_v_html_hardcoded_secret_and_missing_auth_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确定性总装前门禁负例全覆盖：假端点、未知契约键、v-html XSS、硬编码密钥、漏鉴权守卫、未真跑测试。"""
    engine, factory, project_id, run_id, _store = _setup_db_with_passed_backend(tmp_path, monkeypatch)
    try:
        spec = _make_ecommerce_spec()
        openapi_doc = _make_authoritative_ecommerce_openapi(frozen=True)
        fe_bp = _make_5_dept_frontend_blueprint(spec, project_id=project_id)

        run_dir = tmp_path / "run-sec"
        sess = factory()
        try:
            mat = AssetMaterializer(sess, cache_root=tmp_path / "cache-sec")
            fe_base = build_frontend_base(run_dir, fe_bp, openapi_doc, mat)
        finally:
            sess.close()

        base_path = Path(fe_base.base_dir)

        # Case 1: 调用权威 openapi.json 中不存在的 /api/ghost/coupon
        d_ghost = _create_stub_deliverables(
            base_path,
            run_dir / "c1",
            overrides={
                "cart-ui": {
                    "frontend/src/modules/cart-ui/service.ts": (
                        "export const GHOST_URL = '/api/ghost/coupon';\n"
                    )
                }
            },
        )
        m1 = merge_frontend_deliverables(
            base_path, d_ghost, run_dir / "out1", blueprint=fe_bp, openapi_doc=openapi_doc, run_id=run_id
        )
        assert m1.ok is False
        assert any(
            t.dept_id == "cart-ui" and t.error_class == "CONTRACT_MISMATCH" and "/api/ghost/coupon" in t.summary
            for t in m1.conflict_tickets
        )

        # Case 2: 引用未知 API_ENDPOINTS.ghostEndpoint
        d_op = _create_stub_deliverables(
            base_path,
            run_dir / "c2",
            overrides={
                "order-ui": {
                    "frontend/src/modules/order-ui/service.ts": (
                        "import { API_ENDPOINTS } from '../../generated/api-endpoints';\n"
                        "export const BAD = (API_ENDPOINTS as any).ghostEndpoint;\n"
                    )
                }
            },
        )
        m2 = merge_frontend_deliverables(
            base_path, d_op, run_dir / "out2", blueprint=fe_bp, openapi_doc=openapi_doc, run_id=run_id
        )
        assert m2.ok is False
        assert any(
            t.dept_id == "order-ui" and t.error_class == "CONTRACT_MISMATCH" and "ghostEndpoint" in t.summary
            for t in m2.conflict_tickets
        )

        # Case 3: XSS 违规（在 .vue 中使用 v-html）
        d_xss = _create_stub_deliverables(
            base_path,
            run_dir / "c3",
            overrides={
                "catalog-ui": {
                    "frontend/src/modules/catalog-ui/CatalogView.vue": (
                        "<template><div v-html=\"userInput\"></div></template>\n"
                    )
                }
            },
        )
        m3 = merge_frontend_deliverables(
            base_path, d_xss, run_dir / "out3", blueprint=fe_bp, openapi_doc=openapi_doc, run_id=run_id
        )
        assert m3.ok is False
        assert any(
            t.dept_id == "catalog-ui" and t.error_class == "SECURITY_VIOLATION" and "v-html" in t.summary
            for t in m3.conflict_tickets
        )

        # Case 4: 硬编码密钥（sk-live-...）
        d_sec = _create_stub_deliverables(
            base_path,
            run_dir / "c4",
            overrides={
                "payment-ui": {
                    "frontend/src/modules/payment-ui/service.ts": (
                        "export const STRIPE_KEY = 'sk-live-abcdef1234567890xyz';\n"
                    )
                }
            },
        )
        m4 = merge_frontend_deliverables(
            base_path, d_sec, run_dir / "out4", blueprint=fe_bp, openapi_doc=openapi_doc, run_id=run_id
        )
        assert m4.ok is False
        assert any(
            t.dept_id == "payment-ui" and t.error_class == "SECURITY_VIOLATION"
            for t in m4.conflict_tickets
        )

        # Case 5: 受保护业务模块遗漏 requiresAuth: true 守卫
        d_auth = _create_stub_deliverables(
            base_path,
            run_dir / "c5",
            overrides={
                "order-ui": {
                    "frontend/src/modules/order-ui/routes.ts": (
                        "import type { RouteRecordRaw } from 'vue-router';\n"
                        "import OrderView from './OrderView.vue';\n"
                        "export default [{ path: '/orders', name: 'orders', component: OrderView }] as RouteRecordRaw[];\n"
                    )
                }
            },
        )
        m5 = merge_frontend_deliverables(
            base_path, d_auth, run_dir / "out5", blueprint=fe_bp, openapi_doc=openapi_doc, run_id=run_id
        )
        assert m5.ok is False
        assert any(
            t.dept_id == "order-ui" and t.error_class == "CONTRACT_MISMATCH" and "requiresAuth" in t.summary
            for t in m5.conflict_tickets
        )

        # Case 6: 部门测试未真实执行（NOT_RUN / exit_code=None）严禁放行
        d_notrun = _create_stub_deliverables(base_path, run_dir / "c6", not_run_dept="cart-ui")
        m6 = merge_frontend_deliverables(
            base_path, d_notrun, run_dir / "out6", blueprint=fe_bp, openapi_doc=openapi_doc, run_id=run_id
        )
        assert m6.ok is False
        assert any(
            t.dept_id == "cart-ui" and t.error_class == "TEST_NOT_EXECUTED"
            for t in m6.conflict_tickets
        )
    finally:
        engine.dispose()
        for sub in ("cache-sec", "run-sec"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)


class _ScriptedFrontendAssemblyVerifier:
    verifier_kind = "local_process"

    def __init__(self, outcomes: list[tuple[str, int | None, str | None, str]]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def verify_module(
        self,
        *,
        artifact_dir: str | Path,
        module_id: str,
        stack: str = "frontend",
        test_selector: str = "test",
        timeout_seconds: int = 120,
    ) -> ModuleVerificationResult:
        idx = min(len(self.calls), len(self._outcomes) - 1)
        status, exit_code, err_code, log = self._outcomes[idx]
        self.calls.append({"artifact_dir": str(artifact_dir), "module_id": module_id})
        return ModuleVerificationResult(
            artifact_dir=str(artifact_dir),
            module_id=module_id,
            stack="frontend",
            test_selector=test_selector,
            status=status,  # type: ignore[arg-type]
            verifier_kind="local_process",
            error_code=err_code,
            exit_code=exit_code,
            duration_ms=25,
            command=["npx", "vue-tsc", "--noEmit"],
            detail=log,
            log_excerpt=log,
        )


def test_frontend_integration_big_loop_triage_targeted_rework_only_and_infra_zero_rework(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前端大循环验收：

    - Case A：全量验证第 1 轮失败 → 总管归因仅打回 ``payment-ui``（其余 4 部门不重跑）→ 第 2 轮全绿；
    - Case B：全量验证报 ``INFRASTRUCTURE`` → 立即停止、返工次数 = 0、总管归因调用 = 0。
    """
    engine, factory, project_id, run_id, store = _setup_db_with_passed_backend(tmp_path, monkeypatch)
    try:
        spec = _make_ecommerce_spec()
        openapi_doc = _make_authoritative_ecommerce_openapi(frozen=True)
        openapi_sha = canonical_openapi_sha256(openapi_doc)
        fe_bp = _make_5_dept_frontend_blueprint(spec, project_id=project_id)

        run_dir = tmp_path / "run-bigloop"
        sess = factory()
        try:
            mat = AssetMaterializer(sess, cache_root=tmp_path / "cache-bigloop")
            fe_base = build_frontend_base(
                run_dir,
                fe_bp,
                openapi_doc,
                mat,
                openapi_sha256=openapi_sha,
                state_store=store,
                run_id=run_id,
            )
        finally:
            sess.close()

        dept_exec_counts: dict[str, int] = {}

        def _coder_factory(binding: LedgerBinding, round_num: int) -> BaseChatModel:
            dept_exec_counts[binding.dept_id] = dept_exec_counts.get(binding.dept_id, 0) + 1
            code_files, _ = _dept_valid_frontend_files(binding.dept_id)
            return _ScriptedToolModel(
                responses=_write_calls(code_files, prefix=f"bl-c-{binding.dept_id}-r{round_num}")
            )

        def _tester_factory(binding: LedgerBinding, round_num: int) -> BaseChatModel:
            _, test_files = _dept_valid_frontend_files(binding.dept_id)
            return _ScriptedToolModel(
                responses=_write_calls(test_files, prefix=f"bl-t-{binding.dept_id}-r{round_num}")
            )

        dept_verifier = _ScriptedFrontendAssemblyVerifier([("PASS", 0, None, "ok")])
        lead_client = _ScriptedTextClient(_make_lead_json_resolver())

        def _runner_factory(_mod: Module, scoped_verifier: Any, _esc_role: str | None) -> DepartmentRunner:
            return DepartmentRunner(
                session_factory=factory,
                state_store=store,
                verifier=scoped_verifier,
                lead_client=lead_client,
                coder_model_factory=_coder_factory,
                tester_model_factory=_tester_factory,
            )

        pool = AsyncDepartmentPool(
            session_factory=factory,
            verifier=dept_verifier,
            runner_factory=_runner_factory,
            state_store=store,
            max_concurrent_depts_limit=3,
            max_concurrent_verifiers_limit=2,
            max_rounds=3,
        )

        # Case A：总装第 1 轮报 payment-ui 集成错误 → 总管精准归因仅打回 payment-ui → 第 2 轮 PASS
        chief_client = _ScriptedTextClient(
            lambda *_: json.dumps(
                {
                    "defects": [
                        {
                            "dept_id": "payment-ui",
                            "error_class": "TS_TYPE_ERROR",
                            "summary": "PaymentView.vue 缺少 sandboxMode 类型窄化",
                            "affected_paths": ["frontend/src/modules/payment-ui/PaymentView.vue"],
                            "failing_tests": ["test/payment-ui/payment.spec.ts"],
                            "log_excerpt": "TS2322 in PaymentView.vue",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )
        chief = ChiefArchitect(session_factory=factory, state_store=store, client=chief_client)
        assembly_verifier = _ScriptedFrontendAssemblyVerifier(
            [
                ("FAIL", 2, "BUILD_FAILED", "src/modules/payment-ui/PaymentView.vue: error TS2322"),
                ("PASS", 0, None, "all frontend checks passed"),
            ]
        )

        res_a = asyncio.run(
            run_frontend_factory_stage(
                run_id=run_id,
                blueprint=fe_bp,
                spec=spec,
                openapi_doc=openapi_doc,
                openapi_sha256=openapi_sha,
                run_dir=run_dir,
                base_dir=fe_base.base_dir,
                pool=pool,
                chief=chief,
                state_store=store,
                module_verifier=assembly_verifier,
            )
        )
        assert res_a.ok is True
        assert res_a.status == "PASSED"
        assert res_a.integration_rounds_used == 2
        assert res_a.rework_rounds == 1
        assert res_a.reworked_depts_history == [["payment-ui"]]
        assert len(chief_client.prompts) == 1
        # 仅 payment-ui 执行了 2 次，其余 4 个前端部门只执行了 1 次
        assert dept_exec_counts["payment-ui"] == 2
        for other in ("auth-ui", "catalog-ui", "cart-ui", "order-ui"):
            assert dept_exec_counts[other] == 1

        # Case B：INFRASTRUCTURE 失败 → 立即停止，零返工、零总管归因调用
        infra_chief_client = _ScriptedTextClient(lambda *_: '{"defects": []}')
        infra_chief = ChiefArchitect(
            session_factory=factory,
            state_store=store,
            client=infra_chief_client,
        )
        infra_verifier = _ScriptedFrontendAssemblyVerifier(
            [("NOT_RUN", 127, "INFRASTRUCTURE", "executable file not found: npx")]
        )
        res_b = asyncio.run(
            run_frontend_factory_stage(
                run_id=run_id,
                blueprint=fe_bp,
                spec=spec,
                openapi_doc=openapi_doc,
                openapi_sha256=openapi_sha,
                run_dir=run_dir / "infra-case",
                base_dir=fe_base.base_dir,
                pool=pool,
                chief=infra_chief,
                state_store=store,
                module_verifier=infra_verifier,
                deliverables=res_a.deliverables,
            )
        )
        assert res_b.ok is False
        assert res_b.status == "INFRASTRUCTURE"
        assert res_b.error_code == "INFRASTRUCTURE"
        assert res_b.rework_rounds == 0
        assert len(infra_chief_client.prompts) == 0
    finally:
        engine.dispose()
        for sub in ("cache-bigloop", "run-bigloop"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)
