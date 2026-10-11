"""AgentForge 受控 Runner API。

- 仅内部认证调用；除 `/health` 外都必须携带 `X-Runner-Token`；
- 只接受固定任务白名单；工件目录必须落在工作根内；
- 实际执行只经受限 docker run；执行环境不可用时如实返回 `NOT_RUN`，不伪造成功。

启动：``uv run uvicorn agentforge_runner.main:app``
"""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from agentforge_runner import __version__
from agentforge_runner.acceptance import AcceptanceResult, run_acceptance
from agentforge_runner.checks import ALL_CHECKS, VerificationResult, overall_of, run_verification
from agentforge_runner.config import RunnerConfig, load_config
from agentforge_runner.container import build_run_argv, container_runtime_available, run_container
from agentforge_runner.security import UnsafePathError, resolve_artifact_dir
from agentforge_runner.tasks import get_task

_STRICT = ConfigDict(extra="forbid")


class RunRequest(BaseModel):
    model_config = _STRICT

    task: str
    artifact_dir: str


class RunResult(BaseModel):
    model_config = _STRICT

    task: str
    status: str
    exit_code: int | None = None
    detail: str | None = None


class VerifyRequest(BaseModel):
    model_config = _STRICT

    artifact_dir: str
    checks: list[str] = []


class AcceptRequest(BaseModel):
    model_config = _STRICT

    artifact_dir: str


def create_app(config: RunnerConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    app = FastAPI(title="AgentForge Runner", version=__version__)

    def require_token(x_runner_token: str | None = Header(default=None)) -> None:
        if not cfg.token or x_runner_token != cfg.token:
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "agentforge-runner"}

    @app.post("/run", dependencies=[Depends(require_token)], response_model=RunResult)
    def run(request: RunRequest) -> RunResult:
        task = get_task(request.task)
        if task is None:
            raise HTTPException(status_code=422, detail=f"非白名单任务：{request.task}")
        try:
            host_dir = resolve_artifact_dir(cfg.work_root, request.artifact_dir)
        except UnsafePathError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if not container_runtime_available():
            return RunResult(task=task.name, status="NOT_RUN", detail="受控执行环境不可用（无 Docker）")

        name = f"af-run-{uuid.uuid4().hex[:12]}"
        argv = build_run_argv(task, host_dir, cfg, name)
        code, _output = run_container(argv, timeout=task.timeout_seconds, container_name=name)
        return RunResult(
            task=task.name,
            status="SUCCEEDED" if code == 0 else "FAILED",
            exit_code=code,
        )

    @app.post("/verify", dependencies=[Depends(require_token)], response_model=VerificationResult)
    def verify(request: VerifyRequest) -> VerificationResult:
        try:
            host_dir = resolve_artifact_dir(cfg.work_root, request.artifact_dir)
        except UnsafePathError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        names = request.checks or list(ALL_CHECKS)
        results = run_verification(host_dir, cfg, names)
        return VerificationResult(
            artifact_dir=request.artifact_dir,
            overall=overall_of(results),
            checks=results,
        )

    @app.post("/accept", dependencies=[Depends(require_token)], response_model=AcceptanceResult)
    def accept(request: AcceptRequest) -> AcceptanceResult:
        try:
            host_dir = resolve_artifact_dir(cfg.work_root, request.artifact_dir)
        except UnsafePathError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return run_acceptance(host_dir, cfg)

    return app


app = create_app()
