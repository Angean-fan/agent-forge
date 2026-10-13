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
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

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


def _runner_reachable(base_url: str, timeout: int = 4) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/health", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _not_run_report(base_url: str) -> None:
    from agentforge_platform import model_gateway
    from agentforge_platform.evaluation import (
        CASES,
        RUNS_PER_CASE,
        RunEvaluation,
        render_report,
        routing_assertions,
        summarize,
    )

    runs = [
        RunEvaluation(case=case, index=index, run_id=None, status="NOT_RUN", required_pass=False,
                      note=f"Runner 不可达（{base_url}）：必需构建/验收未运行，计为失败")
        for case in CASES for index in range(1, RUNS_PER_CASE + 1)
    ]
    routes = {role: (r.provider, r.model) for role, r in model_gateway.ROUTES.items()}
    summary = summarize(runs)
    DOC.write_text(
        render_report(
            summary, routing=routing_assertions(routes),
            generated_at=datetime.now(UTC).isoformat(),
            runner_note=f"Runner 不可达（{base_url}）——本报告 NOT_RUN，未达到目标。",
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
        from agentforge_platform.models import ModelCall

        for row in session.scalars(select(ModelCall).where(ModelCall.run_id == run_id)).all():
            bucket = usage.setdefault(row.model_role, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                                       "cost_micro": 0, "unknown_calls": 0})
            bucket["calls"] += 1
            bucket["input_tokens"] += int(row.input_tokens or 0)
            bucket["output_tokens"] += int(row.output_tokens or 0)
            bucket["cost_micro"] += int(row.cost_micro or 0)
            bucket["unknown_calls"] += int(row.status != "SETTLED")
        agent_calls = sum(1 for r in session.scalars(
            select(ModelCall).where(ModelCall.run_id == run_id,
                                    ModelCall.model_role.in_(["coding", "complex_repair"]))
        ).all())
        repair_row = session.scalars(
            select(RunTask).where(RunTask.run_id == run_id, RunTask.task_id == "repair:verify")
        ).first()
        repairs = int(repair_row.attempt or 0) if repair_row is not None else 0

    asset = _read_json(run_dir / "asset-search.json") or {}
    coverage = _read_json(run_dir / "coverage.json") or {}
    verification = _read_json(run_dir / "verification.json") or {}
    resolutions = coverage.get("resolutions", [])
    return RunEvaluation(
        case=case, index=index, run_id=run_id, status=status,
        required_pass=status == "SUCCEEDED", error_code=error_code, repairs=repairs,
        agent_calls=agent_calls, cost_micro=cost["settled_micro"], unknown_calls=cost["unknown_calls"],
        usage_by_role=usage, index_revision=asset.get("index_revision"),
        locked_method_units=[f"{u['id']}@{u['version']}" for u in asset.get("reused_method_units", [])],
        rejected_reasons=asset.get("filter_reasons", {}),
        retrieval_candidates=len(asset.get("candidate_refs", [])),
        compatible_selected=len(asset.get("selected_refs", [])),
        reuse_path=any(r.get("action") == "reuse" for r in resolutions) or bool(asset.get("reused_method_units")),
        patch_path=any(r.get("action") == "patch" for r in resolutions),
        verification_overall=(verification.get("runtime") or {}).get("verify_overall"),
        acceptance_overall=(verification.get("runtime") or {}).get("accept_overall"),
        checks=verification.get("checks", []),
    )


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def open_session(factory):
    from agentforge_platform.db import session_scope

    return session_scope(factory)


def _live_report(base_url: str) -> int:
    from alembic import command
    from alembic.config import Config

    from agentforge_platform import model_gateway
    from agentforge_platform.asset_registry import default_registry
    from agentforge_platform.db import make_engine, make_session_factory, session_scope
    from agentforge_platform.evaluation import (
        CASES,
        RUNS_PER_CASE,
        render_report,
        routing_assertions,
        summarize,
    )
    from agentforge_platform.generation_graph import GenerationContext, run_generation
    from agentforge_platform.models import Project, SpecVersion
    from agentforge_platform.runner_client import HttpRunnerClient
    from agentforge_platform.runs import create_run
    from agentforge_platform.spec import SoftwareSpecification
    from agentforge_platform.spec_service import canonical_json, content_hash

    work_root = Path(tempfile.mkdtemp(prefix="demo-p0-"))
    results = []
    for case in CASES:
        spec = SoftwareSpecification.model_validate_json(SPECS[case].read_text(encoding="utf-8"))
        for index in range(1, RUNS_PER_CASE + 1):
            db_url = f"sqlite+pysqlite:///{work_root / f'{case}-{index}.db'}"
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
            started = time.monotonic()
            context = GenerationContext(
                session_factory=factory,
                runner=HttpRunnerClient(base_url=base_url, token=os.environ.get("RUNNER_TOKEN", "")),
                work_root=work_root / "runs", registry=default_registry(),
            )
            run_generation(context, run_id)
            duration = time.monotonic() - started
            evaluation = _metrics(factory, work_root / "runs" / f"run-{run_id}", run_id, case, index)
            evaluation.duration_seconds = round(duration, 1)
            evaluation.passed_first_try = evaluation.required_pass and evaluation.repairs == 0
            results.append(evaluation)
            engine.dispose()

    routes = {role: (r.provider, r.model) for role, r in model_gateway.ROUTES.items()}
    summary = summarize(results)
    DOC.write_text(
        render_report(summary, routing=routing_assertions(routes),
                      generated_at=datetime.now(UTC).isoformat(),
                      runner_note=f"真实 Runner：{base_url}"),
        encoding="utf-8",
    )
    return 0 if summary["goal_met"] else 1


def main() -> int:
    base_url = os.environ.get("RUNNER_BASE_URL", "http://127.0.0.1:9000")
    print(f"# DEMO P0 评估：runner={base_url} reachable={_runner_reachable(base_url)}")
    if not _runner_reachable(base_url):
        _not_run_report(base_url)
        print(f"# Runner 不可达 → 写入 NOT_RUN 报告：{DOC}")
        return 1
    return _live_report(base_url)


if __name__ == "__main__":
    raise SystemExit(main())
