"""Runner 运行配置（全部来自环境变量；无硬编码密钥）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunnerConfig:
    work_root: Path
    executor_image: str
    token: str


def load_config() -> RunnerConfig:
    return RunnerConfig(
        work_root=Path(os.environ.get("RUNNER_WORK_ROOT", "./var/runs")),
        executor_image=os.environ.get(
            "RUNNER_EXECUTOR_IMAGE", "agentforge/runner-executor:demo-0.1"
        ),
        token=os.environ.get("RUNNER_TOKEN", ""),
    )
