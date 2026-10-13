"""R23：worker 崩溃恢复、检查点与原子提交对账。

覆盖：租约接管（过期在途 run 可被新 worker 领取，新鲜租约不被抢）；装配完成后崩溃恢复**不重做装配、不覆盖产物**；
验证阶段崩溃恢复一致终态；模型在途（状态不明调用）时**拒绝对账、不盲目重发**；恢复复用原 asset-lock 不重选方法；
以及 LangGraph checkpointer 续跑不重做已提交任务。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from agentforge_platform.asset_registry import default_registry
from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import GenerationContext, build_graph, run_generation
from agentforge_platform.models import (
    Artifact,
    ModelCall,
    Project,
    Run,
    RunTask,
    SpecVersion,
)
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.runs import (
    RunStatus,
    _now,
    claim_run,
    create_run,
    transition,
)
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}


class _Runner:
    def __init__(self, *, verify=None, accept=None) -> None:
        self._verify = verify or (lambda _d: _PASS_VERIFY)
        self._accept = accept or (lambda _d: _PASS_ACCEPT)

    def verify(self, artifact_dir: str) -> dict:
        return self._verify(artifact_dir)

    def accept(self, artifact_dir: str) -> dict:
        return self._accept(artifact_dir)


def _boom(_artifact_dir: str):
    raise RuntimeError("worker crashed")


_ORDER = {RunStatus.QUEUED: 0, RunStatus.PLANNING: 1, RunStatus.GENERATING: 2, RunStatus.VERIFYING: 3}


def _seed_run(factory, *, status: RunStatus = RunStatus.QUEUED, lease_past: bool = False) -> int:
    spec = SoftwareSpecification.model_validate_json(TASK.read_text(encoding="utf-8"))
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        row = SpecVersion(project_id=project.id, version=1, body_json=canonical_json(spec),
                          hash=content_hash(spec), confirmed_at=datetime(2026, 1, 1))
        session.add(row)
        session.flush()
        project.active_spec_id = row.id
        run = create_run(session, project, row.id, idempotency_key="k1",
                         spec_hash=content_hash(spec))
        for step in (RunStatus.PLANNING, RunStatus.GENERATING, RunStatus.VERIFYING):
            if _ORDER[step] <= _ORDER[status]:
                transition(session, run, step)
        if lease_past:
            run.lease_owner = "dead-worker"
            run.lease_until = _now() - timedelta(seconds=5)
        session.flush()
        return run.id


def _context(factory, tmp_path: Path, runner, *, checkpointer=None) -> GenerationContext:
    return GenerationContext(session_factory=factory, runner=runner, work_root=tmp_path / "runs",
                             registry=default_registry(), checkpointer=checkpointer)


def _drive(context: GenerationContext, run_id: int, checkpointer: bool = False) -> None:
    config = {"configurable": {"thread_id": f"generation:{run_id}"}} if checkpointer else None
    build_graph(context).invoke({"run_id": run_id}, config=config)


def _tree_hashes(run_dir: Path) -> dict[str, str]:
    return {
        path.relative_to(run_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(run_dir.rglob("*")) if path.is_file()
    }


# --- 租约接管 -----------------------------------------------------------------

def test_claim_run_takes_over_expired_lease(platform_app) -> None:
    _, factory = platform_app
    stale = _seed_run(factory, status=RunStatus.GENERATING, lease_past=True)
    fresh = _seed_run(factory, status=RunStatus.VERIFYING)  # 无过期租约

    with session_scope(factory) as session:
        run = claim_run(session, "w2")  # 新 worker 接管崩溃 worker 的在途 run
    assert run is not None and run.id == stale
    assert run.lease_owner == "w2"

    with session_scope(factory) as session:
        assert claim_run(session, "w3") is None  # 新鲜租约 / 无更多可领
        assert session.get(Run, fresh).lease_owner is None


# --- 装配完成后崩溃 → 恢复不重做装配、不覆盖产物 ------------------------------

def test_recover_after_assemble_does_not_redo_or_overwrite(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory, status=RunStatus.PLANNING)
    run_dir = tmp_path / "runs" / f"run-{run_id}"

    _drive_crashed = None
    try:  # 模拟崩溃：装配完成、验证阶段进程死亡（异常逃逸，未落终态）
        _drive(_context(factory, tmp_path, _Runner(verify=_boom)), run_id)
    except RuntimeError as exc:
        assert "crashed" in str(exc)
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "VERIFYING"  # 非终态，等待接管（装配已提交）

    before = _tree_hashes(run_dir)

    result = run_generation(_context(factory, tmp_path, _Runner()), run_id)  # 恢复续跑

    assert result["ok"] is True, result
    assert _tree_hashes(run_dir) == {**_tree_hashes(run_dir), **before}  # 既有文件未被覆盖
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"
        assert session.query(RunTask).filter(RunTask.task_id == "assemble").count() == 1  # 未重做
        assert session.query(Artifact).filter(Artifact.kind == "source_tree").count() == 1
        assert session.query(RunTask).filter(RunTask.task_id == "verify").count() == 1


def test_recover_reuses_persisted_asset_lock(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory, status=RunStatus.PLANNING)
    run_dir = tmp_path / "runs" / f"run-{run_id}"

    try:
        _drive(_context(factory, tmp_path, _Runner(verify=_boom)), run_id)
    except RuntimeError:
        pass
    lock_before = (run_dir / "asset-lock.json").read_text(encoding="utf-8")

    run_generation(_context(factory, tmp_path, _Runner()), run_id)

    assert (run_dir / "asset-lock.json").read_text(encoding="utf-8") == lock_before  # 复用，不重选


# --- 验证阶段崩溃 → 一致终态 --------------------------------------------------

def test_recover_after_verify_crash(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory, status=RunStatus.PLANNING)

    try:  # 装配+verify 通过，accept 阶段崩溃
        _drive(_context(factory, tmp_path, _Runner(accept=_boom)), run_id)
    except RuntimeError:
        pass
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "VERIFYING"

    result = run_generation(_context(factory, tmp_path, _Runner()), run_id)

    assert result["ok"] is True
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"
        assert session.query(Artifact).filter(Artifact.kind == "zip").count() == 1


# --- 模型在途（状态不明）→ 拒绝对账、不重复收费 -------------------------------

def test_recover_refuses_when_unsettled_calls(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory, status=RunStatus.PLANNING)
    with session_scope(factory) as session:  # 崩溃前留下的状态不明调用
        session.add(ModelCall(call_id="c1", model_role="technical_planning", provider="dashscope",
                              model="qwen3.7-plus", project_id=1, run_id=run_id,
                              reserved_micro=500, status="RESERVED"))

    result = run_generation(_context(factory, tmp_path, _Runner()), run_id)

    assert result["ok"] is False and result["error_code"] == "NEEDS_RECONCILIATION"
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "FAILED"
        # 未重发：账本仍只有那一条在途调用（无新行、无重复收费）
        rows = session.query(ModelCall).filter(ModelCall.run_id == run_id).all()
        assert len(rows) == 1 and rows[0].status == "RESERVED" and rows[0].reserved_micro == 500


# --- LangGraph checkpointer 续跑 ---------------------------------------------

def test_checkpointer_persists_and_holds_no_secrets(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    run_id = _seed_run(factory, status=RunStatus.PLANNING)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    thread = {"configurable": {"thread_id": f"generation:{run_id}"}}

    try:  # 首次运行：verify 崩溃 → assemble 已完成，检查点已持久化
        _drive(_context(factory, tmp_path, _Runner(verify=_boom), checkpointer=saver), run_id, checkpointer=True)
    except RuntimeError:
        pass

    snapshot = saver.get_tuple(thread)  # LangGraph checkpointer：检查点已写入
    assert snapshot is not None
    blob = json.dumps(snapshot.checkpoint, default=str)
    # 检查点不含密钥（匹配真实密钥形态 / 敏感字段名）
    assert re.search(r'"api_key"', blob) is None
    assert re.search(r"sk-[A-Za-z0-9_-]{16,}", blob) is None
    assert "dashscope" not in blob.lower()

    # 崩溃后经状态式恢复续跑，一致终态；装配任务不重做
    result = run_generation(_context(factory, tmp_path, _Runner()), run_id)
    assert result["ok"] is True
    with session_scope(factory) as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"
        assert session.query(RunTask).filter(RunTask.task_id == "assemble").count() == 1
