from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentforge_runner.config import RunnerConfig
from agentforge_runner.main import create_app

TOKEN = "test-runner-token"


@pytest.fixture
def runner(tmp_path: Path):
    work_root = tmp_path / "runs"
    work_root.mkdir()
    config = RunnerConfig(
        work_root=work_root,
        executor_image="agentforge/runner-executor:demo-0.1",
        token=TOKEN,
    )
    return TestClient(create_app(config)), work_root
