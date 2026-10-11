"""M16 Runner API：认证、白名单、路径、降级。"""

from __future__ import annotations

import pytest

from agentforge_runner.container import container_runtime_available

AUTH = {"X-Runner-Token": "test-runner-token"}


def test_health_is_public(runner) -> None:
    client, _ = runner

    assert client.get("/health").json()["status"] == "ok"


def test_run_requires_token(runner) -> None:
    client, _ = runner

    response = client.post("/run", json={"task": "backend_test", "artifact_dir": "r1"})

    assert response.status_code == 401


def test_run_rejects_wrong_token(runner) -> None:
    client, _ = runner

    response = client.post(
        "/run",
        json={"task": "backend_test", "artifact_dir": "r1"},
        headers={"X-Runner-Token": "wrong"},
    )

    assert response.status_code == 401


def test_run_rejects_non_whitelisted_task(runner) -> None:
    client, work = runner
    (work / "r1").mkdir()

    response = client.post(
        "/run",
        json={"task": "sh -c 'rm -rf /'", "artifact_dir": "r1"},
        headers=AUTH,
    )

    assert response.status_code == 422


def test_run_rejects_path_traversal(runner) -> None:
    client, _ = runner

    response = client.post(
        "/run",
        json={"task": "backend_test", "artifact_dir": "../escape"},
        headers=AUTH,
    )

    assert response.status_code == 422


def test_run_rejects_missing_directory(runner) -> None:
    client, _ = runner

    response = client.post(
        "/run",
        json={"task": "backend_test", "artifact_dir": "absent"},
        headers=AUTH,
    )

    assert response.status_code == 422


@pytest.mark.skipif(container_runtime_available(), reason="docker 可用时降级分支不适用")
def test_run_degrades_to_not_run_without_docker(runner) -> None:
    client, work = runner
    (work / "r1").mkdir()

    response = client.post(
        "/run",
        json={"task": "backend_test", "artifact_dir": "r1"},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "NOT_RUN"
