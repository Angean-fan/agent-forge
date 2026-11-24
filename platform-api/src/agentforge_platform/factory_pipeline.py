"""FP01 / FP09：异步工厂流水线特性开关、全栈总装、强制 Docker Runner 端到端验收与工厂打包编排器（``factory_pipeline.py``）。

完整流水线闭环（当 ``AGENTFORGE_FACTORY_PIPELINE_ENABLED=1`` 时由 ``worker.py`` / ``run_generation()`` 顶层分发）：
1. 领取/恢复与取消边界检查（含崩溃未结算账本 ``NEEDS_RECONCILIATION`` 阻断）；
2. 后端总管冻结蓝图 → 只读后端底座 + Facade 骨架 → 后端多部门异步并发 → 后端总装 + 强制 Docker Runner 验收 → 冻结权威 ``backend/openapi.json``；
3. 前端总管（以权威 ``openapi.json`` 为唯一输入）冻结前端蓝图 → 只读前端底座 + TS 契约类型 → 前端多部门异步并发 → 声明式路由自动聚合 ``generated-routes.ts`` + 全量构建验证；
4. 全栈总装（合并 ``backend/`` + ``frontend/`` + ``compose.yaml`` + ``README.md`` + ``.env.example`` + ``backend/openapi.json`` + ``factory-blueprint.json``；全部中间暂存位于 ``staging/`` 永不进入 ZIP）；
5. **强制独立 Docker Runner 全栈构建与端到端验收**（严禁降级为 ``LocalProcessModuleVerifier``；缺 Docker Runner 明确标 ``NOT_RUN``，绝不标 ``SUCCEEDED``）；
6. 全量密钥扫描 + 排除 ``node_modules/``、``target/``、``staging/``、``.cache/`` + 打包 ``source.zip``（含 ``backend/openapi.json``、``factory-blueprint.json``、``cost-report.json``、``department-cost-report.json``、``verification.json``、``EXTERNAL_SERVICES.md``），并在数据库中登记可下载 ZIP 产物。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform.asset_cache import (
    AssetMaterializer,
    default_cache_root,
    make_writable_tree,
)
from agentforge_platform.asset_store_pg import PgAssetStore
from agentforge_platform.blueprint_contracts import ArchitectureBlueprint
from agentforge_platform.contract import BusinessContract, ContractError, compile_from_spec
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.factory_backend_stage import BackendStageResult, run_backend_factory_stage
from agentforge_platform.factory_base_builder import build_backend_base
from agentforge_platform.factory_chief import ChiefArchitect
from agentforge_platform.factory_frontend_base import build_frontend_base, canonical_openapi_sha256
from agentforge_platform.factory_frontend_stage import (
    FrontendStageResult,
    run_frontend_factory_stage,
)
from agentforge_platform.factory_scheduler import AsyncDepartmentPool
from agentforge_platform.factory_state import (
    FactoryCancelledError,
    FactoryPhase,
    FactoryStateStore,
)
from agentforge_platform.method_lock import resolve_lock
from agentforge_platform.models import Run, SpecVersion
from agentforge_platform.module_verifier import (
    ModuleVerifier,
    build_module_verifier,
    require_docker_runner_for_final_acceptance,
    run_final_acceptance_accept,
    run_final_acceptance_verify,
)
from agentforge_platform.packaging import (
    PackagingError,
    build_cost_report,
    package_run,
    verification_passed,
)
from agentforge_platform.readiness import (
    READINESS_MOCK_ONLY,
    compute_readiness,
    declared_services,
)
from agentforge_platform.resolver import AssetLock, resolve_assets
from agentforge_platform.runner_client import RunnerClient
from agentforge_platform.runs import (
    RECOVERABLE_STATUSES,
    TERMINAL_STATUSES,
    RunStatus,
    append_event,
    record_artifact,
    run_directory,
    transition,
    unsettled_call_count,
)
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import require_frozen_spec

if TYPE_CHECKING:
    from agentforge_platform.generation_graph import GenerationContext

FACTORY_PIPELINE_ENV = "AGENTFORGE_FACTORY_PIPELINE_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

StageRunner = Callable[["GenerationContext", int, FactoryStateStore, FactoryPhase], dict[str, Any]]

_COPY_EXCLUDED_DIRS = frozenset(
    {
        "node_modules",
        "target",
        "dist",
        "build",
        "__pycache__",
        ".git",
        ".venv",
        ".staging",
        "staging",
        ".cache",
        "logs",
    }
)
_TEMPLATE_TOP_FILES = ("compose.yaml", "README.md", ".env.example", "manifest.json")
_INFRA_OVERALL_STATES = frozenset({"INFRASTRUCTURE", "NOT_RUN", "ERROR"})


def factory_pipeline_enabled() -> bool:
    """判定是否显式开启异步工厂流水线（默认 ``False``）。"""
    raw = os.environ.get(FACTORY_PIPELINE_ENV, "").strip().lower()
    return raw in _TRUTHY


@dataclass
class FactoryRuntime:
    """FP09 工厂流水线运行时组件集合（由 ``worker.py`` 或测试按需注入）。"""

    chief: ChiefArchitect
    backend_pool: AsyncDepartmentPool
    frontend_pool: AsyncDepartmentPool
    frontend_module_verifier: ModuleVerifier | None = None
    asset_cache_root: Path | None = None
    max_integration_rounds: int | None = None
    sync_assets_from_filesystem: bool = True


def build_factory_runtime(
    *,
    session_factory: sessionmaker[Session],
    runner_client: RunnerClient,
    work_root: str | Path,
    chief: ChiefArchitect | None = None,
    backend_pool: AsyncDepartmentPool | None = None,
    frontend_pool: AsyncDepartmentPool | None = None,
    frontend_module_verifier: ModuleVerifier | None = None,
    asset_cache_root: str | Path | None = None,
    max_integration_rounds: int | None = None,
) -> FactoryRuntime:
    """构造默认生产工厂运行时（全量走 A04 路由、A07 账本与分层验证器）。"""
    store = FactoryStateStore(session_factory)
    dept_verifier = build_module_verifier(runner_client=runner_client, work_root=work_root)
    resolved_chief = chief or ChiefArchitect(session_factory=session_factory, state_store=store)
    resolved_be_pool = backend_pool or AsyncDepartmentPool(
        session_factory=session_factory,
        verifier=dept_verifier,
        state_store=store,
    )
    resolved_fe_pool = frontend_pool or AsyncDepartmentPool(
        session_factory=session_factory,
        verifier=dept_verifier,
        state_store=store,
    )
    return FactoryRuntime(
        chief=resolved_chief,
        backend_pool=resolved_be_pool,
        frontend_pool=resolved_fe_pool,
        frontend_module_verifier=frontend_module_verifier or dept_verifier,
        asset_cache_root=Path(asset_cache_root).resolve() if asset_cache_root else None,
        max_integration_rounds=max_integration_rounds,
    )


def _transition_run_if_active(
    context: GenerationContext,
    run_id: int,
    target: RunStatus,
    *,
    event_type: str,
    payload: dict[str, Any],
    error_code: str | None = None,
    external_readiness: str | None = None,
) -> None:
    with session_scope(context.session_factory) as db:
        run = db.get(Run, run_id)
        if run is None:
            return
        if external_readiness is not None:
            run.external_readiness = external_readiness
        if RunStatus(run.status) not in TERMINAL_STATUSES:
            transition(
                db,
                run,
                target,
                error_code=error_code if target is RunStatus.FAILED else None,
            )
        append_event(db, run, event_type, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _copy_clean_subtree(src_dir: Path, dst_dir: Path) -> None:
    """将后端/前端已总装源码干净复制到全栈交付根目录（排除构建缓存、node_modules、符号链接）。"""
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)

    for src_path in sorted(src_dir.rglob("*")):
        rel = src_path.relative_to(src_dir)
        if any(part in _COPY_EXCLUDED_DIRS for part in rel.parts):
            continue
        if src_path.is_symlink() or src_path.is_dir():
            continue
        target = dst_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, target)
    make_writable_tree(dst_dir)


def assemble_fullstack_workspace(
    run_dir: str | Path,
    *,
    backend_assembled_dir: str | Path,
    frontend_assembled_dir: str | Path,
    template_dir: str | Path,
    authoritative_openapi_doc: dict[str, Any],
    factory_blueprint_payload: dict[str, Any],
) -> Path:
    """FP09 确定性全栈总装（0 模型调用）：合并完整后端、前端、compose.yaml、README.md 与权威 OpenAPI。"""
    run_root = Path(run_dir).resolve()
    run_root.mkdir(parents=True, exist_ok=True)

    be_src = Path(backend_assembled_dir).resolve() / "backend"
    fe_src = Path(frontend_assembled_dir).resolve() / "frontend"
    tpl_root = Path(template_dir).resolve()

    if not be_src.is_dir():
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"全栈总装缺少后端总装目录：{be_src}",
            status_code=422,
        )
    if not fe_src.is_dir():
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"全栈总装缺少前端总装目录：{fe_src}",
            status_code=422,
        )

    _copy_clean_subtree(be_src, run_root / "backend")
    _copy_clean_subtree(fe_src, run_root / "frontend")

    # 从已认证模板物化目录复制顶层编排与说明文件（绝不修改已发布模板原件）
    for fname in _TEMPLATE_TOP_FILES:
        src_file = tpl_root / fname
        if src_file.is_file() and not src_file.is_symlink():
            dst_file = run_root / fname
            shutil.copy2(src_file, dst_file)
            dst_file.chmod(0o644)

    # 写入权威冻结 backend/openapi.json 与 factory-blueprint.json
    oa_target = run_root / "backend" / "openapi.json"
    oa_target.parent.mkdir(parents=True, exist_ok=True)
    oa_target.write_text(
        json.dumps(authoritative_openapi_doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (run_root / "factory-blueprint.json").write_text(
        json.dumps(factory_blueprint_payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return run_root


def _collect_sandbox_integrations(
    backend_bp: ArchitectureBlueprint,
    frontend_bp: ArchitectureBlueprint,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for bp in (backend_bp, frontend_bp):
        for item in bp.sandbox_integrations:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id") or item.get("kind") or json.dumps(item, sort_keys=True))
            if key not in seen_keys:
                seen_keys.add(key)
                merged.append(dict(item))
    return merged


def _resolve_template_dir(
    materializer: AssetMaterializer,
    lock: AssetLock,
) -> Path:
    templates = [asset for asset in lock.assets if asset.kind == "template"]
    if len(templates) != 1:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"AssetLock 模板数量必须为 1，实际为 {len(templates)}",
            status_code=422,
        )
    tpl = templates[0]
    return materializer.materialize_release("template", tpl.id, tpl.version)


def _is_not_run_or_infra_report(report: dict[str, Any] | None) -> bool:
    if not isinstance(report, dict):
        return True
    overall = str(report.get("overall") or "").strip().upper()
    err = str(report.get("error_code") or report.get("category") or "").strip().upper()
    if overall in _INFRA_OVERALL_STATES or err in ("INFRASTRUCTURE", "RUNNER_UNAVAILABLE"):
        return True
    checks = report.get("checks")
    if isinstance(checks, list):
        for item in checks:
            if not isinstance(item, dict):
                continue
            st = str(item.get("status") or "").strip().upper()
            ec = str(item.get("error_code") or item.get("error_class") or "").strip().upper()
            if st == "NOT_RUN" or ec == "INFRASTRUCTURE":
                return True
    return False


def _has_real_factory_inputs(context: GenerationContext, run_id: int) -> bool:
    """区分 FP01 最小骨架单测（无 SpecVersion / 无 Runner 方法）与 FP09 真实工厂运行。"""
    if context.factory_runtime is not None:
        return True
    with session_scope(context.session_factory) as db:
        run = db.get(Run, run_id)
        if run is None:
            return False
        spec_row = db.get(SpecVersion, run.spec_id)
        if spec_row is None:
            return False
    runner = context.runner
    is_local_verifier = getattr(runner, "verifier_kind", None) == "local_process"
    has_runner_methods = callable(getattr(runner, "verify", None)) and callable(
        getattr(runner, "accept", None)
    )
    return bool(has_runner_methods or is_local_verifier)


def _run_full_factory_pipeline(
    context: GenerationContext,
    run_id: int,
    store: FactoryStateStore,
    recovery_summary: dict[str, Any],
) -> dict[str, Any]:
    """FP09 完整端到端工厂流水线：后端阶段 → 前端阶段 → 全栈总装 → 强制 Docker Runner 验收 → 打包。"""
    # 硬红线：全栈最终验收必须强制使用独立 Docker Runner，严禁 LocalProcessModuleVerifier
    require_docker_runner_for_final_acceptance(context.runner)

    # 1. 加载冻结规格、校验 spec_hash、解析 AssetLock 与 BusinessContract
    with session_scope(context.session_factory) as db:
        run = db.get(Run, run_id)
        if run is None:
            raise AppException(ErrorCode.RUN_NOT_FOUND, f"run {run_id} 不存在", status_code=404)
        project_id = int(run.project_id)
        spec_row = db.get(SpecVersion, run.spec_id)
        if spec_row is None:
            raise AppException(
                ErrorCode.SPEC_NOT_READY, f"规格版本 {run.spec_id} 不存在", status_code=409
            )
        spec: SoftwareSpecification = require_frozen_spec(spec_row)
        if run.spec_hash and run.spec_hash != spec_row.hash:
            raise AppException(
                ErrorCode.SPEC_CONFLICT,
                "运行记录的冻结 spec_hash 与规格版本不一致",
                status_code=409,
            )

    profile_id = spec.technical_profile.profile_id
    profile_ref = f"{profile_id}@{spec.technical_profile.profile_version}"
    contract: BusinessContract | None = None
    try:
        contract = compile_from_spec(spec)
    except ContractError:
        # 工厂模式支持 4..25 业务实体，超出旧 Demo 1..3 实体契约编译器上限时由权威 OpenAPI 承载契约
        contract = None

    run_dir = run_directory(context.work_root, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    persisted_lock = run_dir / "asset-lock.json"
    if persisted_lock.is_file():
        lock = AssetLock.model_validate_json(persisted_lock.read_text(encoding="utf-8"))
    elif contract is not None:
        lock = resolve_lock(
            context.registry,
            profile_id,
            spec,
            contract,
            methods_root_path=context.methods_root,
        )
    else:
        lock = resolve_assets(context.registry, profile_id)

    runtime: FactoryRuntime = (
        context.factory_runtime
        if isinstance(context.factory_runtime, FactoryRuntime)
        else build_factory_runtime(
            session_factory=context.session_factory,
            runner_client=context.runner,
            work_root=context.work_root,
        )
    )

    # 全部工厂中间暂存目录置于 run_dir/staging/，packaging.collect_files 自动排除，绝不污染 source.zip
    factory_staging_root = run_dir / "staging"
    factory_staging_root.mkdir(parents=True, exist_ok=True)

    # 确保资产库已同步并物化只读基础模板
    cache_root = runtime.asset_cache_root or default_cache_root()
    with session_scope(context.session_factory) as db:
        if runtime.sync_assets_from_filesystem:
            PgAssetStore(db).sync_from_filesystem()
        materializer = AssetMaterializer(db, cache_root=cache_root)
        template_dir = _resolve_template_dir(materializer, lock)

    # 切入 GENERATING 阶段
    with session_scope(context.session_factory) as db:
        run = db.get(Run, run_id)
        if run is not None and RunStatus(run.status) is RunStatus.PLANNING:
            transition(db, run, RunStatus.GENERATING)

    # 2. 后端工厂阶段：总管蓝图 → 只读后端底座 → 多部门并行 → 总装 + 强制 Docker Runner 验收 → 冻结 OpenAPI
    store.raise_if_cancelled(run_id, boundary="factory_before_backend")
    existing_be_bp = store.get_latest_blueprint(run_id, FactoryPhase.BACKEND, approved_only=True)
    if existing_be_bp is not None:
        backend_bp = ArchitectureBlueprint.model_validate_json(existing_be_bp.blueprint_json)
    else:
        backend_bp = runtime.chief.plan_backend_blueprint(spec, profile_ref, run_id)

    with session_scope(context.session_factory) as db:
        be_mat = AssetMaterializer(db, cache_root=cache_root)
        be_base = build_backend_base(factory_staging_root, backend_bp, lock, be_mat)

    be_stage: BackendStageResult = asyncio.run(
        run_backend_factory_stage(
            run_id=run_id,
            blueprint=backend_bp,
            spec=spec,
            run_dir=factory_staging_root,
            base_dir=be_base.base_dir,
            pool=runtime.backend_pool,
            chief=runtime.chief,
            runner_client=context.runner,
            state_store=store,
            max_integration_rounds=runtime.max_integration_rounds,
            work_root=context.work_root,
            template_dir=template_dir,
            lock=lock,
        )
    )
    if not be_stage.ok or not be_stage.frozen_openapi_doc:
        return _handle_stage_stop(
            context=context,
            run_id=run_id,
            stage="backend",
            stage_status=be_stage.status,
            error_code=be_stage.error_code or "BACKEND_STAGE_FAILED",
            recovery_summary=recovery_summary,
            detail={"backend_stage": be_stage.model_dump(mode="json")},
        )

    frozen_openapi_doc = be_stage.frozen_openapi_doc
    frozen_openapi_sha = be_stage.frozen_openapi_sha256 or canonical_openapi_sha256(
        frozen_openapi_doc
    )

    # 3. 前端工厂阶段：总管蓝图（消费冻结 OpenAPI）→ 只读前端底座 + TS 契约 → 多部门并行 → 路由聚合与构建验证
    store.raise_if_cancelled(run_id, boundary="factory_before_frontend")
    existing_fe_bp = store.get_latest_blueprint(run_id, FactoryPhase.FRONTEND, approved_only=True)
    if existing_fe_bp is not None:
        frontend_bp = ArchitectureBlueprint.model_validate_json(existing_fe_bp.blueprint_json)
    else:
        frontend_bp = runtime.chief.plan_frontend_blueprint(
            spec,
            frozen_openapi_doc,
            profile_ref,
            run_id,
            openapi_sha256=frozen_openapi_sha,
        )

    with session_scope(context.session_factory) as db:
        fe_mat = AssetMaterializer(db, cache_root=cache_root)
        fe_base = build_frontend_base(
            factory_staging_root,
            frontend_bp,
            frozen_openapi_doc,
            fe_mat,
            lock=lock,
            openapi_sha256=frozen_openapi_sha,
            state_store=store,
            run_id=run_id,
        )

    fe_stage: FrontendStageResult = asyncio.run(
        run_frontend_factory_stage(
            run_id=run_id,
            blueprint=frontend_bp,
            spec=spec,
            openapi_doc=frozen_openapi_doc,
            openapi_sha256=frozen_openapi_sha,
            run_dir=factory_staging_root,
            base_dir=fe_base.base_dir,
            pool=runtime.frontend_pool,
            chief=runtime.chief,
            state_store=store,
            runner_client=context.runner if runtime.frontend_module_verifier is None else None,
            module_verifier=runtime.frontend_module_verifier,
            max_integration_rounds=runtime.max_integration_rounds,
            work_root=context.work_root,
        )
    )
    if not fe_stage.ok:
        return _handle_stage_stop(
            context=context,
            run_id=run_id,
            stage="frontend",
            stage_status=fe_stage.status,
            error_code=fe_stage.error_code or "FRONTEND_STAGE_FAILED",
            recovery_summary=recovery_summary,
            detail={"frontend_stage": fe_stage.model_dump(mode="json")},
        )

    # 4. 全栈总装：合并后端 + 前端 + compose.yaml + README.md + 权威 openapi.json + factory-blueprint.json
    store.raise_if_cancelled(run_id, boundary="factory_before_fullstack_assembly")
    blueprint_bundle = {
        "mode": "factory",
        "run_id": run_id,
        "openapi_sha256": frozen_openapi_sha,
        "backend": backend_bp.model_dump(mode="json"),
        "frontend": frontend_bp.model_dump(mode="json"),
    }
    assemble_fullstack_workspace(
        run_dir,
        backend_assembled_dir=be_stage.assembled_dir,
        frontend_assembled_dir=fe_stage.assembled_dir,
        template_dir=template_dir,
        authoritative_openapi_doc=frozen_openapi_doc,
        factory_blueprint_payload=blueprint_bundle,
    )
    # 在调用独立 Docker Runner /verify & /accept 前先落盘 asset-lock.json 与 spec.json，
    # 确保 Runner static_manifest 与 MySQL digest 对账（digest_reconciled）可正常通过。
    (run_dir / "asset-lock.json").write_text(
        lock.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "spec.json").write_text(
        spec.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    with session_scope(context.session_factory) as db:
        run = db.get(Run, run_id)
        if run is not None and RunStatus(run.status) is RunStatus.GENERATING:
            transition(db, run, RunStatus.VERIFYING)
            append_event(
                db,
                run,
                "stage",
                json.dumps(
                    {
                        "stage": "fullstack_assemble",
                        "mode": "factory",
                        "openapi_sha256": frozen_openapi_sha,
                    }
                ),
            )

    # 5. 全栈最终验收：强制走独立 Docker Runner（缺少 Docker Runner 明确标 NOT_RUN，绝不标 SUCCEEDED）
    store.raise_if_cancelled(run_id, boundary="factory_before_fullstack_verify")
    sandbox_integrations = _collect_sandbox_integrations(backend_bp, frontend_bp)
    verification: dict[str, Any]
    acceptance: dict[str, Any]
    runner_unavailable_error: str | None = None

    try:
        verification = run_final_acceptance_verify(context.runner, run_dir.name)
        if _is_not_run_or_infra_report(verification):
            runner_unavailable_error = str(
                verification.get("error_code") or "RUNNER_UNAVAILABLE"
            )
            verification = {
                **verification,
                "overall": "NOT_RUN",
                "error_code": runner_unavailable_error,
            }
            acceptance = {"overall": "NOT_RUN", "cases": []}
        else:
            acceptance = run_final_acceptance_accept(context.runner, run_dir.name)
            if _is_not_run_or_infra_report(acceptance):
                runner_unavailable_error = str(
                    acceptance.get("error_code") or "RUNNER_UNAVAILABLE"
                )
                acceptance = {
                    **acceptance,
                    "overall": "NOT_RUN",
                    "error_code": runner_unavailable_error,
                }
    except (RuntimeError, OSError, TimeoutError) as runner_exc:
        runner_unavailable_error = "RUNNER_UNAVAILABLE"
        verification = {
            "overall": "NOT_RUN",
            "error_code": "RUNNER_UNAVAILABLE",
            "detail": f"独立 Docker Runner 不可用：{runner_exc}",
            "checks": [
                {
                    "name": "docker_runner",
                    "status": "NOT_RUN",
                    "detail": str(runner_exc),
                }
            ],
        }
        acceptance = {
            "overall": "NOT_RUN",
            "error_code": "RUNNER_UNAVAILABLE",
            "cases": [],
        }

    # 6. 外部服务就绪度计算（沙箱模拟绝不伪标 LIVE_VERIFIED）+ 打包与密钥扫描
    services = declared_services(spec, verification)
    if sandbox_integrations:
        external_readiness = READINESS_MOCK_ONLY
    else:
        external_readiness = compute_readiness(services)

    with session_scope(context.session_factory) as db:
        cost_report = build_cost_report(db, project_id=project_id, run_id=run_id)

    try:
        pkg = package_run(
            run_dir,
            spec=spec,
            lock=lock,
            contract=contract,
            verification=verification,
            acceptance=acceptance,
            cost_report=cost_report,
            factory_blueprint=blueprint_bundle,
            authoritative_openapi=frozen_openapi_doc,
            include_department_cost_report=True,
            sandbox_integrations=sandbox_integrations,
            external_readiness=external_readiness,
        )
    except PackagingError as exc:
        _transition_run_if_active(
            context,
            run_id,
            RunStatus.FAILED,
            event_type="failed",
            payload={"code": exc.code, "stage": "factory_package", "detail": str(exc)},
            error_code=exc.code,
            external_readiness=external_readiness,
        )
        return {
            "run_id": run_id,
            "ok": False,
            "mode": "factory",
            "error_code": exc.code,
            "detail": str(exc),
            "recovery": recovery_summary,
        }

    fullstack_ok = (
        runner_unavailable_error is None
        and verification_passed(verification, acceptance)
        and pkg.verified
    )

    with session_scope(context.session_factory) as db:
        run = db.get(Run, run_id)
        if run is not None:
            run.external_readiness = external_readiness
            artifact_row = record_artifact(
                db,
                run,
                pkg.zip_path,
                pkg.zip_sha256,
                "zip",
                verified=pkg.verified,
            )
            artifact_id = int(artifact_row.id)
            append_event(
                db,
                run,
                "packaged",
                json.dumps(
                    {
                        "mode": "factory",
                        "zip": pkg.zip_path,
                        "files": pkg.file_count,
                        "verified": pkg.verified,
                        "external_readiness": external_readiness,
                    }
                ),
            )
            if fullstack_ok:
                transition(db, run, RunStatus.SUCCEEDED)
                append_event(db, run, "succeeded", json.dumps({"mode": "factory"}))
            else:
                fail_code = runner_unavailable_error or "VERIFICATION_FAILED"
                transition(db, run, RunStatus.FAILED, error_code=fail_code)
                append_event(
                    db,
                    run,
                    "failed",
                    json.dumps(
                        {
                            "code": fail_code,
                            "stage": "fullstack_verify",
                            "verify_overall": verification.get("overall"),
                            "accept_overall": acceptance.get("overall"),
                        }
                    ),
                )
        else:
            artifact_id = None

    return {
        "run_id": run_id,
        "ok": fullstack_ok,
        "mode": "factory",
        "status": "SUCCEEDED" if fullstack_ok else "FAILED",
        "verification_status": str(verification.get("overall") or "NOT_RUN"),
        "acceptance_status": str(acceptance.get("overall") or "NOT_RUN"),
        "external_readiness": external_readiness,
        "artifact_id": artifact_id,
        "zip_path": pkg.zip_path,
        "zip_sha256": pkg.zip_sha256,
        "openapi_sha256": frozen_openapi_sha,
        "recovery": recovery_summary,
        "error_code": "" if fullstack_ok else (runner_unavailable_error or "VERIFICATION_FAILED"),
    }


def _handle_stage_stop(
    *,
    context: GenerationContext,
    run_id: int,
    stage: str,
    stage_status: str,
    error_code: str,
    recovery_summary: dict[str, Any],
    detail: dict[str, Any],
) -> dict[str, Any]:
    if stage_status == "CANCELLED":
        _transition_run_if_active(
            context,
            run_id,
            RunStatus.CANCELLED,
            event_type="cancelled",
            payload={"code": "RUN_CANCELLED", "stage": stage, "mode": "factory"},
        )
        return {
            "run_id": run_id,
            "ok": False,
            "mode": "factory",
            "error_code": "RUN_CANCELLED",
            "recovery": recovery_summary,
            **detail,
        }
    if stage_status == "PAUSED_BUDGET":
        _transition_run_if_active(
            context,
            run_id,
            RunStatus.PAUSED_BUDGET,
            event_type="paused_budget",
            payload={"code": "BUDGET_EXCEEDED", "stage": stage, "mode": "factory"},
        )
        return {
            "run_id": run_id,
            "ok": False,
            "mode": "factory",
            "error_code": "PAUSED_BUDGET",
            "recovery": recovery_summary,
            **detail,
        }
    final_code = "INFRASTRUCTURE" if stage_status == "INFRASTRUCTURE" else error_code
    _transition_run_if_active(
        context,
        run_id,
        RunStatus.FAILED,
        event_type="failed",
        payload={"code": final_code, "stage": stage, "mode": "factory"},
        error_code=final_code,
    )
    return {
        "run_id": run_id,
        "ok": False,
        "mode": "factory",
        "verification_status": "NOT_RUN" if stage_status == "INFRASTRUCTURE" else "FAIL",
        "error_code": final_code,
        "recovery": recovery_summary,
        **detail,
    }


def run_factory_pipeline(
    context: GenerationContext,
    run_id: int,
    *,
    backend_runner: StageRunner | None = None,
    frontend_runner: StageRunner | None = None,
) -> dict[str, Any]:
    """FP01/FP09 顶层异步工厂流水线编排器：处理领取/恢复、取消检查、全栈执行与打包交付。"""
    from agentforge_platform.generation_graph import GenerationError

    store = FactoryStateStore(context.session_factory)

    with session_scope(context.session_factory) as db:
        run = db.get(Run, run_id)
        if run is None:
            raise GenerationError("RUN_NOT_FOUND", f"run {run_id} 不存在")
        if run.cancel_requested:
            if RunStatus(run.status) not in TERMINAL_STATUSES:
                transition(db, run, RunStatus.CANCELLED)
                append_event(db, run, "cancelled", json.dumps({"stage": "factory_pre_claim"}))
            return {
                "run_id": run_id,
                "ok": False,
                "mode": "factory",
                "error_code": "RUN_CANCELLED",
            }
        recovering = RunStatus(run.status) in RECOVERABLE_STATUSES
        if recovering:
            if unsettled_call_count(db, run_id) > 0:
                transition(db, run, RunStatus.FAILED, error_code="NEEDS_RECONCILIATION")
                append_event(
                    db,
                    run,
                    "failed",
                    json.dumps({"code": "NEEDS_RECONCILIATION", "stage": "factory_recover"}),
                )
                return {
                    "run_id": run_id,
                    "ok": False,
                    "mode": "factory",
                    "error_code": "NEEDS_RECONCILIATION",
                }
            previous = str(run.status)
            run.status = RunStatus.PLANNING
            db.flush()
            append_event(db, run, "recovered", json.dumps({"from": previous, "mode": "factory"}))
        else:
            transition(db, run, RunStatus.PLANNING)
            append_event(db, run, "claimed", json.dumps({"mode": "factory"}))
            append_event(db, run, "factory_started", json.dumps({"recovering": False}))

    try:
        store.raise_if_cancelled(run_id, boundary="before_backend_recovery")
        backend_recovery = store.scan_recovery_plan(run_id, FactoryPhase.BACKEND)
        frontend_recovery = store.scan_recovery_plan(run_id, FactoryPhase.FRONTEND)
        recovery_summary = {
            "backend": backend_recovery.as_dict(),
            "frontend": frontend_recovery.as_dict(),
        }

        # 显式传入自定义阶段钩子时保留 FP01 阶段测试语义
        if backend_runner is not None or frontend_runner is not None:
            results: dict[str, Any] = {}
            if backend_runner is not None:
                store.raise_if_cancelled(run_id, boundary="before_backend_stage")
                results["backend"] = backend_runner(context, run_id, store, FactoryPhase.BACKEND)

            if frontend_runner is not None:
                store.raise_if_cancelled(run_id, boundary="before_frontend_stage")
                results["frontend"] = frontend_runner(context, run_id, store, FactoryPhase.FRONTEND)

            store.raise_if_cancelled(run_id, boundary="before_finalize")
            return {
                "run_id": run_id,
                "ok": True,
                "mode": "factory",
                "recovery": recovery_summary,
                "stages": results,
            }

        # 未注入工厂运行时且缺少真实冻结规格/Runner 时（如 FP01 骨架单测），保持明确落 FAILED 语义
        if not _has_real_factory_inputs(context, run_id):
            _transition_run_if_active(
                context,
                run_id,
                RunStatus.FAILED,
                event_type="failed",
                payload={
                    "code": "FACTORY_STAGE_NOT_IMPLEMENTED",
                    "mode": "factory",
                    "recovery": recovery_summary,
                },
                error_code="FACTORY_STAGE_NOT_IMPLEMENTED",
            )
            return {
                "run_id": run_id,
                "ok": False,
                "mode": "factory",
                "error_code": "FACTORY_STAGE_NOT_IMPLEMENTED",
                "recovery": recovery_summary,
            }

        # FP09 完整端到端工厂流水线执行
        return _run_full_factory_pipeline(context, run_id, store, recovery_summary)
    except FactoryCancelledError:
        cancelled_depts = store.cancel_incomplete_departments(
            run_id, reason="run_cancel_requested"
        )
        _transition_run_if_active(
            context,
            run_id,
            RunStatus.CANCELLED,
            event_type="cancelled",
            payload={
                "code": "RUN_CANCELLED",
                "mode": "factory",
                "cancelled_depts": cancelled_depts,
            },
        )
        return {
            "run_id": run_id,
            "ok": False,
            "mode": "factory",
            "error_code": "RUN_CANCELLED",
            "cancelled_depts": cancelled_depts,
        }
    except Exception as exc:  # noqa: BLE001
        raw_code = getattr(exc, "code", None)
        code = str(raw_code.value if hasattr(raw_code, "value") else (raw_code or type(exc).__name__))
        if code == "BUDGET_EXCEEDED":
            _transition_run_if_active(
                context,
                run_id,
                RunStatus.PAUSED_BUDGET,
                event_type="paused_budget",
                payload={"code": code, "mode": "factory"},
            )
            return {
                "run_id": run_id,
                "ok": False,
                "mode": "factory",
                "error_code": "PAUSED_BUDGET",
            }
        _transition_run_if_active(
            context,
            run_id,
            RunStatus.FAILED,
            event_type="failed",
            payload={"code": code, "mode": "factory", "detail": str(exc)[:400]},
            error_code=code,
        )
        return {
            "run_id": run_id,
            "ok": False,
            "mode": "factory",
            "error_code": code,
            "detail": str(exc)[:400],
        }


__all__ = [
    "FACTORY_PIPELINE_ENV",
    "FactoryRuntime",
    "StageRunner",
    "assemble_fullstack_workspace",
    "build_factory_runtime",
    "factory_pipeline_enabled",
    "run_factory_pipeline",
]
