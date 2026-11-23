"""FP09：全栈总装、独立 Docker Runner 端到端验收与工厂交付包打包测试。

覆盖 WhenDone 全部要求：
1. 开启 ``AGENTFORGE_FACTORY_PIPELINE_ENABLED=1`` 后，经 ``POST /api/projects/{id}/runs`` +
   ``worker.run_worker(...)`` 完整跑通：
   ``后端总管 → 后端并行部门 → 后端总装验收(冻结权威 openapi.json) → 前端总管 → 前端并行部门``
   ``→ 前端路由聚合 → 全栈总装 → 强制独立 Docker Runner 端到端验收 → ZIP 打包``；
2. 下载的 ``source.zip`` 包含完整前后端源码、SQL（含 ``V900__relations.sql``）、``compose.yaml``、
   ``README.md``、权威 ``backend/openapi.json``、``factory-blueprint.json``、按部门拆分的
   ``cost-report.json`` 与 ``department-cost-report.json``、``verification.json``（沙箱支付明确标
   ``MOCK_ONLY`` / ``SANDBOX_VERIFIED``，绝不伪标 ``LIVE_VERIFIED``）、``EXTERNAL_SERVICES.md``，
   排除 ``node_modules/``、``target/``、``staging/``、``.cache/``、``.env``，且密钥扫描 0 命中；
3. 负例：
   - 独立 Docker Runner 不可用时，明确标 ``NOT_RUN``、run 落 ``FAILED``（绝不标 ``SUCCEEDED``）；
   - 企图将 ``LocalProcessModuleVerifier`` 用于全栈最终验收时，安全守卫拒绝（``FORBIDDEN``）；
   - 打包前密钥扫描命中即中止打包、删除临时 ZIP 并标 ``SECRET_DETECTED``；
   - 关闭 ``AGENTFORGE_FACTORY_PIPELINE_ENABLED=false`` 时原有串行路径与 Demo 范围校验零改动。
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agentforge_platform.asset_cache import remove_readonly_tree
from agentforge_platform.asset_registry import REPO_ROOT, default_registry
from agentforge_platform.blueprint_contracts import ArchitectureBlueprint
from agentforge_platform.clients import Completion, Usage
from agentforge_platform.db import session_scope
from agentforge_platform.factory_chief import ChiefArchitect
from agentforge_platform.factory_department import API_FRAGMENT_FILENAME, DepartmentRunner
from agentforge_platform.factory_pipeline import (
    FACTORY_PIPELINE_ENV,
    FactoryRuntime,
    factory_pipeline_enabled,
)
from agentforge_platform.factory_scheduler import AsyncDepartmentPool
from agentforge_platform.factory_state import FactoryStateStore
from agentforge_platform.interface_registry import (
    KIND_HTTP,
    KIND_JAVA_METHOD,
    STATUS_APPROVED_FOR_BUILD,
    PublicCapability,
)
from agentforge_platform.models import Project, Run, SpecVersion
from agentforge_platform.module_registry import Module
from agentforge_platform.module_verifier import (
    LocalProcessModuleVerifier,
    ModuleVerificationResult,
)
from agentforge_platform.packaging import PackagingError, package_run
from agentforge_platform.patch_engine import LedgerBinding
from agentforge_platform.resolver import resolve_assets
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash
from agentforge_platform.worker import build_generation_handler, run_worker

CAMPUS_SPEC_PATH = REPO_ROOT / "schemas" / "campus-activity.spec.json"
ALICE_CREDS = {"username": "alice", "password": "alice-secret"}
BASE_PKG = "com.agentforge.template"
PKG_PATH = "com/agentforge/template"

FIVE_BE_DEPTS = ("auth", "product", "cart", "order", "payment")
FIVE_FE_DEPTS = ("auth-ui", "catalog-ui", "cart-ui", "order-ui", "payment-ui")


def _configure_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = {
        "version": "fp09-test-v1",
        "currency": "CNY",
        "roles": {
            role: {
                "provider": "openai_compatible",
                "model": f"test-{role}",
                "base_url": "https://api.fake-llm.example/v1",
                "api_key_env": "FAKE_FP09_KEY",
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
    base["goal"] = "电商沙箱全栈 5 部门：登录注册、商品展示、购物车、订单与本地模拟支付"
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


def _make_backend_blueprint(spec: SoftwareSpecification, *, project_id: int) -> ArchitectureBlueprint:
    spec_hash = content_hash(spec)
    profile_ref = f"{spec.technical_profile.profile_id}@{spec.technical_profile.profile_version}"
    pid = str(project_id)
    depts = [
        ("auth", "用户认证模块", (100, 199), "/api/auth/register", "POST", "UserAccount", "AuthPage", []),
        ("product", "商品目录模块", (200, 299), "/api/products", "GET", "Product", "ProductHomePage", []),
        ("cart", "购物车模块", (300, 399), "/api/cart/items", "POST", "CartItem", "CartPage", ["product"]),
        ("order", "订单结算模块", (400, 499), "/api/orders", "POST", "PurchaseOrder", "OrderCheckoutPage", ["cart"]),
        (
            "payment",
            "本地沙箱支付模块",
            (500, 599),
            "/api/payments/sandbox-pay",
            "POST",
            "PaymentRecord",
            "SandboxPayPage",
            ["order"],
        ),
    ]
    modules: list[Module] = []
    contracts: list[PublicCapability] = []
    for dept_id, name, mig_range, http_path, method, _ent, _page, deps in depts:
        cls = dept_id.capitalize()
        readable = [f"{d}.facade" for d in deps]
        modules.append(
            Module(
                project_id=pid,
                module_id=dept_id,
                name=name,
                based_on_spec_hash=spec_hash,
                profile_ref=profile_ref,
                owned_paths=[
                    f"backend/src/main/java/{PKG_PATH}/modules/{dept_id}/internal/",
                    f"backend/src/main/resources/db/migration/V{mig_range[0]}__{dept_id}.sql",
                ],
                test_paths=[f"backend/src/test/java/{PKG_PATH}/modules/{dept_id}/"],
                owned_package=f"{BASE_PKG}.modules.{dept_id}",
                migration_range=mig_range,
                depends_on_modules=deps,
                readable_contract_refs=readable,
            )
        )
        contracts.append(
            PublicCapability(
                capability_id=f"{dept_id}.facade",
                owner_module_id=dept_id,
                interface_kind=KIND_JAVA_METHOD,
                symbol=f"{cls}Facade.describe{cls}",
                signature=f"String describe{cls}(Long id)",
                status=STATUS_APPROVED_FOR_BUILD,
                version=1,
            )
        )
        contracts.append(
            PublicCapability(
                capability_id=f"{dept_id}.http",
                owner_module_id=dept_id,
                interface_kind=KIND_HTTP,
                http_method=method,
                http_path=http_path,
                signature=f"{method} {http_path}",
                status=STATUS_APPROVED_FOR_BUILD,
                version=1,
            )
        )

    return ArchitectureBlueprint(
        phase="backend",
        project_id=pid,
        based_on_spec_hash=spec_hash,
        profile_ref=profile_ref,
        base_package=BASE_PKG,
        modules=modules,
        frozen_contracts=contracts,
        covered_entities={d: [ent] for d, _, _, _, _, ent, _, _ in depts},
        covered_pages={d: [pg] for d, _, _, _, _, _, pg, _ in depts},
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


def _make_frontend_blueprint(spec: SoftwareSpecification, *, project_id: int) -> ArchitectureBlueprint:
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
        covered_pages={d: [pg] for d, *_, pg in depts},
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


class _ScriptedClient:
    def __init__(self, resolver) -> None:
        self._resolver = resolver
        self.prompts: list[str] = []

    def complete(self, *, model: str, prompt: str, **kwargs: Any) -> Completion:
        self.prompts.append(prompt)
        return Completion(
            content=self._resolver(prompt, len(self.prompts)),
            model=model,
            request_id=f"req-fp09-{len(self.prompts)}",
            usage=Usage(input_tokens=600, output_tokens=300),
            finish_reason="stop",
        )


class _ScriptedToolModel(BaseChatModel):
    responses: list[AIMessage]
    model_name: str = "test-dept_coder"

    @property
    def _llm_type(self) -> str:
        return "scripted_fp09"

    def _get_ls_params(self, **kwargs: Any) -> dict[str, Any]:
        return {"ls_provider": "openai", "ls_model_name": self.model_name}

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedToolModel:
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any):
        msg = self.responses.pop(0) if self.responses else AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _write_calls(file_map: dict[str, str], *, prefix: str) -> list[AIMessage]:
    calls = [
        {
            "name": "write_file",
            "args": {"file_path": rel, "content": content},
            "id": f"call-{prefix}-{idx}",
            "type": "tool_call",
        }
        for idx, (rel, content) in enumerate(file_map.items(), start=1)
    ]
    return [AIMessage(content="", tool_calls=calls), AIMessage(content="done")]


class _MockDeptVerifier:
    verifier_kind = "docker_runner"

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
            stack="java" if stack == "java" else "frontend",
            test_selector=test_selector,
            status="PASS",
            verifier_kind="docker_runner",
            exit_code=0,
            duration_ms=30,
            command=["docker-runner", stack, test_selector],
            detail="ok",
            log_excerpt="Tests run: 1, Failures: 0",
        )


class _MockDockerRunnerClient:
    """模拟独立 Docker Runner（实现 verify / accept / verify_module）。"""

    def __init__(
        self,
        *,
        fail_fullstack_with_unreachable: bool = False,
        inject_secret_in_backend: bool = False,
        work_root: Path | None = None,
    ) -> None:
        self.verify_calls: list[str] = []
        self.accept_calls: list[str] = []
        self._fail_fullstack_with_unreachable = fail_fullstack_with_unreachable
        self._inject_secret_in_backend = inject_secret_in_backend
        self._work_root = work_root

    def verify(self, artifact_dir: str) -> dict[str, Any]:
        self.verify_calls.append(artifact_dir)
        is_fullstack = not artifact_dir.endswith("backend_assembled")
        if is_fullstack and self._fail_fullstack_with_unreachable:
            raise RuntimeError("Docker Runner connection refused on http://127.0.0.1:9000/verify")
        if is_fullstack and self._inject_secret_in_backend and self._work_root is not None:
            leak = self._work_root / artifact_dir / "backend" / "leaked.properties"
            leak.write_text("OPENAI_KEY=sk-live-secret9999999999999999\n", encoding="utf-8")
        return {
            "overall": "PASS",
            "checks": [
                {"name": "static_manifest", "status": "PASS"},
                {"name": "backend_test", "status": "PASS"},
                {"name": "backend_build", "status": "PASS"},
                {"name": "frontend_build", "status": "PASS"},
            ],
        }

    def accept(self, artifact_dir: str) -> dict[str, Any]:
        self.accept_calls.append(artifact_dir)
        return {
            "overall": "PASS",
            "cases": [{"name": "ecommerce_sandbox_e2e", "status": "PASS"}],
            "scenarios": [{"name": "ecommerce_sandbox_e2e", "status": "PASS"}],
        }


def _backend_dept_files(dept_id: str) -> tuple[dict[str, str], dict[str, str]]:
    mig_map = {
        "auth": ("V100__auth.sql", "/api/auth/register", "post", "registerUser", ["PUBLIC"]),
        "product": ("V200__product.sql", "/api/products", "get", "listProducts", ["PUBLIC"]),
        "cart": ("V300__cart.sql", "/api/cart/items", "post", "addCartItem", ["USER"]),
        "order": ("V400__order.sql", "/api/orders", "post", "createOrder", ["USER"]),
        "payment": (
            "V500__payment.sql",
            "/api/payments/sandbox-pay",
            "post",
            "executeSandboxPay",
            ["USER"],
        ),
    }
    sql_name, path, method, op_id, auth_roles = mig_map[dept_id]
    cls = dept_id.capitalize()
    frag = {
        "paths": {
            path: {
                method: {
                    "operationId": op_id,
                    "x-department": dept_id,
                    "x-auth": auth_roles,
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"ok": {"type": "boolean"}},
                                        "required": ["ok"],
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
        "components": {"schemas": {}},
    }
    code = {
        f"backend/src/main/java/{PKG_PATH}/modules/{dept_id}/internal/{cls}Controller.java": (
            f"package {BASE_PKG}.modules.{dept_id}.internal;\n"
            f"public class {cls}Controller {{ public String ok() {{ return \"ok\"; }} }}\n"
        ),
        f"backend/src/main/resources/db/migration/{sql_name}": (
            f"CREATE TABLE IF NOT EXISTS t_{dept_id} (id BIGINT PRIMARY KEY);\n"
        ),
        API_FRAGMENT_FILENAME: json.dumps(frag, ensure_ascii=False, indent=2),
    }
    tests = {
        f"backend/src/test/java/{PKG_PATH}/modules/{dept_id}/{cls}ControllerTest.java": (
            f"package {BASE_PKG}.modules.{dept_id};\n"
            f"public class {cls}ControllerTest {{}}\n"
        )
    }
    return code, tests


def _frontend_dept_files(dept_id: str) -> tuple[dict[str, str], dict[str, str]]:
    meta_map = {
        "auth-ui": ("/auth", "registerUser", False),
        "catalog-ui": ("/products", "listProducts", False),
        "cart-ui": ("/cart", "addCartItem", True),
        "order-ui": ("/orders", "createOrder", True),
        "payment-ui": ("/payments", "executeSandboxPay", True),
    }
    route_path, op_key, req_auth = meta_map[dept_id]
    auth_str = "true" if req_auth else "false"
    code = {
        f"frontend/src/modules/{dept_id}/service.ts": (
            f"import {{ API_ENDPOINTS }} from '../../generated/api-endpoints';\n"
            f"export const ENDPOINT = API_ENDPOINTS.{op_key}.path;\n"
        ),
        f"frontend/src/modules/{dept_id}/View.vue": (
            "<template><section>ok</section></template>\n"
        ),
        f"frontend/src/modules/{dept_id}/routes.ts": (
            "import type { RouteRecordRaw } from 'vue-router';\n"
            f"const routes: RouteRecordRaw[] = [{{ path: '{route_path}', name: '{dept_id}', "
            f"component: () => import('./View.vue'), meta: {{ requiresAuth: {auth_str} }} }}];\n"
            "export default routes;\n"
        ),
    }
    tests = {
        f"frontend/test/{dept_id}/module.spec.ts": (
            "import { describe, expect, it } from 'vitest';\n"
            f"describe('{dept_id}', () => {{ it('works', () => {{ expect(true).toBe(true); }}); }});\n"
        )
    }
    return code, tests


def _build_scripted_factory_runtime(
    *,
    factory,
    spec: SoftwareSpecification,
    project_id: int,
    tmp_path: Path,
) -> FactoryRuntime:
    store = FactoryStateStore(factory)
    be_bp = _make_backend_blueprint(spec, project_id=project_id)
    fe_bp = _make_frontend_blueprint(spec, project_id=project_id)

    def _chief_resolver(prompt: str, _idx: int) -> str:
        if "前端项目总管" in prompt or "phase: frontend" in prompt or "openapi.json" in prompt:
            return fe_bp.model_dump_json()
        return be_bp.model_dump_json()

    chief = ChiefArchitect(
        session_factory=factory,
        state_store=store,
        client=_ScriptedClient(_chief_resolver),
    )

    lead_client = _ScriptedClient(
        lambda *_: json.dumps(
            {
                "summary": "实施部门最小职责交付",
                "files_to_create": [],
                "reused_assets": [],
                "consumed_contracts": [],
                "exposed_endpoints": [],
                "test_focus": ["契约对齐与单测通过"],
            },
            ensure_ascii=False,
        )
    )
    dept_verifier = _MockDeptVerifier()

    def _be_coder(binding: LedgerBinding, r: int) -> BaseChatModel:
        code, _ = _backend_dept_files(binding.dept_id)
        return _ScriptedToolModel(responses=_write_calls(code, prefix=f"be-c-{binding.dept_id}-r{r}"))

    def _be_tester(binding: LedgerBinding, r: int) -> BaseChatModel:
        _, tests = _backend_dept_files(binding.dept_id)
        return _ScriptedToolModel(responses=_write_calls(tests, prefix=f"be-t-{binding.dept_id}-r{r}"))

    def _fe_coder(binding: LedgerBinding, r: int) -> BaseChatModel:
        code, _ = _frontend_dept_files(binding.dept_id)
        return _ScriptedToolModel(responses=_write_calls(code, prefix=f"fe-c-{binding.dept_id}-r{r}"))

    def _fe_tester(binding: LedgerBinding, r: int) -> BaseChatModel:
        _, tests = _frontend_dept_files(binding.dept_id)
        return _ScriptedToolModel(responses=_write_calls(tests, prefix=f"fe-t-{binding.dept_id}-r{r}"))

    be_pool = AsyncDepartmentPool(
        session_factory=factory,
        verifier=dept_verifier,
        state_store=store,
        runner_factory=lambda _m, v, _e: DepartmentRunner(
            session_factory=factory,
            state_store=store,
            verifier=v,
            lead_client=lead_client,
            coder_model_factory=_be_coder,
            tester_model_factory=_be_tester,
        ),
        max_concurrent_depts_limit=3,
        max_concurrent_verifiers_limit=2,
    )
    fe_pool = AsyncDepartmentPool(
        session_factory=factory,
        verifier=dept_verifier,
        state_store=store,
        runner_factory=lambda _m, v, _e: DepartmentRunner(
            session_factory=factory,
            state_store=store,
            verifier=v,
            lead_client=lead_client,
            coder_model_factory=_fe_coder,
            tester_model_factory=_fe_tester,
        ),
        max_concurrent_depts_limit=3,
        max_concurrent_verifiers_limit=2,
    )
    return FactoryRuntime(
        chief=chief,
        backend_pool=be_pool,
        frontend_pool=fe_pool,
        frontend_module_verifier=dept_verifier,
        asset_cache_root=tmp_path / "asset-cache",
    )


def _seed_confirmed_ecommerce_project(factory, spec: SoftwareSpecification) -> int:
    spec_json = canonical_json(spec)
    spec_hash = content_hash(spec)
    with session_scope(factory) as db:
        project = Project(owner_id=1, name="ecommerce-factory", requirement_text=spec.goal)
        db.add(project)
        db.flush()
        sv = SpecVersion(
            project_id=project.id,
            version=1,
            body_json=spec_json,
            hash=spec_hash,
            confirmed_at=datetime.now(UTC),
        )
        db.add(sv)
        db.flush()
        project.active_spec_id = sv.id
        db.flush()
        return int(project.id)


def test_e2e_api_post_runs_worker_full_factory_pipeline_and_download_zip(
    platform_app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """核心端到端闭环：POST /api/projects/{id}/runs → worker → 后端/前端并行 → Docker Runner 全栈验收 → 下载 ZIP。"""
    app, factory = platform_app
    _configure_models(tmp_path, monkeypatch)
    monkeypatch.setenv(FACTORY_PIPELINE_ENV, "1")
    work_root = tmp_path / "work-runs"
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(work_root))

    spec = _make_ecommerce_spec()
    project_id = _seed_confirmed_ecommerce_project(factory, spec)

    client = TestClient(app)
    assert client.post("/api/auth/login", json=ALICE_CREDS).status_code == 200

    # 1. POST /api/projects/{id}/runs 提交工厂运行（202 QUEUED）
    resp = client.post(
        f"/api/projects/{project_id}/runs",
        json={},
        headers={
            "X-CSRF-Token": client.cookies.get("af_csrf"),
            "Idempotency-Key": "fp09-e2e-run-1",
        },
    )
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["data"]["id"]

    # 2. 经独立 worker 领取并驱动完整 FP09 异步工厂流水线
    docker_runner = _MockDockerRunnerClient(work_root=work_root)
    runtime = _build_scripted_factory_runtime(
        factory=factory,
        spec=spec,
        project_id=project_id,
        tmp_path=tmp_path,
    )
    handler = build_generation_handler(
        factory,
        runner=docker_runner,
        factory_runtime=runtime,
    )
    run_worker(
        session_factory=factory,
        handler=handler,
        worker_id="worker-fp09",
        iterations=1,
        poll_interval=0.01,
    )

    try:
        events = client.get(f"/api/runs/{run_id}/events").json()["data"]["items"]
        # 独立 Docker Runner 分别执行了后端总验收 + 全栈最终验收（各 1 次 verify + accept）
        assert len(docker_runner.verify_calls) == 2, f"events={events}"
        assert len(docker_runner.accept_calls) == 2
        assert docker_runner.verify_calls[-1] == f"run-{run_id}"

        # 3. 查询 GET /api/runs/{run_id} 核验状态、沙箱外部就绪度与可下载产物
        detail = client.get(f"/api/runs/{run_id}").json()["data"]
        assert detail["status"] == "SUCCEEDED"
        assert detail["external_readiness"] == "MOCK_ONLY"
        assert detail["package"]["available"] is True
        assert detail["package"]["verified"] is True
        zip_artifacts = [a for a in detail["artifacts"] if a["kind"] == "zip"]
        assert len(zip_artifacts) == 1
        artifact_id = zip_artifacts[0]["id"]

        # 4. 下载 ZIP 并逐项核验 FP09 全部交付清单与安全排除规则
        dl = client.get(f"/api/runs/{run_id}/artifacts/{artifact_id}")
        assert dl.status_code == 200
        assert dl.headers.get("X-Artifact-Verified") == "true"

        with zipfile.ZipFile(io.BytesIO(dl.content), "r") as zf:
            names = set(zf.namelist())

            # 必需交付清单项
            for required in (
                "compose.yaml",
                "README.md",
                "spec.json",
                "asset-lock.json",
                "backend/openapi.json",
                "backend/src/main/resources/db/migration/V100__auth.sql",
                "backend/src/main/resources/db/migration/V500__payment.sql",
                "backend/src/main/resources/db/migration/V900__relations.sql",
                "frontend/src/generated/api-types.ts",
                "frontend/src/generated/api-endpoints.ts",
                "frontend/src/router/generated-routes.ts",
                "frontend/src/modules/payment-ui/routes.ts",
                "factory-blueprint.json",
                "cost-report.json",
                "department-cost-report.json",
                "verification.json",
                "EXTERNAL_SERVICES.md",
            ):
                assert required in names, f"ZIP 缺少必需交付项：{required}"

            # 严禁包含 node_modules/、target/、staging/、.cache/、.env
            for name in names:
                parts = Path(name).parts
                assert not (
                    set(parts) & {"node_modules", "target", "staging", ".cache", ".staging", ".env"}
                ), f"ZIP 违规包含排除目录/文件：{name}"

            # 权威 OpenAPI 已冻结
            openapi_doc = json.loads(zf.read("backend/openapi.json").decode("utf-8"))
            assert openapi_doc["x-agentforge-frozen"] is True
            assert "/api/payments/sandbox-pay" in openapi_doc["paths"]

            # 蓝图包含后端与前端 5 部门
            bp_doc = json.loads(zf.read("factory-blueprint.json").decode("utf-8"))
            assert len(bp_doc["backend"]["modules"]) == 5
            assert len(bp_doc["frontend"]["modules"]) == 5

            # 成本报表同时包含总计与按阶段/部门明细
            cost_doc = json.loads(zf.read("cost-report.json").decode("utf-8"))
            dept_cost_doc = json.loads(zf.read("department-cost-report.json").decode("utf-8"))
            assert cost_doc["generation_phase"]["calls"] > 0
            assert "by_department" in cost_doc
            assert "backend" in dept_cost_doc["by_department"]["by_phase"]
            assert "frontend" in dept_cost_doc["by_department"]["by_phase"]

            # verification.json 明确标 MOCK_ONLY / SANDBOX_VERIFIED，绝不伪标 LIVE_VERIFIED
            ver_doc = json.loads(zf.read("verification.json").decode("utf-8"))
            assert ver_doc["verified"] is True
            assert ver_doc["external_readiness"] == "MOCK_ONLY"
            assert ver_doc["external_readiness"] != "LIVE_VERIFIED"
            assert ver_doc["sandbox_verification"]["status"] == "SANDBOX_VERIFIED"
            assert ver_doc["sandbox_verification"]["live_verified"] is False

            # EXTERNAL_SERVICES.md 明确标注支付为本地沙箱模拟及切换指引
            ext_md = zf.read("EXTERNAL_SERVICES.md").decode("utf-8")
            assert "MOCK_ONLY" in ext_md
            assert "SANDBOX_VERIFIED" in ext_md
            assert "/api/payments/sandbox-pay" in ext_md
            assert "生产环境切换指引" in ext_md
    finally:
        for p in (tmp_path / "asset-cache", work_root):
            if p.exists():
                remove_readonly_tree(p)


def test_negative_missing_or_unreachable_docker_runner_marks_not_run_and_never_succeeds(
    platform_app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """负例：全栈验收时 Docker Runner 不可用 → 明确标 NOT_RUN、run 落 FAILED，绝不标 SUCCEEDED；禁止本地进程替代。"""
    app, factory = platform_app
    _configure_models(tmp_path, monkeypatch)
    monkeypatch.setenv(FACTORY_PIPELINE_ENV, "1")
    work_root = tmp_path / "work-neg"
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(work_root))

    spec = _make_ecommerce_spec()
    project_id = _seed_confirmed_ecommerce_project(factory, spec)

    client = TestClient(app)
    assert client.post("/api/auth/login", json=ALICE_CREDS).status_code == 200

    # Case A：全栈阶段 Docker Runner 连接拒绝 → NOT_RUN + FAILED
    run_id_a = client.post(
        f"/api/projects/{project_id}/runs",
        json={},
        headers={
            "X-CSRF-Token": client.cookies.get("af_csrf"),
            "Idempotency-Key": "fp09-runner-down",
        },
    ).json()["data"]["id"]

    down_runner = _MockDockerRunnerClient(fail_fullstack_with_unreachable=True, work_root=work_root)
    runtime_a = _build_scripted_factory_runtime(
        factory=factory, spec=spec, project_id=project_id, tmp_path=tmp_path
    )
    run_worker(
        session_factory=factory,
        handler=build_generation_handler(factory, runner=down_runner, factory_runtime=runtime_a),
        worker_id="worker-down",
        iterations=1,
        poll_interval=0.01,
    )

    with session_scope(factory) as db:
        run_a = db.get(Run, run_id_a)
        assert run_a is not None
        assert run_a.status == "FAILED"
        assert run_a.error_code == "RUNNER_UNAVAILABLE"

    ver_file = work_root / f"run-{run_id_a}" / "verification.json"
    assert ver_file.is_file()
    ver_data = json.loads(ver_file.read_text(encoding="utf-8"))
    assert ver_data["verified"] is False
    assert ver_data["runtime"]["verify_overall"] == "NOT_RUN"

    # Case B：企图将 LocalProcessModuleVerifier 传入工厂流水线 → 直接 403 FORBIDDEN 落 FAILED
    run_id_b = client.post(
        f"/api/projects/{project_id}/runs",
        json={},
        headers={
            "X-CSRF-Token": client.cookies.get("af_csrf"),
            "Idempotency-Key": "fp09-local-forbidden",
        },
    ).json()["data"]["id"]

    local_verifier = LocalProcessModuleVerifier(work_root=work_root)
    runtime_b = _build_scripted_factory_runtime(
        factory=factory, spec=spec, project_id=project_id, tmp_path=tmp_path
    )
    run_worker(
        session_factory=factory,
        handler=build_generation_handler(
            factory,
            runner=local_verifier,  # type: ignore[arg-type]
            factory_runtime=runtime_b,
        ),
        worker_id="worker-local-block",
        iterations=1,
        poll_interval=0.01,
    )

    with session_scope(factory) as db:
        run_b = db.get(Run, run_id_b)
        assert run_b is not None
        assert run_b.status == "FAILED"
        assert run_b.error_code == "FORBIDDEN"

    for p in (tmp_path / "asset-cache", work_root):
        if p.exists():
            remove_readonly_tree(p)


def test_negative_packaging_secret_scan_blocks_zip_and_excludes_staging_cache_node_modules_target(
    tmp_path: Path,
) -> None:
    """负例：打包前密钥扫描命中即中止且删除暂存 ZIP；合法文件打包时彻底排除 staging/.cache/node_modules/target/.env。"""
    spec = _make_ecommerce_spec()
    lock = resolve_assets(default_registry(), spec.technical_profile.profile_id)
    ok_ver = {
        "overall": "PASS",
        "checks": [
            {"name": "static_manifest", "status": "PASS"},
            {"name": "backend_test", "status": "PASS"},
            {"name": "backend_build", "status": "PASS"},
            {"name": "frontend_build", "status": "PASS"},
        ],
    }
    ok_acc = {"overall": "PASS", "cases": []}
    cost = {
        "currency_unit": "micro_yuan",
        "generation_phase": {"calls": 1, "cost_micro": 100},
        "by_department": {"by_phase": {}},
    }

    # Case A：源码含疑似真实密钥 → 抛 PackagingError("SECRET_DETECTED") 且不残留 source.zip
    dir_leak = tmp_path / "run-leak"
    (dir_leak / "backend").mkdir(parents=True)
    (dir_leak / "backend" / "secret.txt").write_text(
        "API_KEY=sk-live-abcdef1234567890xyz\n", encoding="utf-8"
    )
    with pytest.raises(PackagingError) as exc:
        package_run(
            dir_leak,
            spec=spec,
            lock=lock,
            verification=ok_ver,
            acceptance=ok_acc,
            cost_report=cost,
        )
    assert exc.value.code == "SECRET_DETECTED"
    assert not (dir_leak / "source.zip").exists()

    # Case B：包含 node_modules/、target/、staging/、.cache/、.env → 自动全部排除不入 ZIP
    dir_clean = tmp_path / "run-clean"
    for junk in (
        "frontend/node_modules/pkg/index.js",
        "backend/target/app.jar",
        "staging/backend/auth/tmp.java",
        ".cache/assets/a.txt",
        ".env",
    ):
        p = dir_clean / junk
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("SHOULD_NOT_BE_IN_ZIP\n", encoding="utf-8")
    (dir_clean / "backend" / "App.java").parent.mkdir(parents=True, exist_ok=True)
    (dir_clean / "backend" / "App.java").write_text("class App {}\n", encoding="utf-8")

    res = package_run(
        dir_clean,
        spec=spec,
        lock=lock,
        verification=ok_ver,
        acceptance=ok_acc,
        cost_report=cost,
        include_department_cost_report=True,
    )
    with zipfile.ZipFile(dir_clean / res.zip_path, "r") as zf:
        names = set(zf.namelist())
        assert "backend/App.java" in names
        assert "department-cost-report.json" in names
        for forbidden_dir in ("node_modules", "target", "staging", ".cache", ".env"):
            assert not any(forbidden_dir in Path(n).parts for n in names)


def test_feature_flag_off_keeps_legacy_serial_path_zero_change(
    platform_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：AGENTFORGE_FACTORY_PIPELINE_ENABLED=false 时保持既有串行与 Demo 3 实体范围上限零改动。"""
    app, factory = platform_app
    monkeypatch.setenv(FACTORY_PIPELINE_ENV, "false")
    assert factory_pipeline_enabled() is False

    # 5 实体电商规格在关闭工厂开关时，POST /api/projects/{id}/runs 仍按原 Demo 上限返回 422 UNSUPPORTED_SCOPE
    spec = _make_ecommerce_spec()
    project_id = _seed_confirmed_ecommerce_project(factory, spec)
    client = TestClient(app)
    assert client.post("/api/auth/login", json=ALICE_CREDS).status_code == 200

    resp = client.post(
        f"/api/projects/{project_id}/runs",
        json={},
        headers={
            "X-CSRF-Token": client.cookies.get("af_csrf"),
            "Idempotency-Key": "fp09-flag-off",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "UNSUPPORTED_SCOPE"
