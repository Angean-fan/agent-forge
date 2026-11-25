"""A06：模块与接口契约底座升级（APPROVED_FOR_BUILD、SQL 号段与 Facade Stub 生成）测试。"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from agentforge_platform.agent_files import FileBackend
from agentforge_platform.asset_registry import REPO_ROOT
from agentforge_platform.blueprint_contracts import (
    ArchitectureBlueprint,
    DtoContract,
    DtoField,
    generate_java_facade_stubs,
    validate_blueprint,
)
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.interface_registry import (
    KIND_JAVA_METHOD,
    STATUS_APPROVED_FOR_BUILD,
    STATUS_DRAFT,
    InterfaceRegistry,
    PublicCapability,
    approve_for_build,
    lookup,
)
from agentforge_platform.module_parallel import plan_waves
from agentforge_platform.module_registry import Module, ModuleRegistry
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import content_hash

CAMPUS_SPEC_PATH = REPO_ROOT / "schemas" / "campus-activity.spec.json"
BASE_PKG = "com.agentforge.shop"
PKG_PATH = "com/agentforge/shop"


def _make_ecommerce_spec() -> SoftwareSpecification:
    base = json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8"))
    base["goal"] = "5 部门电商系统：用户认证、商品管理、购物车、订单与模拟支付"
    entities = ["UserAccount", "Product", "CartItem", "PurchaseOrder", "PaymentRecord"]
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
        {"name": "LoginPage", "entity": "UserAccount"},
        {"name": "ProductHomePage", "entity": "Product"},
        {"name": "CartPage", "entity": "CartItem"},
        {"name": "OrderCheckoutPage", "entity": "PurchaseOrder"},
        {"name": "SandboxPayPage", "entity": "PaymentRecord"},
    ]
    base["integrations"] = []
    base["capability_requests"] = []
    return SoftwareSpecification.model_validate(base)


def _cap(
    capability_id: str,
    owner_module_id: str,
    symbol: str,
    signature: str,
    *,
    status: str = STATUS_APPROVED_FOR_BUILD,
    version: int = 1,
    errors: list[str] | None = None,
    auth_constraints: list[str] | None = None,
) -> PublicCapability:
    return PublicCapability(
        capability_id=capability_id,
        owner_module_id=owner_module_id,
        interface_kind=KIND_JAVA_METHOD,
        symbol=symbol,
        signature=signature,
        status=status,
        version=version,
        errors=errors or ["NOT_FOUND"],
        auth_constraints=auth_constraints or ["AUTHENTICATED"],
    )


def _mod(
    *,
    project_id: str,
    spec_hash: str,
    module_id: str,
    name: str,
    owned_paths: list[str],
    test_paths: list[str] | None = None,
    owned_package: str | None = None,
    migration_range: tuple[int, int] | None = None,
    depends_on_modules: list[str] | None = None,
    readable_contract_refs: list[str] | None = None,
) -> Module:
    return Module(
        project_id=project_id,
        module_id=module_id,
        name=name,
        based_on_spec_hash=spec_hash,
        profile_ref="java-spring-vue@1.0.0",
        owned_paths=owned_paths,
        test_paths=test_paths or [],
        owned_package=owned_package,
        migration_range=migration_range,
        depends_on_modules=depends_on_modules or [],
        readable_contract_refs=readable_contract_refs or [],
    )


def _make_ecommerce_blueprint(spec: SoftwareSpecification) -> ArchitectureBlueprint:
    project_id = "proj-ecommerce-5dept"
    spec_hash = content_hash(spec)

    modules = [
        _mod(
            project_id=project_id,
            spec_hash=spec_hash,
            module_id="auth",
            name="用户认证部",
            owned_paths=[
                f"backend/src/main/java/{PKG_PATH}/modules/auth/internal/",
                "backend/src/main/resources/db/migration/V100__auth_user.sql",
            ],
            test_paths=[f"backend/src/test/java/{PKG_PATH}/modules/auth/"],
            owned_package=f"{BASE_PKG}.modules.auth",
            migration_range=(100, 199),
        ),
        _mod(
            project_id=project_id,
            spec_hash=spec_hash,
            module_id="product",
            name="商品目录部",
            owned_paths=[
                f"backend/src/main/java/{PKG_PATH}/modules/product/internal/",
                "backend/src/main/resources/db/migration/V200__product_catalog.sql",
            ],
            test_paths=[f"backend/src/test/java/{PKG_PATH}/modules/product/"],
            owned_package=f"{BASE_PKG}.modules.product",
            migration_range=(200, 299),
        ),
        _mod(
            project_id=project_id,
            spec_hash=spec_hash,
            module_id="cart",
            name="购物车部",
            owned_paths=[
                f"backend/src/main/java/{PKG_PATH}/modules/cart/internal/",
                "backend/src/main/resources/db/migration/V300__cart_item.sql",
            ],
            test_paths=[f"backend/src/test/java/{PKG_PATH}/modules/cart/"],
            owned_package=f"{BASE_PKG}.modules.cart",
            migration_range=(300, 399),
            depends_on_modules=["auth", "product"],
            readable_contract_refs=["auth.get_user_summary", "product.get_product_snapshot"],
        ),
        _mod(
            project_id=project_id,
            spec_hash=spec_hash,
            module_id="order",
            name="订单履约部",
            owned_paths=[
                f"backend/src/main/java/{PKG_PATH}/modules/order/internal/",
                "backend/src/main/resources/db/migration/V400__purchase_order.sql",
            ],
            test_paths=[f"backend/src/test/java/{PKG_PATH}/modules/order/"],
            owned_package=f"{BASE_PKG}.modules.order",
            migration_range=(400, 499),
            depends_on_modules=["auth", "product", "cart"],
            readable_contract_refs=[
                "auth.get_user_summary",
                "product.get_product_snapshot",
                "product.reserve_stock",
                "cart.list_cart_items",
            ],
        ),
        _mod(
            project_id=project_id,
            spec_hash=spec_hash,
            module_id="payment",
            name="模拟支付部",
            owned_paths=[
                f"backend/src/main/java/{PKG_PATH}/modules/payment/internal/",
                "backend/src/main/resources/db/migration/V500__payment_record.sql",
            ],
            test_paths=[f"backend/src/test/java/{PKG_PATH}/modules/payment/"],
            owned_package=f"{BASE_PKG}.modules.payment",
            migration_range=(500, 599),
            depends_on_modules=["order"],
            readable_contract_refs=["order.get_order_payable"],
        ),
    ]

    shared_dtos = [
        DtoContract(
            name="UserSummaryDto",
            owner_module_id="auth",
            fields=[
                DtoField(name="userId", java_type="Long"),
                DtoField(name="username", java_type="String"),
                DtoField(name="role", java_type="String"),
            ],
        ),
        DtoContract(
            name="ProductSnapshotDto",
            owner_module_id="product",
            fields=[
                DtoField(name="productId", java_type="Long"),
                DtoField(name="title", java_type="String"),
                DtoField(name="unitPrice", java_type="BigDecimal"),
                DtoField(name="stock", java_type="Integer"),
            ],
        ),
        DtoContract(
            name="CartItemDto",
            owner_module_id="cart",
            fields=[
                DtoField(name="itemId", java_type="Long"),
                DtoField(name="productId", java_type="Long"),
                DtoField(name="quantity", java_type="Integer"),
                DtoField(name="snapshot", java_type="ProductSnapshotDto"),
            ],
        ),
        DtoContract(
            name="OrderPayableDto",
            owner_module_id="order",
            fields=[
                DtoField(name="orderId", java_type="Long"),
                DtoField(name="userId", java_type="Long"),
                DtoField(name="totalAmount", java_type="BigDecimal"),
                DtoField(name="status", java_type="String"),
            ],
        ),
        DtoContract(
            name="PaymentReceiptDto",
            owner_module_id="payment",
            fields=[
                DtoField(name="paymentId", java_type="Long"),
                DtoField(name="orderId", java_type="Long"),
                DtoField(name="paidAmount", java_type="BigDecimal"),
                DtoField(name="paidAt", java_type="Instant"),
                DtoField(name="sandboxMode", java_type="Boolean"),
            ],
        ),
    ]

    frozen_contracts = [
        _cap(
            "auth.get_user_summary",
            "auth",
            "AuthFacade.getUserSummary",
            "UserSummaryDto getUserSummary(Long userId)",
        ),
        _cap(
            "product.get_product_snapshot",
            "product",
            "ProductFacade.getProductSnapshot",
            "ProductSnapshotDto getProductSnapshot(Long productId)",
        ),
        _cap(
            "product.reserve_stock",
            "product",
            "ProductFacade.reserveStock",
            "Boolean reserveStock(Long productId, Integer quantity)",
            errors=["NOT_FOUND", "OUT_OF_STOCK"],
        ),
        _cap(
            "cart.list_cart_items",
            "cart",
            "CartFacade.listCartItems",
            "List<CartItemDto> listCartItems(Long userId)",
        ),
        _cap(
            "order.get_order_payable",
            "order",
            "OrderFacade.getOrderPayable",
            "OrderPayableDto getOrderPayable(Long orderId)",
        ),
        _cap(
            "payment.execute_sandbox_pay",
            "payment",
            "PaymentFacade.executeSandboxPay",
            "PaymentReceiptDto executeSandboxPay(Long orderId, BigDecimal amount)",
            errors=["ORDER_NOT_FOUND", "INVALID_AMOUNT"],
        ),
    ]

    return ArchitectureBlueprint(
        project_id="proj-ecommerce-5dept",
        based_on_spec_hash=spec_hash,
        profile_ref="java-spring-vue@1.0.0",
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
    )


def test_interface_registry_approved_for_build_lifecycle_and_backward_compat() -> None:
    registry = InterfaceRegistry(project_id="p1", based_on_spec_hash="h1")
    cap = _cap(
        "product.get_snapshot",
        "product",
        "ProductFacade.getSnapshot",
        "ProductSnapshotDto getSnapshot(Long productId)",
        status=STATUS_DRAFT,
    )

    frozen = approve_for_build(registry, cap)
    assert len(frozen.capabilities) == 1
    assert frozen.capabilities[0].status == STATUS_APPROVED_FOR_BUILD
    assert frozen.capabilities[0].verification_ref is None

    # 向后兼容：默认 lookup 仍只查 VERIFIED，不破坏 F03 契约语义
    assert lookup(frozen, "product.get_snapshot") is None
    # 显式开启 include_approved 时可检索开工前冻结契约
    found = lookup(frozen, "product.get_snapshot", include_approved=True)
    assert found is not None
    assert found.status == STATUS_APPROVED_FOR_BUILD

    # 跨模块覆盖被拒绝
    hijack = cap.model_copy(update={"owner_module_id": "order"})
    with pytest.raises(AppException) as exc_info:
        approve_for_build(frozen, hijack)
    assert exc_info.value.code == ErrorCode.FORBIDDEN

    # 未授权破坏性签名变更被阻塞
    breaking = cap.model_copy(update={"signature": "String getSnapshot(String code)"})
    with pytest.raises(AppException) as exc_info:
        approve_for_build(frozen, breaking)
    assert exc_info.value.code == ErrorCode.INTERFACE_CONFLICT


def test_5_department_ecommerce_blueprint_validates_and_generates_compilable_facade_stubs(
    tmp_path: Path,
) -> None:
    spec = _make_ecommerce_spec()
    blueprint = _make_ecommerce_blueprint(spec)

    issues = validate_blueprint(blueprint, spec)
    assert issues == []

    # 生成只读 Java Facade & DTO 骨架（含跨包 import 如 BigDecimal / Instant / List / 跨模块 DTO）
    generated = generate_java_facade_stubs(blueprint, tmp_path)
    assert generated == blueprint.facade_stub_paths()
    assert len(generated) == 10  # 5 个 Facade + 5 个 DTO

    # 二次重复生成具备字节级确定性
    before_snapshots = {rel: (tmp_path / rel).read_bytes() for rel in generated}
    generated_again = generate_java_facade_stubs(blueprint, tmp_path)
    assert generated_again == generated
    for rel in generated:
        file_path = tmp_path / rel
        assert file_path.read_bytes() == before_snapshots[rel]
        # 文件权限已设为只读（无写位）
        mode = stat.S_IMODE(file_path.stat().st_mode)
        assert (mode & stat.S_IWUSR) == 0

    # 验证跨包 import 自动补齐
    cart_dto_src = (
        tmp_path / f"backend/src/main/java/{PKG_PATH}/modules/cart/api/CartItemDto.java"
    ).read_text(encoding="utf-8")
    assert f"import {BASE_PKG}.modules.product.api.ProductSnapshotDto;" in cart_dto_src

    payment_facade_src = (
        tmp_path / f"backend/src/main/java/{PKG_PATH}/modules/payment/api/PaymentFacade.java"
    ).read_text(encoding="utf-8")
    assert "import java.math.BigDecimal;" in payment_facade_src
    assert "PaymentReceiptDto executeSandboxPay(Long orderId, BigDecimal amount);" in payment_facade_src

    # 若本机装有 javac，执行真实编译验证（保证骨架 100% 合法可编译）
    javac = shutil.which("javac")
    if javac is not None:
        out_dir = tmp_path / "compiled-classes"
        out_dir.mkdir()
        java_files = [str(tmp_path / rel) for rel in generated]
        proc = subprocess.run(
            [javac, "-d", str(out_dir), *java_files],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"javac 编译生成的 Facade Stub 失败：\n{proc.stderr}"
        assert (out_dir / PKG_PATH / "modules/payment/api/PaymentFacade.class").is_file()
        assert (out_dir / PKG_PATH / "modules/cart/api/CartItemDto.class").is_file()


def test_generated_facade_stubs_are_protected_in_file_backend(tmp_path: Path) -> None:
    spec = _make_ecommerce_spec()
    blueprint = _make_ecommerce_blueprint(spec)
    generated = generate_java_facade_stubs(blueprint, tmp_path)
    target_facade = next(p for p in generated if p.endswith("ProductFacade.java"))

    # 即使 FileBackend 给了整个 backend/ 范围，仍然拒绝单部门修改 modules/<dept>/api/*.java
    backend = FileBackend(tmp_path, allowed_paths=["backend"])
    assert backend.read(target_facade)  # 只读允许

    with pytest.raises(AppException) as exc_info:
        backend.write(target_facade, b"// tampered facade\n")
    assert exc_info.value.code == ErrorCode.FORBIDDEN
    assert "受保护路径不可修改" in exc_info.value.message

    # 但部门自己的 internal 目录仍允许正常写入
    internal_file = f"backend/src/main/java/{PKG_PATH}/modules/product/internal/ProductService.java"
    backend.write(internal_file, b"package com.agentforge.shop.modules.product.internal;\n")


def test_module_parallel_respects_disjoint_migration_range_and_preserves_legacy_mutex() -> None:
    # 互不重叠的 migration_range：auth(100-199) 与 product(200-299) 可同波并行写各自迁移脚本
    disjoint_reg = ModuleRegistry(
        project_id="p1",
        based_on_spec_hash="h1",
        profile_ref="java-spring-vue@1.0.0",
        modules=[
            _mod(
                project_id="p1",
                spec_hash="h1",
                module_id="auth",
                name="认证",
                owned_paths=["backend/src/main/resources/db/migration/V100__auth.sql"],
                migration_range=(100, 199),
            ),
            _mod(
                project_id="p1",
                spec_hash="h1",
                module_id="product",
                name="商品",
                owned_paths=["backend/src/main/resources/db/migration/V200__product.sql"],
                migration_range=(200, 299),
            ),
        ],
    )
    plan_disjoint = plan_waves(disjoint_reg)
    assert plan_disjoint.waves == (("auth", "product"),)
    assert plan_disjoint.parallel is True

    # 未声明 migration_range 的旧模块（或重叠号段）：仍保持互斥串行
    legacy_reg = ModuleRegistry(
        project_id="p1",
        based_on_spec_hash="h1",
        profile_ref="java-spring-vue@1.0.0",
        modules=[
            _mod(
                project_id="p1",
                spec_hash="h1",
                module_id="auth",
                name="认证",
                owned_paths=["backend/src/main/resources/db/migration/V2__auth.sql"],
            ),
            _mod(
                project_id="p1",
                spec_hash="h1",
                module_id="product",
                name="商品",
                owned_paths=["backend/src/main/resources/db/migration/V3__product.sql"],
            ),
        ],
    )
    plan_legacy = plan_waves(legacy_reg)
    assert plan_legacy.parallel is False
    assert plan_legacy.waves == (("auth",), ("product",))
    assert any("共享资源冲突" in reason for reason in plan_legacy.serialized_reasons)

    # 重叠的 migration_range 同样被防御性串行化
    overlap_reg = ModuleRegistry(
        project_id="p1",
        based_on_spec_hash="h1",
        profile_ref="java-spring-vue@1.0.0",
        modules=[
            _mod(
                project_id="p1",
                spec_hash="h1",
                module_id="auth",
                name="认证",
                owned_paths=["backend/src/main/resources/db/migration/V100__auth.sql"],
                migration_range=(100, 220),
            ),
            _mod(
                project_id="p1",
                spec_hash="h1",
                module_id="product",
                name="商品",
                owned_paths=["backend/src/main/resources/db/migration/V200__product.sql"],
                migration_range=(200, 299),
            ),
        ],
    )
    plan_overlap = plan_waves(overlap_reg)
    assert plan_overlap.parallel is False
    assert plan_overlap.waves == (("auth",), ("product",))


def test_negative_1_directory_and_api_path_overlap_rejected() -> None:
    spec = _make_ecommerce_spec()
    bp = _make_ecommerce_blueprint(spec)

    # 1a. 两个部门的 owned_paths 目录重叠
    modules_overlap = list(bp.modules)
    modules_overlap[1] = modules_overlap[1].model_copy(
        update={
            "owned_paths": [
                *modules_overlap[1].owned_paths,
                f"backend/src/main/java/{PKG_PATH}/modules/auth/internal/Helper.java",
            ]
        }
    )
    issues = validate_blueprint(bp.model_copy(update={"modules": modules_overlap}), spec)
    assert any("唯一写入权冲突" in item for item in issues)

    # 1b. 部门试图将只读 Facade Stub 目录声明进可写 owned_paths
    modules_api = list(bp.modules)
    modules_api[0] = modules_api[0].model_copy(
        update={
            "owned_paths": [
                *modules_api[0].owned_paths,
                f"backend/src/main/java/{PKG_PATH}/modules/auth/api/",
            ]
        }
    )
    api_issues = validate_blueprint(bp.model_copy(update={"modules": modules_api}), spec)
    assert any("禁止将只读 Facade Stub 路径纳入可写 owned_paths" in item for item in api_issues)


def test_negative_2_sql_migration_range_overlap_and_bounds_rejected() -> None:
    spec = _make_ecommerce_spec()
    bp = _make_ecommerce_blueprint(spec)

    # 2a. auth(100-250) 与 product(200-299) SQL 号段重叠
    modules_overlap = list(bp.modules)
    modules_overlap[0] = modules_overlap[0].model_copy(update={"migration_range": (100, 250)})
    issues = validate_blueprint(bp.model_copy(update={"modules": modules_overlap}), spec)
    assert any("SQL 号段重叠" in item for item in issues)

    # 2b. 侵占底座保留区 V001..V009 或跨表外键保留区 V900+
    modules_oob = list(bp.modules)
    modules_oob[0] = modules_oob[0].model_copy(update={"migration_range": (1, 99)})
    modules_oob[4] = modules_oob[4].model_copy(update={"migration_range": (850, 920)})
    oob_issues = validate_blueprint(bp.model_copy(update={"modules": modules_oob}), spec)
    assert any("超出允许范围" in item and "auth" in item for item in oob_issues)
    assert any("超出允许范围" in item and "payment" in item for item in oob_issues)


def test_negative_3_ambiguous_signature_rejected(tmp_path: Path) -> None:
    spec = _make_ecommerce_spec()
    bp = _make_ecommerce_blueprint(spec)

    bad_contracts = list(bp.frozen_contracts)
    bad_contracts[0] = bad_contracts[0].model_copy(
        update={"signature": "UserSummaryDto getUserSummary(TODO userId)"}
    )
    bad_contracts[1] = bad_contracts[1].model_copy(
        update={"signature": "List<*> getProductSnapshot(Long productId)"}
    )
    bad_bp = bp.model_copy(update={"frozen_contracts": bad_contracts})

    issues = validate_blueprint(bad_bp, spec)
    assert any("含模糊签名" in item and "auth.get_user_summary" in item for item in issues)
    assert any("含模糊签名" in item and "product.get_product_snapshot" in item for item in issues)

    # 生成器同样拒绝模糊签名，绝不落盘非法 Java 代码
    with pytest.raises(AppException) as exc_info:
        generate_java_facade_stubs(bad_bp, tmp_path)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


def test_negative_4_dependency_cycle_rejected() -> None:
    spec = _make_ecommerce_spec()
    bp = _make_ecommerce_blueprint(spec)

    # 制造 cart <-> order 显式模块依赖环
    modules_cycle = list(bp.modules)
    modules_cycle[2] = modules_cycle[2].model_copy(
        update={"depends_on_modules": ["auth", "product", "order"]}
    )
    issues = validate_blueprint(bp.model_copy(update={"modules": modules_cycle}), spec)
    assert any("存在环" in item for item in issues)


def test_negative_5_undeclared_cross_module_dependency_and_missing_coverage_rejected() -> None:
    spec = _make_ecommerce_spec()
    bp = _make_ecommerce_blueprint(spec)

    # 5a. order 引用了 product 的契约，但未在 depends_on_modules 中声明依赖 product
    modules_undeclared = list(bp.modules)
    modules_undeclared[3] = modules_undeclared[3].model_copy(
        update={"depends_on_modules": ["auth", "cart"]}
    )
    issues = validate_blueprint(bp.model_copy(update={"modules": modules_undeclared}), spec)
    assert any(
        "未声明的跨模块依赖" in item and "order" in item and "product" in item
        for item in issues
    )

    # 5b. 遗漏冻结业务实体 PaymentRecord
    incomplete_bp = bp.model_copy(
        update={
            "covered_entities": {
                "auth": ["UserAccount"],
                "product": ["Product"],
                "cart": ["CartItem"],
                "order": ["PurchaseOrder"],
            }
        }
    )
    cov_issues = validate_blueprint(incomplete_bp, spec)
    assert any("未覆盖全部冻结业务实体" in item and "PaymentRecord" in item for item in cov_issues)


def test_blueprint_prompt_contract_lists_every_field() -> None:
    """总管提示词内联的字段契约必须覆盖全部 pydantic 模型字段（防止新增字段后模型再次猜键名）。"""
    from agentforge_platform.blueprint_contracts import blueprint_prompt_contract
    from agentforge_platform.interface_registry import PublicCapability
    from agentforge_platform.module_registry import Module

    text = blueprint_prompt_contract()
    for model in (ArchitectureBlueprint, Module, PublicCapability, DtoContract, DtoField):
        assert f"{model.__name__}:" in text
        for field_name in model.model_fields:
            assert f"- {field_name}:" in text, f"{model.__name__}.{field_name} 不在提示词契约中"
    assert "extra=forbid" in text
