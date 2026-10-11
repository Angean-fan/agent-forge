"""M16 容器隔离契约（不依赖 Docker，仅检查生成的 docker run 参数）。"""

from __future__ import annotations

from pathlib import Path

from agentforge_runner.config import RunnerConfig
from agentforge_runner.container import build_run_argv
from agentforge_runner.tasks import get_task

IMAGE = "agentforge/runner-executor@sha256:" + "a" * 64


def _argv() -> list[str]:
    config = RunnerConfig(work_root=Path("/srv/runs"), executor_image=IMAGE, token="t")
    task = get_task("backend_test")
    assert task is not None
    return build_run_argv(task, Path("/srv/runs/r1"), config, "af-run-1")


def _value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_isolation_flags() -> None:
    argv = _argv()

    assert _value(argv, "--network") == "none"
    assert "--read-only" in argv
    assert _value(argv, "--cap-drop") == "ALL"
    assert _value(argv, "--security-opt") == "no-new-privileges"
    assert _value(argv, "--user") == "1000:1000"
    assert "--pids-limit" in argv
    assert _value(argv, "--memory") == "2g"
    assert _value(argv, "--cpus") == "2"
    assert "--rm" in argv


def test_no_host_docker_socket_and_single_workspace_mount() -> None:
    argv = _argv()
    joined = " ".join(argv)

    assert "docker.sock" not in joined
    mounts = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--mount"]
    assert len(mounts) == 1
    assert "/srv/runs/r1" in mounts[0]
    assert mounts[0].endswith("dst=/workspace")


def test_command_is_fixed_argv_not_shell() -> None:
    argv = _argv()

    assert "sh" not in argv
    assert "bash" not in argv
    assert "-c" not in argv
    image_index = argv.index(IMAGE)
    assert argv[image_index + 1:] == ["mvn", "-B", "test"]
