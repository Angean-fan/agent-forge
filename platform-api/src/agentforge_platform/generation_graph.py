"""M23 LangGraph 固定生成图。

`load_confirmed_spec → resolve_assets → assemble → verify → package_or_fail`，由 worker 驱动。

- 生成图**只接收已确认的** `spec_id/spec_hash`，不读取原始聊天、不重新解释需求；
- 不引入 DeepAgent / Qwen 技术规划 / 自动修复 / 并行节点；
- 数据库为权威状态，节点入口检查运行有效性，记录 task、事件与阶段结果；
- **未通过全部必需检查不得进入 SUCCEEDED**（package_or_fail 决定终态）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform.assembler import assemble
from agentforge_platform.asset_registry import AssetRegistry, default_registry
from agentforge_platform.contract import BusinessContract, compile_from_spec
from agentforge_platform.db import session_scope
from agentforge_platform.models import Run, SpecVersion
from agentforge_platform.packaging import (
    PackagingError,
    build_cost_report,
    package_run,
    verification_passed,
)
from agentforge_platform.resolver import AssetLock, resolve_assets
from agentforge_platform.runner_client import RunnerClient
from agentforge_platform.runs import (
    RunStatus,
    append_event,
    record_artifact,
    record_task,
    run_directory,
    transition,
)
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import content_hash


class GenerationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GenerationState(TypedDict, total=False):
    run_id: int
    project_id: int
    spec_id: int
    spec_hash: str
    profile: str
    spec: SoftwareSpecification
    contract: BusinessContract
    lock: AssetLock
    lock_hash: str
    artifact_dir: str
    manifest_hash: str
    verification: dict
    acceptance: dict
    error_code: str
    ok: bool


@dataclass(frozen=True)
class GenerationContext:
    session_factory: sessionmaker[Session]
    runner: RunnerClient
    work_root: Path
    registry: AssetRegistry


def _require_active(db: Session, run_id: int, expected: RunStatus) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise GenerationError("RUN_NOT_FOUND", f"run {run_id} 不存在")
    if RunStatus(run.status) in (RunStatus.SUCCEEDED, RunStatus.FAILED):
        raise GenerationError("RUN_INVALID_TRANSITION", f"run 已终结：{run.status}")
    if RunStatus(run.status) is not expected:
        raise GenerationError("RUN_INVALID_TRANSITION", f"期望 {expected}，实为 {run.status}")
    return run


def _verify_error_code(verification: dict, acceptance: dict) -> str:
    codes = [c.get("error_code") for c in verification.get("checks", [])]
    codes += [c.get("error_code") for c in acceptance.get("cases", [])]
    if "INFRASTRUCTURE" in codes:
        return "INFRASTRUCTURE"
    return "VERIFICATION_FAILED"


def build_graph(context: GenerationContext):
    def load_confirmed_spec(state: GenerationState) -> GenerationState:
        run_id = state["run_id"]
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.PLANNING)
            spec_row = db.get(SpecVersion, run.spec_id)
            if spec_row is None or spec_row.confirmed_at is None:
                raise GenerationError("SPEC_NOT_FOUND", "缺少已确认（冻结）规格")
            spec = SoftwareSpecification.model_validate_json(spec_row.body_json)
            if content_hash(spec) != spec_row.hash:
                raise GenerationError("SPEC_CONFLICT", "规格 hash 与冻结值不一致")
            contract = compile_from_spec(spec)
            record_task(db, run, "load_confirmed_spec", status="DONE")
            append_event(
                db, run, "stage",
                json.dumps({"stage": "load_confirmed_spec", "spec_hash": spec_row.hash}),
            )
            return {
                "project_id": run.project_id,
                "spec_id": run.spec_id,
                "spec_hash": spec_row.hash,
                "profile": spec.technical_profile.profile_id,
                "spec": spec,
                "contract": contract,
            }

    def resolve_assets_node(state: GenerationState) -> GenerationState:
        run_id = state["run_id"]
        lock = resolve_assets(context.registry, state["profile"])
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.PLANNING)
            record_task(db, run, "resolve_assets", status="DONE")
            append_event(
                db, run, "stage",
                json.dumps({"stage": "resolve_assets", "lock_hash": lock.lock_hash}),
            )
        return {"lock_hash": lock.lock_hash, "lock": lock}

    def assemble_node(state: GenerationState) -> GenerationState:
        run_id = state["run_id"]
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.PLANNING)
            transition(db, run, RunStatus.GENERATING)
        target = run_directory(context.work_root, run_id)
        result = assemble(
            state["contract"], state["lock"], context.registry, target, spec=state["spec"]
        )
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.GENERATING)
            record_task(db, run, "assemble", status="DONE")
            record_artifact(db, run, ".", result.manifest_hash, "source_tree")
            append_event(
                db, run, "stage",
                json.dumps({"stage": "assemble", "manifest_hash": result.manifest_hash}),
            )
        return {"artifact_dir": target.name, "manifest_hash": result.manifest_hash}

    def verify_node(state: GenerationState) -> GenerationState:
        run_id = state["run_id"]
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.GENERATING)
            transition(db, run, RunStatus.VERIFYING)
        verification = context.runner.verify(state["artifact_dir"])
        acceptance = context.runner.accept(state["artifact_dir"])
        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.VERIFYING)
            record_task(db, run, "verify", status="DONE")
            append_event(
                db, run, "verification",
                json.dumps({"verify": verification.get("overall"),
                            "accept": acceptance.get("overall")}),
            )
        return {"verification": verification, "acceptance": acceptance}

    def package_or_fail(state: GenerationState) -> GenerationState:
        run_id = state["run_id"]
        verification = state["verification"]
        acceptance = state["acceptance"]
        ok = verification_passed(verification, acceptance)
        run_dir = run_directory(context.work_root, run_id)

        with session_scope(context.session_factory) as db:
            cost_report = build_cost_report(db, project_id=state["project_id"], run_id=run_id)

        try:
            package = package_run(
                run_dir,
                spec=state["spec"],
                lock=state["lock"],
                contract=state["contract"],
                verification=verification,
                acceptance=acceptance,
                cost_report=cost_report,
            )
        except PackagingError as exc:
            with session_scope(context.session_factory) as db:
                run = _require_active(db, run_id, RunStatus.VERIFYING)
                transition(db, run, RunStatus.FAILED, error_code=exc.code)
                append_event(db, run, "failed", json.dumps({"code": exc.code, "stage": "package"}))
            return {"ok": False, "error_code": exc.code}

        with session_scope(context.session_factory) as db:
            run = _require_active(db, run_id, RunStatus.VERIFYING)
            record_artifact(
                db, run, package.zip_path, package.zip_sha256, "zip", verified=package.verified
            )
            append_event(
                db, run, "packaged",
                json.dumps({"zip": package.zip_path, "files": package.file_count,
                            "verified": package.verified}),
            )
            if ok:
                transition(db, run, RunStatus.SUCCEEDED)
                append_event(db, run, "succeeded", "{}")
                return {"ok": True, "error_code": ""}
            code = _verify_error_code(verification, acceptance)
            transition(db, run, RunStatus.FAILED, error_code=code)
            append_event(db, run, "failed", json.dumps({"code": code, "stage": "verify"}))
            return {"ok": False, "error_code": code}

    graph = StateGraph(GenerationState)
    graph.add_node("load_confirmed_spec", load_confirmed_spec)
    graph.add_node("resolve_assets", resolve_assets_node)
    graph.add_node("assemble", assemble_node)
    graph.add_node("verify", verify_node)
    graph.add_node("package_or_fail", package_or_fail)
    graph.add_edge(START, "load_confirmed_spec")
    graph.add_edge("load_confirmed_spec", "resolve_assets")
    graph.add_edge("resolve_assets", "assemble")
    graph.add_edge("assemble", "verify")
    graph.add_edge("verify", "package_or_fail")
    graph.add_edge("package_or_fail", END)
    return graph.compile()


def run_generation(context: GenerationContext, run_id: int) -> GenerationState:
    """worker 处理入口：QUEUED→PLANNING 后驱动生成图；异常落 FAILED 并留事件。"""
    with session_scope(context.session_factory) as db:
        run = db.get(Run, run_id)
        if run is None:
            raise GenerationError("RUN_NOT_FOUND", f"run {run_id} 不存在")
        transition(db, run, RunStatus.PLANNING)
        append_event(db, run, "claimed", "{}")

    try:
        return build_graph(context).invoke({"run_id": run_id})
    except Exception as exc:  # noqa: BLE001  # 兜底落 FAILED，避免未捕获异常造成假成功
        code = getattr(exc, "code", None) or type(exc).__name__
        with session_scope(context.session_factory) as db:
            run = db.get(Run, run_id)
            if run is not None and RunStatus(run.status) not in (
                RunStatus.SUCCEEDED, RunStatus.FAILED
            ):
                transition(db, run, RunStatus.FAILED, error_code=str(code))
            if run is not None:
                append_event(db, run, "failed", json.dumps({"code": str(code)}))
        return {"run_id": run_id, "ok": False, "error_code": str(code)}


def default_context(
    *, session_factory: sessionmaker[Session], work_root: str | Path, runner: RunnerClient
) -> GenerationContext:
    return GenerationContext(
        session_factory=session_factory,
        runner=runner,
        work_root=Path(work_root),
        registry=default_registry(),
    )


__all__ = [
    "GenerationContext",
    "GenerationError",
    "GenerationState",
    "build_graph",
    "default_context",
    "run_generation",
]
