"""容器执行契约。

把固定任务翻译成一条**受限 docker run**（仅此一种执行方式，不暴露任意 shell）：
非 root、只读根、无网络、丢弃全部 capability、禁提权、资源与进程限制；
只挂载本次 run 的工件目录，绝不挂载宿主 docker socket、平台库、模型密钥或宿主敏感目录。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from agentforge_runner.config import RunnerConfig
from agentforge_runner.tasks import Task

DOCKER = "docker"
TIMEOUT_EXIT_CODE = 124


def container_runtime_available() -> bool:
    return shutil.which(DOCKER) is not None


def build_run_argv(task: Task, host_dir: Path, config: RunnerConfig, name: str) -> list[str]:
    return [
        DOCKER,
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=256m,mode=1777",
        "--user",
        "1000:1000",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--memory",
        "2g",
        "--cpus",
        "2",
        "--workdir",
        task.workdir,
        "--mount",
        f"type=bind,src={Path(host_dir).resolve()},dst=/workspace",
        config.executor_image,
        *task.command,
    ]


def run_container(argv: list[str], *, timeout: int, container_name: str) -> int:
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        subprocess.run([DOCKER, "rm", "-f", container_name], capture_output=True, check=False)
        return TIMEOUT_EXIT_CODE
    return completed.returncode
