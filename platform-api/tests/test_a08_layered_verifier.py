"""A08：分层验证器（DockerRunnerModuleVerifier / LocalProcessModuleVerifier / 总验收强隔离守卫）测试。"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.module_verifier import (
    LOCAL_DEPT_TEST_RUNNER_ENV,
    DockerRunnerModuleVerifier,
    LocalProcessModuleVerifier,
    build_module_verifier,
    build_sanitized_env,
    local_dept_test_runner_enabled,
    redact_and_truncate_log,
    require_docker_runner_for_final_acceptance,
    run_final_acceptance_accept,
    run_final_acceptance_verify,
)
from agentforge_platform.runner_client import HttpRunnerClient


def _make_executable_script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _FakeDockerRunnerClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def verify(self, artifact_dir: str) -> dict:
        self.calls.append(("verify", {"artifact_dir": artifact_dir}))
        return {"overall": "PASS", "checks": []}

    def accept(self, artifact_dir: str) -> dict:
        self.calls.append(("accept", {"artifact_dir": artifact_dir}))
        return {"overall": "PASS", "cases": []}

    def verify_module(
        self,
        *,
        artifact_dir: str,
        module_id: str,
        stack: str,
        test_selector: str,
        timeout_seconds: int = 120,
    ) -> dict:
        payload = {
            "artifact_dir": artifact_dir,
            "module_id": module_id,
            "stack": stack,
            "test_selector": test_selector,
            "timeout_seconds": timeout_seconds,
        }
        self.calls.append(("verify_module", payload))
        return {
            "artifact_dir": artifact_dir,
            "module_id": module_id,
            "stack": stack,
            "test_selector": test_selector,
            "status": "PASS",
            "exit_code": 0,
            "duration_ms": 42,
            "command": ["mvn", "-B", "test", f"-Dtest={test_selector}"],
            "log_excerpt": "Tests run: 2, Failures: 0",
        }


def test_default_verifier_is_docker_runner_and_local_runner_is_disabled_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LOCAL_DEPT_TEST_RUNNER_ENV, raising=False)
    assert local_dept_test_runner_enabled() is False

    fake_client = _FakeDockerRunnerClient()
    verifier = build_module_verifier(runner_client=fake_client, work_root=tmp_path)
    assert isinstance(verifier, DockerRunnerModuleVerifier)

    res = verifier.verify_module(
        artifact_dir="run-01",
        module_id="auth",
        stack="java",
        test_selector="AuthServiceTest",
    )
    assert res.status == "PASS"
    assert res.verifier_kind == "docker_runner"
    assert fake_client.calls[0][0] == "verify_module"

    # 默认开关关闭时直接调用 LocalProcessModuleVerifier 必须被拒绝
    local_verifier = LocalProcessModuleVerifier(work_root=tmp_path)
    (tmp_path / "run-01" / "backend").mkdir(parents=True)
    with pytest.raises(AppException) as exc_info:
        local_verifier.verify_module(
            artifact_dir="run-01",
            module_id="auth",
            stack="java",
            test_selector="AuthServiceTest",
        )
    assert exc_info.value.code == ErrorCode.FORBIDDEN


def test_local_verifier_strips_sensitive_env_vars_in_real_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOCAL_DEPT_TEST_RUNNER_ENV, "1")
    # 注入一批高敏环境变量，验证子进程绝对看不到任何一个
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-super-secret-value")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-super-secret-value")
    monkeypatch.setenv(
        "AGENTFORGE_DATABASE_URL",
        "postgresql+psycopg://angean:myRealPass@127.0.0.1:15432/agentforge",
    )
    monkeypatch.setenv("RUNNER_TOKEN", "runner-secret-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-key")

    sanitized = build_sanitized_env()
    for secret_key in (
        "DEEPSEEK_API_KEY",
        "DASHSCOPE_API_KEY",
        "AGENTFORGE_DATABASE_URL",
        "RUNNER_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
    ):
        assert secret_key not in sanitized

    # 真实子进程实测：放一个模拟 mvn 脚本在临时 PATH 下，把子进程实际看到的环境变量落盘校验
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    dump_file = tmp_path / "child_env.json"
    _make_executable_script(
        fake_bin / "mvn",
        f"#!{sys.executable}\n"
        "import json, os\n"
        f"open({str(dump_file)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
        "print('Tests run: 3, Failures: 0')\n",
    )
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    work_root = tmp_path / "runs"
    (work_root / "r1" / "backend").mkdir(parents=True)
    verifier = LocalProcessModuleVerifier(work_root=work_root)

    res = verifier.verify_module(
        artifact_dir="r1",
        module_id="auth",
        stack="java",
        test_selector="AuthServiceTest",
    )
    assert res.status == "PASS"
    assert res.verifier_kind == "local_process"

    child_env = json.loads(dump_file.read_text(encoding="utf-8"))
    for secret_key in (
        "DEEPSEEK_API_KEY",
        "DASHSCOPE_API_KEY",
        "AGENTFORGE_DATABASE_URL",
        "RUNNER_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
    ):
        assert secret_key not in child_env
    assert child_env.get("CI") == "true"


def test_local_verifier_enforces_command_whitelist_selector_and_workdir_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOCAL_DEPT_TEST_RUNNER_ENV, "1")
    work_root = tmp_path / "runs"
    (work_root / "r1" / "backend").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    verifier = LocalProcessModuleVerifier(work_root=work_root)

    # 1. 非白名单命令被硬拒绝（绝不允许 sh/bash/python/curl 或绝对路径）
    for bad_cmd in (["sh", "-c", "id"], ["bash", "-c", "ls"], ["curl", "http://127.0.0.1"], ["/bin/sh"]):
        with pytest.raises(AppException) as exc_info:
            verifier._run_whitelisted_command(bad_cmd, cwd=work_root / "r1", timeout_seconds=5)
        assert exc_info.value.code == ErrorCode.FORBIDDEN

    # 2. 注入型选择器被拦截
    for bad_sel in ("AuthTest; id", "AuthTest && whoami", "../AuthTest", "-Dexec.args=1"):
        with pytest.raises(AppException) as exc_info:
            verifier.verify_module(
                artifact_dir="r1",
                module_id="auth",
                stack="java",
                test_selector=bad_sel,
            )
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    # 3. 越界工作目录被拦截
    for bad_dir in ("../outside", str(outside)):
        with pytest.raises(AppException) as exc_info:
            verifier.verify_module(
                artifact_dir=bad_dir,
                module_id="auth",
                stack="java",
                test_selector="AuthServiceTest",
            )
        assert exc_info.value.code == ErrorCode.FORBIDDEN


def test_local_verifier_kills_entire_process_tree_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOCAL_DEPT_TEST_RUNNER_ENV, "1")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    grandchild_pid_file = tmp_path / "grandchild.pid"

    # 模拟 mvn 启动一个长驻子进程（孙进程），验证超时后整棵进程树被强杀，无孤儿残留
    _make_executable_script(
        fake_bin / "mvn",
        f"#!{sys.executable}\n"
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(grandchild_pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n",
    )
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    work_root = tmp_path / "runs"
    (work_root / "r1" / "backend").mkdir(parents=True)
    verifier = LocalProcessModuleVerifier(work_root=work_root)

    started = time.monotonic()
    res = verifier.verify_module(
        artifact_dir="r1",
        module_id="order",
        stack="java",
        test_selector="OrderServiceTest",
        timeout_seconds=1,
    )
    elapsed = time.monotonic() - started

    assert res.status == "FAIL"
    assert res.error_code == "TIMEOUT"
    assert res.exit_code == 124
    assert elapsed < 8.0

    # 确认孙进程也已随进程组被强杀（进程不存在）
    assert grandchild_pid_file.is_file()
    gc_pid = int(grandchild_pid_file.read_text(encoding="utf-8").strip())
    # 给内核极短暂回收窗口
    alive = True
    for _ in range(20):
        try:
            os.kill(gc_pid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            alive = False
            break
    assert alive is False, f"超时强杀后孙进程 {gc_pid} 仍存活"


def test_local_verifier_redacts_secrets_and_truncates_long_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOCAL_DEPT_TEST_RUNNER_ENV, "1")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _make_executable_script(
        fake_bin / "mvn",
        f"#!{sys.executable}\n"
        "import sys\n"
        "print('sk-secretkey1234567890abcdef')\n"
        "print('postgresql+psycopg://angean:UltraSecretPwd@127.0.0.1:15432/agentforge')\n"
        "print('password=TopSecret999 token=ghp_1234567890abcdefghijklmnop')\n"
        "print('X' * 8000)\n"
        "sys.exit(1)\n",
    )
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    work_root = tmp_path / "runs"
    (work_root / "r1" / "backend").mkdir(parents=True)
    verifier = LocalProcessModuleVerifier(work_root=work_root, log_limit=600)

    res = verifier.verify_module(
        artifact_dir="r1",
        module_id="payment",
        stack="java",
        test_selector="PaymentServiceTest",
    )
    assert res.status == "FAIL"
    assert res.error_code == "BUILD_FAILED"
    excerpt = res.log_excerpt or ""
    assert "sk-secretkey1234567890abcdef" not in excerpt
    assert "UltraSecretPwd" not in excerpt
    assert "TopSecret999" not in excerpt
    assert "ghp_1234567890abcdefghijklmnop" not in excerpt
    assert "truncated" in excerpt
    assert len(excerpt) < 700

    # 独立校验 redact_and_truncate_log
    direct = redact_and_truncate_log("eyJhbGciOi.payload123.sig456\n" + "Z" * 5000, limit=200)
    assert "eyJhbGciOi" not in direct
    assert "<jwt>" in direct
    assert "truncated" in direct


def test_final_acceptance_strictly_forbids_local_process_verifier(tmp_path: Path) -> None:
    local_verifier = LocalProcessModuleVerifier(work_root=tmp_path)

    # 1. 总验收安全守卫函数直接抛出 403 FORBIDDEN
    with pytest.raises(AppException) as exc1:
        require_docker_runner_for_final_acceptance(local_verifier)
    assert exc1.value.code == ErrorCode.FORBIDDEN
    assert "Docker Runner" in exc1.value.message

    with pytest.raises(AppException) as exc2:
        run_final_acceptance_verify(local_verifier, "run-01")  # type: ignore[arg-type]
    assert exc2.value.code == ErrorCode.FORBIDDEN

    with pytest.raises(AppException) as exc3:
        run_final_acceptance_accept(local_verifier, "run-01")  # type: ignore[arg-type]
    assert exc3.value.code == ErrorCode.FORBIDDEN

    # 2. 直接在 LocalProcessModuleVerifier 上调用 verify/accept 同样抛出 403 FORBIDDEN
    with pytest.raises(AppException) as exc4:
        local_verifier.verify("run-01")
    assert exc4.value.code == ErrorCode.FORBIDDEN

    with pytest.raises(AppException) as exc5:
        local_verifier.accept("run-01")
    assert exc5.value.code == ErrorCode.FORBIDDEN

    # 3. DockerRunnerModuleVerifier 禁止包装 LocalProcessModuleVerifier
    with pytest.raises(AppException) as exc6:
        DockerRunnerModuleVerifier(local_verifier)
    assert exc6.value.code == ErrorCode.FORBIDDEN

    # 4. 合法 Docker Runner 客户端（HttpRunnerClient / 符合协议的测试替身）通过总验收守卫
    http_client = HttpRunnerClient(base_url="http://127.0.0.1:18080", token="t")
    assert require_docker_runner_for_final_acceptance(http_client) is http_client

    fake_docker = _FakeDockerRunnerClient()
    assert run_final_acceptance_verify(fake_docker, "run-01")["overall"] == "PASS"
    assert run_final_acceptance_accept(fake_docker, "run-01")["overall"] == "PASS"
