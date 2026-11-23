"""FP03：只读后端公共底座装配与跨模块 api/*Facade.java 骨架自动生成测试。"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

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
from agentforge_platform.factory_base_builder import (
    READONLY_MANIFEST_NAME,
    assert_base_integrity,
    build_backend_base,
    check_department_paths_against_base,
    load_base_manifest,
    verify_base_integrity,
)
from agentforge_platform.interface_registry import (
    KIND_JAVA_METHOD,
    STATUS_APPROVED_FOR_BUILD,
    PublicCapability,
)
from agentforge_platform.module_registry import Module
from agentforge_platform.resolver import AssetLock, resolve_assets
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


def _make_ecommerce_spec() -> SoftwareSpecification:
    base = json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8"))
    base["goal"] = "电商沙箱 5 部门后端骨架：登录注册、商品目录、购物车、订单与模拟支付"
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
    base["pages"] = [{"name": f"{name}Page", "entity": name} for name in entities]
    base["integrations"] = []
    base["capability_requests"] = []
    return SoftwareSpecification.model_validate(base)


def _make_5_dept_blueprint(spec: SoftwareSpecification) -> ArchitectureBlueprint:
    spec_hash = content_hash(spec)
    profile_ref = f"{spec.technical_profile.profile_id}@{spec.technical_profile.profile_version}"
    pid = "proj-ecommerce-fp03"

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
        _m("product", "商品目录部", "V200__product_catalog.sql", (200, 299)),
        _m(
            "cart",
            "购物车部",
            "V300__cart_item.sql",
            (300, 399),
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
                DtoField(name="snapshot", java_type="ProductSnapshotDto"),
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
                DtoField(name="paidAt", java_type="Instant"),
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
    ]

    bp = ArchitectureBlueprint(
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
    )
    assert validate_blueprint(bp, spec) == []
    return bp


def _seed_sqlite_materializer(tmp_path: Path) -> tuple[object, AssetMaterializer, AssetLock]:
    url = f"sqlite+pysqlite:///{tmp_path / 'fp03_assets.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        PgAssetStore(db).sync_from_filesystem()

    session = factory()
    materializer = AssetMaterializer(session, cache_root=tmp_path / "cache")
    lock = resolve_assets(default_registry(), "demo-ts-mp")
    return (engine, session), materializer, lock


def test_build_backend_base_5_department_ecommerce_deterministic_and_readonly(
    tmp_path: Path,
) -> None:
    handles, materializer, lock = _seed_sqlite_materializer(tmp_path)
    engine, session = handles
    try:
        spec = _make_ecommerce_spec()
        bp = _make_5_dept_blueprint(spec)

        run_dir_1 = tmp_path / "run-1"
        res_1 = build_backend_base(run_dir_1, bp, lock, materializer)

        base_dir = Path(res_1.base_dir)
        assert base_dir == (run_dir_1 / "backend_base").resolve()
        assert (base_dir / "backend" / "pom.xml").is_file()
        assert (
            base_dir / f"backend/src/main/java/{PKG_PATH}/common/ApiResponse.java"
        ).is_file()
        assert (
            base_dir / f"backend/src/main/java/{PKG_PATH}/security/JwtService.java"
        ).is_file()
        assert (
            base_dir / "backend/src/main/resources/db/migration/V1__create_app_user.sql"
        ).is_file()
        # 绝不生成与 V1__create_app_user.sql 版本号冲突的 V001__base.sql
        assert not (
            base_dir / "backend/src/main/resources/db/migration/V001__base.sql"
        ).exists()

        # 5 部门 Facade 与 5 份共享 DTO 全部落盘
        assert len(res_1.facade_stub_paths) == 5
        assert len(res_1.shared_dto_paths) == 5
        for dept, cls_name in (
            ("auth", "AuthFacade"),
            ("product", "ProductFacade"),
            ("cart", "CartFacade"),
            ("order", "OrderFacade"),
            ("payment", "PaymentFacade"),
        ):
            rel = f"backend/src/main/java/{PKG_PATH}/modules/{dept}/api/{cls_name}.java"
            assert rel in res_1.facade_stub_paths
            fpath = base_dir / rel
            assert fpath.is_file()
            # 文件必须设为只读（无写位）
            assert not (fpath.stat().st_mode & stat.S_IWUSR)

        # 清单文件存在且全量 SHA-256 校验通过
        manifest_path = Path(res_1.manifest_path)
        assert manifest_path.name == READONLY_MANIFEST_NAME
        assert manifest_path.is_file()
        manifest = load_base_manifest(manifest_path)
        assert manifest.manifest_hash == res_1.manifest_hash
        assert verify_base_integrity(base_dir, manifest_path) == []

        # 第二次确定性构建产出完全一致的 manifest_hash（0 模型调用、零漂移）
        run_dir_2 = tmp_path / "run-2"
        res_2 = build_backend_base(run_dir_2, bp, lock, materializer)
        assert res_2.manifest_hash == res_1.manifest_hash
        assert res_2.facade_stub_paths == res_1.facade_stub_paths
    finally:
        session.close()
        engine.dispose()
        for d in (tmp_path / "run-1", tmp_path / "run-2", tmp_path / "cache"):
            if d.exists():
                remove_readonly_tree(d)


def test_base_integrity_detects_tampering_deletion_and_rogue_api_files(
    tmp_path: Path,
) -> None:
    handles, materializer, lock = _seed_sqlite_materializer(tmp_path)
    engine, session = handles
    try:
        spec = _make_ecommerce_spec()
        bp = _make_5_dept_blueprint(spec)
        res = build_backend_base(tmp_path / "run-tamper", bp, lock, materializer)
        manifest = load_base_manifest(res.manifest_path)

        # 复制一份可写合并视图，模拟总装校验
        merged = tmp_path / "merged"
        shutil.copytree(res.base_dir, merged)
        make_writable_tree(merged)
        assert_base_integrity(merged, manifest)

        # 1. 篡改他部门 api/ProductFacade.java 签名 → 必须拦截
        facade = merged / f"backend/src/main/java/{PKG_PATH}/modules/product/api/ProductFacade.java"
        original_facade = facade.read_text(encoding="utf-8")
        facade.write_text(original_facade + "\n// tampered by cart dept\n", encoding="utf-8")
        with pytest.raises(AppException) as exc:
            assert_base_integrity(merged, manifest)
        assert exc.value.code == ErrorCode.CONTRACT_CONFLICT
        assert "ProductFacade.java" in exc.value.message
        facade.write_text(original_facade, encoding="utf-8")
        assert_base_integrity(merged, manifest)

        # 2. 篡改底座 pom.xml → 必须拦截
        pom = merged / "backend/pom.xml"
        original_pom = pom.read_text(encoding="utf-8")
        pom.write_text(original_pom + "<!-- tampered -->\n", encoding="utf-8")
        violations_pom = verify_base_integrity(merged, manifest)
        assert any("backend/pom.xml" in v for v in violations_pom)
        pom.write_text(original_pom, encoding="utf-8")

        # 3. 删除底座安全文件 → 必须拦截
        jwt_file = merged / f"backend/src/main/java/{PKG_PATH}/security/JwtService.java"
        jwt_bytes = jwt_file.read_bytes()
        jwt_file.unlink()
        violations_del = verify_base_integrity(merged, manifest)
        assert any("JwtService.java" in v and "缺失" in v for v in violations_del)
        jwt_file.write_bytes(jwt_bytes)

        # 4. 在 modules/product/api/ 私自新增未冻结 Facade → 必须拦截
        rogue = merged / f"backend/src/main/java/{PKG_PATH}/modules/product/api/RogueFacade.java"
        rogue.write_text("package com.agentforge.template.modules.product.api;\npublic interface RogueFacade {}\n", encoding="utf-8")
        violations_rogue = verify_base_integrity(merged, manifest)
        assert any("RogueFacade.java" in v for v in violations_rogue)
        rogue.unlink()

        # 5. 路径级快速拦截检查
        bad_changes = check_department_paths_against_base(
            [
                "backend/pom.xml",
                f"backend/src/main/java/{PKG_PATH}/modules/product/api/ProductFacade.java",
                f"backend/src/main/java/{PKG_PATH}/modules/order/internal/OrderService.java",
                "backend/src/main/resources/db/migration/V400__purchase_order.sql",
            ],
            manifest,
        )
        assert len(bad_changes) == 2
        assert any("pom.xml" in msg for msg in bad_changes)
        assert any("ProductFacade.java" in msg for msg in bad_changes)
    finally:
        session.close()
        engine.dispose()
        for d in (tmp_path / "run-tamper", tmp_path / "cache"):
            if d.exists():
                remove_readonly_tree(d)


def test_negative_tampered_lock_and_wrong_phase_rejected(tmp_path: Path) -> None:
    handles, materializer, lock = _seed_sqlite_materializer(tmp_path)
    engine, session = handles
    try:
        spec = _make_ecommerce_spec()
        bp = _make_5_dept_blueprint(spec)

        tampered_lock = lock.model_copy(update={"lock_hash": "0" * 64})
        with pytest.raises(AppException) as exc:
            build_backend_base(tmp_path / "run-bad-lock", bp, tampered_lock, materializer)
        assert exc.value.code == ErrorCode.ASSET_CHECKSUM_MISMATCH

        frontend_bp = bp.model_copy(update={"phase": "frontend"})
        with pytest.raises(AppException) as exc_fe:
            build_backend_base(tmp_path / "run-fe", frontend_bp, lock, materializer)
        assert exc_fe.value.code == ErrorCode.VALIDATION_ERROR
    finally:
        session.close()
        engine.dispose()
        if (tmp_path / "cache").exists():
            remove_readonly_tree(tmp_path / "cache")


def test_maven_test_compile_on_pure_skeleton_when_mvn_available(tmp_path: Path) -> None:
    """若本机具备 Maven/JDK，直接在纯骨架目录验证 ``mvn test-compile`` 通过；无环境标 NOT_RUN。"""
    mvn = shutil.which("mvn")
    java = shutil.which("java")
    if not mvn or not java:
        pytest.skip("NOT_RUN: 本机未安装 Maven 或 JDK，跳过骨架 mvn test-compile 验证")

    handles, materializer, lock = _seed_sqlite_materializer(tmp_path)
    engine, session = handles
    try:
        spec = _make_ecommerce_spec()
        bp = _make_5_dept_blueprint(spec)
        res = build_backend_base(tmp_path / "run-mvn", bp, lock, materializer)

        # 先尝试离线 -o（利用本机 ~/.m2 缓存）；若缺缓存再回退标准在线编译
        cmd_offline = [mvn, "-B", "-o", "test-compile"]
        proc = subprocess.run(
            cmd_offline,
            cwd=res.backend_project_dir,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if proc.returncode != 0 and (
            "offline mode" in proc.stdout.lower()
            or "not been downloaded" in proc.stdout.lower()
            or "non-resolvable parent pom" in proc.stdout.lower()
        ):
            cmd_online = [mvn, "-B", "test-compile"]
            proc = subprocess.run(
                cmd_online,
                cwd=res.backend_project_dir,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            if proc.returncode != 0 and (
                "unknownhostexception" in proc.stdout.lower()
                or "could not transfer artifact" in proc.stdout.lower()
            ):
                pytest.skip("NOT_RUN: 本机 Maven 本地仓库缺失依赖且无法联网下载，跳过 mvn test-compile")

        assert proc.returncode == 0, f"mvn test-compile 失败：\n{proc.stdout[-2000:]}\n{proc.stderr[-1000:]}"

        target_dir = Path(res.backend_project_dir) / "target"
        classes_dir = target_dir / "classes" / PKG_PATH
        test_classes_dir = target_dir / "test-classes" / PKG_PATH
        assert (classes_dir / "TemplateApplication.class").is_file()
        assert (classes_dir / "common" / "ApiResponse.class").is_file()
        assert (test_classes_dir / "HealthTest.class").is_file()
        for dept, cls_name, dto_name in (
            ("auth", "AuthFacade", "UserSummaryDto"),
            ("product", "ProductFacade", "ProductSnapshotDto"),
            ("cart", "CartFacade", "CartItemDto"),
            ("order", "OrderFacade", "OrderPayableDto"),
            ("payment", "PaymentFacade", "PaymentReceiptDto"),
        ):
            assert (classes_dir / "modules" / dept / "api" / f"{cls_name}.class").is_file()
            assert (classes_dir / "modules" / dept / "api" / f"{dto_name}.class").is_file()

        # 编译完成后原只读底座仍保持零篡改
        assert verify_base_integrity(res.base_dir, res.manifest_path) == []
    finally:
        session.close()
        engine.dispose()
        for d in (tmp_path / "run-mvn", tmp_path / "cache"):
            if d.exists():
                remove_readonly_tree(d)


def test_live_postgres_materialize_and_build_backend_base(tmp_path: Path) -> None:
    """实连本地/隧道 PostgreSQL：从 PG 物化模板生成只读底座，验证与 SQLite/Git 逐字节一致。"""
    pg_url = (
        read_local_env_var("AGENTFORGE_TEST_DATABASE_URL")
        or read_local_env_var("AGENTFORGE_DATABASE_URL")
    )
    if not pg_url or not postgres_reachable(pg_url):
        pytest.skip("NOT_RUN: 本地 PostgreSQL 或云端隧道未连接，跳过实库 PG FP03 验证")

    ensure_postgres_database(pg_url)
    schema = f"test_fp03_{uuid.uuid4().hex[:8]}"
    ensure_postgres_schema(pg_url, schema)
    scoped_url = isolated_schema_url(pg_url, schema)

    try:
        command.upgrade(_alembic_config(scoped_url), "head")
        engine = make_engine(scoped_url)
        factory = make_session_factory(engine)
        try:
            with session_scope(factory) as db:
                PgAssetStore(db).sync_from_filesystem()

            spec = _make_ecommerce_spec()
            bp = _make_5_dept_blueprint(spec)
            lock = resolve_assets(default_registry(), "demo-ts-mp")

            session = factory()
            try:
                pg_mat = AssetMaterializer(session, cache_root=tmp_path / "pg-cache")
                res_pg = build_backend_base(tmp_path / "run-pg", bp, lock, pg_mat)
            finally:
                session.close()

            # 与本地 SQLite 同步路径产出逐字节一致的 manifest_hash
            handles, sqlite_mat, _ = _seed_sqlite_materializer(tmp_path / "sqlite-side")
            sq_engine, sq_session = handles
            try:
                res_sq = build_backend_base(tmp_path / "run-sq", bp, lock, sqlite_mat)
            finally:
                sq_session.close()
                sq_engine.dispose()

            assert res_pg.manifest_hash == res_sq.manifest_hash
            assert verify_base_integrity(res_pg.base_dir, res_pg.manifest_path) == []
        finally:
            engine.dispose()
    finally:
        drop_postgres_schema(pg_url, schema)
        for sub in ("pg-cache", "run-pg", "run-sq", "sqlite-side"):
            p = tmp_path / sub
            if p.exists():
                remove_readonly_tree(p)
