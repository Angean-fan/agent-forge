"""A08：Runner 单模块快检端点 POST /verify-module 单元测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentforge_runner import container as container_mod
from agentforge_runner import module_checks
from agentforge_runner.module_checks import build_module_tasks

AUTH = {"X-Runner-Token": "test-runner-token"}


def test_verify_module_requires_token(runner) -> None:
    client, _ = runner
    resp = client.post(
        "/verify-module",
        json={
            "artifact_dir": "r1",
            "module_id": "auth",
            "stack": "java",
            "test_selector": "AuthServiceTest",
        },
    )
    assert resp.status_code == 401


def test_verify_module_java_passes_and_redacts_logs(runner, monkeypatch: pytest.MonkeyPatch) -> None:
    client, work = runner
    (work / "r1").mkdir()

    seen_argvs: list[list[str]] = []

    monkeypatch.setattr(module_checks, "container_runtime_available", lambda: True)

    def _fake_run_container(argv: list[str], *, timeout: int, container_name: str) -> tuple[int, str]:
        seen_argvs.append(argv)
        assert container_name.startswith("af-mod-auth-")
        assert timeout <= 60
        return (
            0,
            "Tests run: 4, Failures: 0, Errors: 0\npassword=leaked_pw_123\ntoken=tok_secret\n",
        )

    monkeypatch.setattr(module_checks, "run_container", _fake_run_container)

    resp = client.post(
        "/verify-module",
        json={
            "artifact_dir": "r1",
            "module_id": "auth",
            "stack": "java",
            "test_selector": "com.agentforge.shop.modules.auth.AuthServiceTest#testLogin",
            "timeout_seconds": 60,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PASS"
    assert body["exit_code"] == 0
    assert body["module_id"] == "auth"
    assert body["stack"] == "java"
    assert "-Dtest=com.agentforge.shop.modules.auth.AuthServiceTest#testLogin" in body["command"]
    assert "leaked_pw_123" not in (body["log_excerpt"] or "")
    assert "tok_secret" not in (body["log_excerpt"] or "")
    # 确认走受限 docker run，无 shell
    assert len(seen_argvs) == 1
    assert "--cap-drop" in seen_argvs[0]
    assert "--read-only" in seen_argvs[0]
    assert "mvn" in seen_argvs[0]


def test_verify_module_java_reports_no_tests_as_not_run(
    runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, work = runner
    (work / "r1").mkdir()
    monkeypatch.setattr(module_checks, "container_runtime_available", lambda: True)
    monkeypatch.setattr(
        module_checks,
        "run_container",
        lambda argv, *, timeout, container_name: (0, "Tests run: 0, Failures: 0"),
    )

    resp = client.post(
        "/verify-module",
        json={
            "artifact_dir": "r1",
            "module_id": "product",
            "stack": "java",
            "test_selector": "ProductServiceTest",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "NOT_RUN"
    assert resp.json()["error_code"] == "NO_TESTS"


def test_verify_module_frontend_runs_tsc_then_vitest(
    runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, work = runner
    (work / "r1").mkdir()

    invoked_commands: list[list[str]] = []
    monkeypatch.setattr(module_checks, "container_runtime_available", lambda: True)

    def _fake_run(argv: list[str], *, timeout: int, container_name: str) -> tuple[int, str]:
        invoked_commands.append(argv)
        return 0, "ok"

    monkeypatch.setattr(module_checks, "run_container", _fake_run)

    resp = client.post(
        "/verify-module",
        json={
            "artifact_dir": "r1",
            "module_id": "cart",
            "stack": "frontend",
            "test_selector": "test/cart.test.ts",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "PASS"
    assert len(invoked_commands) == 2
    assert invoked_commands[0][-3:] == ["npx", "vue-tsc", "--noEmit"]
    assert invoked_commands[1][-4:] == ["npx", "vitest", "run", "test/cart.test.ts"]


@pytest.mark.parametrize(
    ("stack", "bad_selector"),
    [
        ("java", "AuthTest; rm -rf /"),
        ("java", "AuthTest && curl http://evil.example"),
        ("java", "AuthTest$(whoami)"),
        ("java", "-Dexec.executable=sh"),
        ("java", "../AuthTest"),
        ("java", ""),
        ("frontend", "../escape.test.ts"),
        ("frontend", "/etc/passwd"),
        ("frontend", "test/a.test.ts | cat /etc/shadow"),
        ("frontend", "--config=evil.js"),
    ],
)
def test_verify_module_rejects_injection_selectors(
    runner, stack: str, bad_selector: str
) -> None:
    client, work = runner
    (work / "r1").mkdir(exist_ok=True)

    resp = client.post(
        "/verify-module",
        json={
            "artifact_dir": "r1",
            "module_id": "auth",
            "stack": stack,
            "test_selector": bad_selector,
        },
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_verify_module_rejects_bad_module_id_and_artifact_traversal(runner) -> None:
    client, _ = runner

    r_mod = client.post(
        "/verify-module",
        json={
            "artifact_dir": "r1",
            "module_id": "../evil",
            "stack": "java",
            "test_selector": "AuthTest",
        },
        headers=AUTH,
    )
    assert r_mod.status_code == 422

    r_dir = client.post(
        "/verify-module",
        json={
            "artifact_dir": "../escape",
            "module_id": "auth",
            "stack": "java",
            "test_selector": "AuthTest",
        },
        headers=AUTH,
    )
    assert r_dir.status_code == 422


def test_verify_module_timeout_and_container_cleanup(
    runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, work = runner
    (work / "r1").mkdir()

    # 1. 验证容器超时返回 124 时，接口返回 FAIL/TIMEOUT 并附带清理说明
    monkeypatch.setattr(module_checks, "container_runtime_available", lambda: True)
    monkeypatch.setattr(
        module_checks,
        "run_container",
        lambda argv, *, timeout, container_name: (124, "container timed out"),
    )
    resp = client.post(
        "/verify-module",
        json={
            "artifact_dir": "r1",
            "module_id": "order",
            "stack": "java",
            "test_selector": "OrderServiceTest",
            "timeout_seconds": 10,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "FAIL"
    assert body["error_code"] == "TIMEOUT"
    assert body["exit_code"] == 124
    assert "超时" in (body["detail"] or "")

    # 2. 验证底层 run_container 超时时必定执行 docker rm -f <container_name>
    cleanup_calls: list[list[str]] = []

    def _fake_subprocess_run(argv, **kwargs):
        if argv[:2] == ["docker", "rm"]:
            cleanup_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(container_mod.subprocess, "run", _fake_subprocess_run)
    code, msg = container_mod.run_container(
        ["docker", "run", "img"], timeout=1, container_name="af-mod-order-abc"
    )
    assert code == 124
    assert "timed out" in msg
    assert cleanup_calls == [["docker", "rm", "-f", "af-mod-order-abc"]]


def test_verify_module_degrades_to_not_run_without_docker(
    runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, work = runner
    (work / "r1").mkdir()
    monkeypatch.setattr(module_checks, "container_runtime_available", lambda: False)

    resp = client.post(
        "/verify-module",
        json={
            "artifact_dir": "r1",
            "module_id": "payment",
            "stack": "java",
            "test_selector": "PaymentServiceTest",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "NOT_RUN"
    assert resp.json()["error_code"] == "INFRASTRUCTURE"
    assert build_module_tasks(module_id="payment", stack="java", test_selector="PaymentServiceTest")


def test_existing_verify_and_accept_contracts_unchanged(runner, tmp_path: Path) -> None:
    client, work = runner
    (work / "r1").mkdir()
    # 既有 /verify 与 /accept 鉴权与路径契约保持原样
    assert client.post("/verify", json={"artifact_dir": "r1"}).status_code == 401
    assert client.post("/accept", json={"artifact_dir": "r1"}).status_code == 401
    assert tmp_path.is_dir()


def test_verify_module_java_failure_reads_surefire_reports_directly(
    runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, work = runner
    sf = work / "r1" / "backend" / "target" / "surefire-reports"
    sf.mkdir(parents=True)
    (sf / "PaymentControllerApiTest.txt").write_text(
        "Test set: com.agentforge.template.modules.payment.PaymentControllerApiTest\n"
        "Tests run: 2, Failures: 1, Errors: 0, Skipped: 0 <<< FAILURE!\n"
        "com.agentforge.template.modules.payment.PaymentControllerApiTest.pay_denied -- Time elapsed: 0.04 s <<< FAILURE!\n"
        "java.lang.AssertionError: Expecting code to raise a throwable.\n"
        "\tat com.agentforge.template.modules.payment.PaymentControllerApiTest.pay_denied(PaymentControllerApiTest.java:144)\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(module_checks, "container_runtime_available", lambda: True)
    monkeypatch.setattr(
        module_checks,
        "run_container",
        lambda argv, *, timeout, container_name: (
            1,
            (
                "[ERROR] Failed to execute goal surefire:test\n"
                "[ERROR] [Help 1] http://cwiki.apache.org/confluence/display/MAVEN/MojoFailureException\n"
            ),
        ),
    )

    resp = client.post(
        "/verify-module",
        json={
            "artifact_dir": "r1",
            "module_id": "payment",
            "stack": "java",
            "test_selector": "PaymentControllerApiTest",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "FAIL"
    excerpt = body["log_excerpt"] or ""
    assert "[surefire-failures]" in excerpt
    assert "Expecting code to raise a throwable." in excerpt
    assert "PaymentControllerApiTest.java:144" in excerpt
    assert "[Help 1]" not in excerpt
