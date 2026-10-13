"""R27：正式 9 次独立生成评估与 Demo P0 判定（**真实运行**，需可达的受控 Runner）。

对校园活动 / 个人任务 / 图书借阅**各 3 次**独立生成（各自独立 DB、独立 run 目录），汇总质量/费用/延迟/
资产覆盖四层指标，写出 ``docs/DEMO_P0_ACCEPTANCE.md``。

- **Runner 不可达时不伪造**：直接按 NOT_RUN 记录并如实标注“未达到目标”，不宣称 PASS。
- 缺失/超时的必需测试计为失败；不人工改源码后计为成功。
- 生成期模型角色默认关闭（确定性装配，0 模型调用）；如需 Qwen/DeepSeek 参与，由环境变量显式开启。

用法（需可达 Runner；本机如未跑 Runner 会得到 NOT_RUN 报告）：
    cd platform-api
    RUNNER_BASE_URL=http://<host>:9000 RUNNER_TOKEN=... \
        uv run --locked python ../scripts/demo_p0_eval.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
PLATFORM_DIR = REPO / "platform-api"
SRC = PLATFORM_DIR / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DOC = REPO / "docs" / "DEMO_P0_ACCEPTANCE.md"
SPECS = {
    "campus": REPO / "schemas" / "campus-activity.spec.json",
    "task": REPO / "schemas" / "task-management.spec.json",
    "lending": REPO / "schemas" / "book-lending.spec.json",
}
R27_PROFILE_VERSION = "0.4"  # 离线 Runner 镜像锁在 profiles/demo-ts-mp-0.4.json


def _r27_spec(path: Path):
    """把历史样例规格固定到经离线 Runner 认证的 R27 Profile。"""
    from agentforge_platform.spec import SoftwareSpecification

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["technical_profile"]["profile_version"] = R27_PROFILE_VERSION
    return SoftwareSpecification.model_validate(payload)


def _ensure_method_index() -> str:
    """生成前重建只读索引，使 9 次 run 锁定同一可追溯快照。"""
    from agentforge_platform.method_index import build_index, index_path, methods_root

    return build_index(methods_root(), index_path()).revision


class ScopedRunnerClient:
    """把生成图的 run 名称映射到共享 Runner 根下的独立评估目录。"""

    def __init__(self, *, base_url: str, token: str, scope: str) -> None:
        from agentforge_platform.runner_client import HttpRunnerClient

        self._client = HttpRunnerClient(base_url=base_url, token=token)
        self._scope = scope.strip("/")
        if not self._scope or any(part in ("", ".", "..") for part in self._scope.split("/")):
            raise ValueError("评估目录 scope 非法")

    def verify(self, artifact_dir: str) -> dict:
        return self._client.verify(f"{self._scope}/{artifact_dir}")

    def accept(self, artifact_dir: str) -> dict:
        return self._client.accept(f"{self._scope}/{artifact_dir}")


def _runner_reachable(base_url: str, timeout: int = 4) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/health", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _not_run_report(base_url: str, reason: str | None = None) -> None:
    from agentforge_platform import model_gateway
    from agentforge_platform.evaluation import (
        CASES,
        RUNS_PER_CASE,
        RunEvaluation,
        render_report,
        routing_assertions,
        summarize,
    )

    reason = reason or f"Runner 不可达（{base_url}）"
    runs = [
        RunEvaluation(case=case, index=index, run_id=None, status="NOT_RUN", required_pass=False,
                      note=f"{reason}：必需构建/验收未运行，计为失败")
        for case in CASES for index in range(1, RUNS_PER_CASE + 1)
    ]
    routes = {role: (r.provider, r.model) for role, r in model_gateway.ROUTES.items()}
    summary = summarize(runs)
    DOC.write_text(
        render_report(
            summary, routing=routing_assertions(routes),
            generated_at=datetime.now(UTC).isoformat(),
            runner_note=f"{reason}——本报告 NOT_RUN，未达到目标。",
        ),
        encoding="utf-8",
    )


def _metrics(factory, run_dir: Path, run_id: int, case: str, index: int) -> object:
    from sqlalchemy import select

    from agentforge_platform.evaluation import RunEvaluation
    from agentforge_platform.models import Run, RunTask
    from agentforge_platform.runs import run_cost

    with open_session(factory) as session:
        run = session.get(Run, run_id)
        status = run.status
        error_code = run.error_code
        cost = run_cost(session, run_id)
        usage: dict[str, dict] = {}
        model_calls: list[dict] = []
        from agentforge_platform.models import ModelCall

        for row in session.scalars(select(ModelCall).where(ModelCall.run_id == run_id)).all():
            bucket = usage.setdefault(row.model_role, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                                       "cost_micro": 0, "unknown_calls": 0})
            bucket["calls"] += 1
            bucket["input_tokens"] += int(row.input_tokens or 0)
            bucket["output_tokens"] += int(row.output_tokens or 0)
            bucket["cost_micro"] += int(row.cost_micro or 0) if row.status == "SETTLED" else 0
            bucket["unknown_calls"] += int(row.status != "SETTLED")
            model_calls.append({
                "role": row.model_role, "provider": row.provider, "model": row.model,
                "thinking_mode": row.thinking_mode, "reasoning_effort": row.reasoning_effort,
                "input_tokens": row.input_tokens, "output_tokens": row.output_tokens,
                "cost_micro": row.cost_micro if row.status == "SETTLED" else None,
                "status": row.status,
            })
        agent_calls = sum(1 for r in session.scalars(
            select(ModelCall).where(ModelCall.run_id == run_id,
                                    ModelCall.model_role.in_(["coding", "complex_repair"]))
        ).all())
        repair_row = session.scalars(
            select(RunTask).where(RunTask.run_id == run_id, RunTask.task_id == "repair:verify")
        ).first()
        repairs = int(repair_row.attempt or 0) if repair_row is not None else 0

    asset = _read_json(run_dir / "asset-search.json") or {}
    lock = _read_json(run_dir / "asset-lock.json") or {}
    coverage = _read_json(run_dir / "coverage.json") or {}
    verification = _read_json(run_dir / "verification.json") or {}
    report = _read_json(run_dir / "report.json") or {}
    resolutions = coverage.get("resolutions", [])
    candidates = asset.get("candidate_refs", [])
    selected = asset.get("selected_refs", [])
    locked_units = asset.get("reused_method_units") or lock.get("method_units") or []
    locked_packs = [a for a in lock.get("assets", []) if a.get("kind") == "capability-pack"]
    return RunEvaluation(
        case=case, index=index, run_id=run_id, status=status,
        required_pass=(status == "SUCCEEDED" and verification.get("verified") is True
                       and (run_dir / "source.zip").is_file()),
        error_code=error_code, repairs=repairs,
        agent_calls=agent_calls, cost_micro=cost["settled_micro"], unknown_calls=cost["unknown_calls"],
        usage_by_role=usage, model_calls=model_calls, index_revision=asset.get("index_revision"),
        artifact_dir=str(run_dir), first_failure=report.get("first_failure"),
        locked_method_units=[f"{u['id']}@{u['version']}" for u in locked_units],
        locked_capability_packs=[f"{a['id']}@{a['version']}" for a in locked_packs],
        rejected_reasons={ref: reasons for ref, reasons in asset.get("filter_reasons", {}).items()
                          if reasons},
        uncovered_acceptance_ids=asset.get("uncovered_acceptance_ids", []),
        retrieval_candidates=sum(ref.startswith("method-unit:") for ref in candidates),
        compatible_selected=sum(ref.startswith("method-unit:") for ref in selected),
        pack_retrieval_candidates=sum(ref.startswith("capability-pack:") for ref in candidates),
        pack_compatible_selected=sum(ref.startswith("capability-pack:") for ref in selected),
        reuse_path=any(r.get("action") == "reuse" for r in resolutions) or bool(asset.get("reused_method_units")),
        patch_path=any(r.get("action") == "patch" for r in resolutions),
        verification_overall=(verification.get("runtime") or {}).get("verify_overall"),
        acceptance_overall=(verification.get("runtime") or {}).get("accept_overall"),
        checks=verification.get("checks", []),
        acceptance_cases=verification.get("acceptance_cases", []),
    )


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def open_session(factory):
    from agentforge_platform.db import session_scope

    return session_scope(factory)


def _probe_runner_access(base_url: str, token: str, shared_root: Path, batch_name: str) -> None:
    """用仅静态检查证明 Runner 看得见本次评估的共享目录。"""
    probe_relative = f"{batch_name}/probe"
    probe_dir = shared_root / probe_relative
    probe_dir.mkdir(parents=True)
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/verify",
        data=json.dumps({"artifact_dir": probe_relative, "checks": ["static_manifest"]}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-Runner-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if response.status != 200 or payload.get("artifact_dir") != probe_relative:
                raise RuntimeError("Runner 共享目录探测响应不匹配")
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Runner 共享目录探测失败：HTTP {error.code}") from None
    except (urllib.error.URLError, OSError) as error:
        raise RuntimeError(f"Runner 共享目录探测失败：{type(error).__name__}") from None
    finally:
        probe_dir.rmdir()


def _live_report(base_url: str) -> int:
    from alembic import command
    from alembic.config import Config

    from agentforge_platform import model_gateway
    from agentforge_platform.asset_registry import default_registry
    from agentforge_platform.db import make_engine, make_session_factory, session_scope
    from agentforge_platform.evaluation import (
        CASES,
        RUNS_PER_CASE,
        RunEvaluation,
        render_report,
        routing_assertions,
        summarize,
    )
    from agentforge_platform.generation_graph import GenerationContext, run_generation
    from agentforge_platform.models import Project, SpecVersion
    from agentforge_platform.runs import create_run
    from agentforge_platform.spec_service import canonical_json, content_hash

    raw_root = os.environ.get("RUNNER_WORK_ROOT", "")
    if not raw_root or not Path(raw_root).is_absolute():
        raise ValueError("RUNNER_WORK_ROOT 必须是平台与 Runner 共用的绝对路径")
    token = os.environ.get("RUNNER_TOKEN", "")
    if not token:
        raise ValueError("RUNNER_TOKEN 未配置")
    shared_root = Path(raw_root).resolve()
    shared_root.mkdir(parents=True, exist_ok=True)
    index_revision = _ensure_method_index()
    batch_name = f"r27-{uuid4().hex[:12]}"
    _probe_runner_access(base_url, token, shared_root, batch_name)
    results = []
    for case in CASES:
        spec = _r27_spec(SPECS[case])
        for index in range(1, RUNS_PER_CASE + 1):
            scope = f"{batch_name}/{case}-{index}"
            attempt_root = shared_root / scope
            attempt_root.mkdir(parents=True, exist_ok=False)
            db_url = f"sqlite+pysqlite:///{attempt_root / 'state.db'}"
            started = time.monotonic()
            engine = None
            try:
                config = Config(str(PLATFORM_DIR / "alembic.ini"))
                config.set_main_option("script_location", str(PLATFORM_DIR / "migrations"))
                config.set_main_option("sqlalchemy.url", db_url)
                command.upgrade(config, "head")
                engine = make_engine(db_url)
                factory = make_session_factory(engine)
                with session_scope(factory) as session:
                    project = Project(owner_id=1, name=f"{case}-{index}", requirement_text="eval")
                    session.add(project)
                    session.flush()
                    row = SpecVersion(project_id=project.id, version=1, body_json=canonical_json(spec),
                                      hash=content_hash(spec), confirmed_at=datetime(2026, 1, 1))
                    session.add(row)
                    session.flush()
                    project.active_spec_id = row.id
                    run_id = create_run(session, project, row.id, idempotency_key=f"{case}-{index}",
                                        spec_hash=content_hash(spec)).id
                context = GenerationContext(
                    session_factory=factory,
                    runner=ScopedRunnerClient(base_url=base_url, token=token, scope=scope),
                    work_root=attempt_root, registry=default_registry(),
                )
                run_generation(context, run_id)
                evaluation = _metrics(factory, attempt_root / f"run-{run_id}", run_id, case, index)
                if evaluation.index_revision != index_revision:
                    evaluation.required_pass = False
                    evaluation.error_code = "INDEX_SNAPSHOT_MISSING"
                    evaluation.note = "方法索引快照缺失或与批次快照不一致"
            except Exception as error:  # noqa: BLE001  # 单次失败不能吞掉其余八次评估
                evaluation = RunEvaluation(
                    case=case, index=index, run_id=None, status="NOT_RUN", required_pass=False,
                    error_code=type(error).__name__, note=f"评估驱动异常：{type(error).__name__}",
                    artifact_dir=str(attempt_root),
                )
            finally:
                if engine is not None:
                    engine.dispose()
            duration = time.monotonic() - started
            evaluation.duration_seconds = round(duration, 1)
            evaluation.passed_first_try = evaluation.required_pass and evaluation.repairs == 0
            results.append(evaluation)

    routes = {role: (r.provider, r.model) for role, r in model_gateway.ROUTES.items()}
    summary = summarize(results)
    DOC.write_text(
        render_report(summary, routing=routing_assertions(routes),
                      generated_at=datetime.now(UTC).isoformat(),
                      runner_note=(f"真实 Runner：{base_url}；共享根：{shared_root}；批次：{batch_name}；"
                                   f"Profile：demo-ts-mp@{R27_PROFILE_VERSION}；运行期断网；"
                                   f"方法索引快照：{index_revision}")),
        encoding="utf-8",
    )
    return 0 if summary["goal_met"] else 1


def main() -> int:
    base_url = os.environ.get("RUNNER_BASE_URL", "http://127.0.0.1:9000")
    reachable = _runner_reachable(base_url)
    print(f"# DEMO P0 评估：runner={base_url} reachable={reachable}")
    if not reachable:
        _not_run_report(base_url)
        print(f"# Runner 不可达 → 写入 NOT_RUN 报告：{DOC}")
        return 1
    try:
        return _live_report(base_url)
    except (ValueError, RuntimeError) as error:
        _not_run_report(base_url, str(error))
        print(f"# 评估前置检查失败：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
