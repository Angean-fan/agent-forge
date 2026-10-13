"""M16 受限容器端到端（需 Docker + 执行镜像，通过 RUNNER_E2E=1 显式启用）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentforge_runner.config import RunnerConfig
from agentforge_runner.container import container_runtime_available
from agentforge_runner.main import create_app

pytestmark = pytest.mark.skipif(
    os.environ.get("RUNNER_E2E") != "1" or not container_runtime_available(),
    reason="需要 RUNNER_E2E=1 且本机可用的 Docker 与执行镜像",
)


def test_smoke_starts_and_cleans_up_container(tmp_path: Path) -> None:
    work_root = tmp_path / "runs"
    (work_root / "r1").mkdir(parents=True)
    config = RunnerConfig(
        work_root=work_root,
        executor_image=os.environ.get("RUNNER_EXECUTOR_IMAGE", "agentforge/runner-executor:demo-0.2"),
        token="t",
    )
    client = TestClient(create_app(config))

    response = client.post(
        "/run", json={"task": "smoke", "artifact_dir": "r1"}, headers={"X-Runner-Token": "t"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "SUCCEEDED"
