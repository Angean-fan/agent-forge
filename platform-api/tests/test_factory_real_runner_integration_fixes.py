"""回归测试：修复实机闭环 4 处断点（工厂规格确认、per-run mode 路由、Runner 相对路径与 checks、验收前置文件）。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.factory_pipeline import FACTORY_PIPELINE_ENV
from agentforge_platform.models import Run
from agentforge_platform.module_verifier import (
    DockerRunnerModuleVerifier,
    run_final_acceptance_verify,
    to_runner_artifact_dir,
)
from agentforge_platform.runs import requested_run_mode
from agentforge_platform.worker import build_generation_handler, run_worker

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_fp09_fullstack_factory_delivery import (
    ALICE_CREDS,
    _build_scripted_factory_runtime,
    _configure_models,
    _make_ecommerce_spec,
)


def _login_alice(app) -> tuple[TestClient, dict[str, str]]:
    client = TestClient(app)
    resp = client.post("/api/auth/login", json=ALICE_CREDS)
    assert resp.status_code == 200, resp.text
    csrf = client.cookies.get("af_csrf")
    assert csrf
    return client, {"X-CSRF-Token": csrf}


def _create_project(client: TestClient, headers: dict[str, str], name: str = "电商沙箱") -> int:
    resp = client.post(
        "/api/projects",
        json={"name": name, "requirement": "登录注册、商品展示、购物车、订单与本地模拟支付"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["data"]["id"])


def test_confirm_five_entity_ecommerce_spec_via_http_api_in_factory_mode(
    platform_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 1：工厂开关开启时，5 实体电商规格可经真实 PUT /spec + POST /confirm 冻结；关闭时仍拒。"""
    app, _factory = platform_app
    client, headers = _login_alice(app)
    project_id = _create_project(client, headers)

    spec_payload = _make_ecommerce_spec().model_dump(mode="json")
    put_resp = client.put(
        f"/api/projects/{project_id}/spec",
        json={"version": 0, "draft_revision": 0, "spec": spec_payload},
        headers=headers,
    )
    assert put_resp.status_code == 200, put_resp.text
    draft = put_resp.json()["data"]

    # 开关关闭：严格保持 Demo 3 实体限制（零回归）
    monkeypatch.setenv(FACTORY_PIPELINE_ENV, "0")
    off_resp = client.post(
        f"/api/projects/{project_id}/confirm",
        json={"version": draft["version"], "draft_revision": draft["draft_revision"]},
        headers=headers,
    )
    assert off_resp.status_code == 422
    assert off_resp.json()["error"]["code"] == "UNSUPPORTED_SCOPE"

    # 开关开启：5 实体电商规格经正常 API 确认冻结成功
    monkeypatch.setenv(FACTORY_PIPELINE_ENV, "1")
    on_resp = client.post(
        f"/api/projects/{project_id}/confirm",
        json={"version": draft["version"], "draft_revision": draft["draft_revision"]},
        headers=headers,
    )
    assert on_resp.status_code == 200, on_resp.text
    confirmed = on_resp.json()["data"]
    assert confirmed["confirmed_at"] is not None

    # 负例：缺少 id 主键的 5 实体规格仍应被拒（422 SPEC_NOT_READY）
    bad_project_id = _create_project(client, headers, name="缺主键电商")
    bad_spec = _make_ecommerce_spec().model_dump(mode="json")
    bad_spec["entities"][0]["fields"] = [{"name": "username", "type": "string", "required": True}]
    bad_put = client.put(
        f"/api/projects/{bad_project_id}/spec",
        json={"version": 0, "draft_revision": 0, "spec": bad_spec},
        headers=headers,
    )
    assert bad_put.status_code == 200
    bad_draft = bad_put.json()["data"]
    bad_confirm = client.post(
        f"/api/projects/{bad_project_id}/confirm",
        json={"version": bad_draft["version"], "draft_revision": bad_draft["draft_revision"]},
        headers=headers,
    )
    assert bad_confirm.status_code == 422
    assert bad_confirm.json()["error"]["code"] == "SPEC_NOT_READY"


def test_per_run_mode_routing_and_flag_guard(
    platform_app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 2：POST /runs 的 mode 字段在范围校验与 worker 分发中真实生效。"""
    app, factory = platform_app
    client, headers = _login_alice(app)

    # 准备一个 1 实体合法标准项目（Task）
    project_id = _create_project(client, headers, name="双模式测试项目")
    task_spec = {
        "schema_version": "0.1",
        "project_type": "crud_web",
        "goal": "个人任务管理",
        "actors": ["ADMIN", "MEMBER"],
        "entities": [
            {
                "name": "Task",
                "source": "user_confirmed",
                "fields": [
                    {"name": "id", "type": "long", "required": True},
                    {"name": "title", "type": "string", "required": True},
                    {"name": "description", "type": "text", "required": False},
                    {"name": "status", "type": "string", "required": True},
                    {"name": "owner_id", "type": "long", "required": True},
                    {"name": "created_at", "type": "datetime", "required": True},
                ],
            }
        ],
        "relations": [],
        "pages": [{"name": "TaskList", "entity": "Task", "path": "/tasks"}],
        "permissions": [
            {"role": "ADMIN", "actions": ["create", "read", "update", "delete"], "source": "user_confirmed"},
            {"role": "MEMBER", "actions": ["create", "read", "update"], "source": "user_confirmed"},
        ],
        "business_rules": [{"id": "BR01", "text": "任务标题必填", "source": "user_confirmed"}],
        "acceptance_cases": [
            {"id": "AC01", "scenario": "创建任务", "expected": "成功落库", "source": "user_confirmed"}
        ],
        "technical_profile": {
            "profile_id": "demo-ts-mp",
            "profile_version": "0.3",
            "template_version": "demo-0.2",
        },
        "constraints": [],
        "exclusions": [],
        "capability_requests": [],
        "recipe_requests": [],
        "integrations": [],
        "external_requirements": [],
    }
    put_resp = client.put(
        f"/api/projects/{project_id}/spec",
        json={"version": 0, "draft_revision": 0, "spec": task_spec},
        headers=headers,
    )
    assert put_resp.status_code == 200, put_resp.text
    draft = put_resp.json()["data"]
    confirm_resp = client.post(
        f"/api/projects/{project_id}/confirm",
        json={"version": draft["version"], "draft_revision": draft["draft_revision"]},
        headers=headers,
    )
    assert confirm_resp.status_code == 200, confirm_resp.text

    # 1) 工厂开关关闭时，前端显式请求 mode="factory" 必须明确拒绝（422），不得静默走标准装配
    monkeypatch.setenv(FACTORY_PIPELINE_ENV, "0")
    rejected = client.post(
        f"/api/projects/{project_id}/runs",
        json={"mode": "factory"},
        headers={**headers, "Idempotency-Key": "mode-factory-when-flag-off"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "VALIDATION_ERROR"

    # 2) 工厂开关开启时，前端显式请求 mode="standard" 必须路由到旧串行 generation_graph，而不进入工厂流水线
    monkeypatch.setenv(FACTORY_PIPELINE_ENV, "1")
    work_root = tmp_path / "work-mode-standard"
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(work_root))

    std_resp = client.post(
        f"/api/projects/{project_id}/runs",
        json={"mode": "standard"},
        headers={**headers, "Idempotency-Key": "mode-standard-when-flag-on"},
    )
    assert std_resp.status_code == 202, std_resp.text
    std_run_id = int(std_resp.json()["data"]["id"])

    with session_scope(factory) as db:
        assert requested_run_mode(db, std_run_id) == "standard"

    class _RecordingStandardRunner:
        def __init__(self) -> None:
            self.verify_dirs: list[str] = []

        def verify(self, artifact_dir: str) -> dict[str, Any]:
            self.verify_dirs.append(artifact_dir)
            return {
                "overall": "PASS",
                "checks": [
                    {"name": "static_manifest", "status": "PASS"},
                    {"name": "backend_test", "status": "PASS"},
                    {"name": "backend_build", "status": "PASS"},
                    {"name": "frontend_build", "status": "PASS"},
                ],
            }

        def accept(self, artifact_dir: str) -> dict[str, Any]:
            return {"overall": "PASS", "cases": [{"name": "create_task", "status": "PASS"}]}

    runner = _RecordingStandardRunner()
    run_worker(
        session_factory=factory,
        handler=build_generation_handler(factory, runner=runner),
        worker_id="worker-mode-standard",
        iterations=1,
        poll_interval=0.01,
    )

    with session_scope(factory) as db:
        run_row = db.get(Run, std_run_id)
        assert run_row is not None
        assert run_row.status == "SUCCEEDED"
    # 确认走的是标准装配路径（无 factory_blueprint / staging 目录，且 /factory 返回 factory_active=false）
    factory_view = client.get(f"/api/runs/{std_run_id}/factory").json()["data"]
    assert factory_view["factory_active"] is False
    assert runner.verify_dirs == [f"run-{std_run_id}"]


def test_runner_relative_path_normalization_and_verifier_guard(tmp_path: Path) -> None:
    """Fix 3a：DockerRunnerModuleVerifier 与 to_runner_artifact_dir 强制输出相对路径并拦截越界。"""
    work_root = tmp_path / "work"
    dept_dir = work_root / "run-7" / "staging" / "backend" / "order"
    dept_dir.mkdir(parents=True)

    assert to_runner_artifact_dir(dept_dir, work_root) == "run-7/staging/backend/order"
    assert to_runner_artifact_dir("run-7/staging/backend/order", work_root) == "run-7/staging/backend/order"

    with pytest.raises(AppException) as exc_info:
        to_runner_artifact_dir(tmp_path / "outside", work_root)
    assert exc_info.value.code == ErrorCode.FORBIDDEN

    with pytest.raises(AppException) as exc_info2:
        to_runner_artifact_dir("../escape", work_root)
    assert exc_info2.value.code == ErrorCode.FORBIDDEN

    captured: list[dict[str, Any]] = []

    class _CaptureRunnerClient:
        def verify_module(self, **kwargs: Any) -> dict[str, Any]:
            captured.append(kwargs)
            return {
                "artifact_dir": kwargs["artifact_dir"],
                "module_id": kwargs["module_id"],
                "stack": kwargs["stack"],
                "test_selector": kwargs["test_selector"],
                "status": "PASS",
                "exit_code": 0,
            }

    verifier = DockerRunnerModuleVerifier(_CaptureRunnerClient(), work_root=work_root)
    res = verifier.verify_module(
        artifact_dir=dept_dir,
        module_id="order",
        stack="java",
        test_selector="OrderServiceTest",
    )
    assert res.status == "PASS"
    assert captured[0]["artifact_dir"] == "run-7/staging/backend/order"
    assert not captured[0]["artifact_dir"].startswith("/")


def test_factory_pipeline_passes_relative_paths_checks_and_prewrites_asset_lock(
    platform_app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 3b & Fix 4：验证后端总装与全栈总装传给真实风格 Runner 的路径均为相对路径、带 checks 且前置落盘 asset-lock.json。"""
    app, factory = platform_app
    _configure_models(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_FP09_KEY", "sk-fake-fp09-key")
    monkeypatch.setenv(FACTORY_PIPELINE_ENV, "1")
    work_root = tmp_path / "work-relative"
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(work_root))

    client, headers = _login_alice(app)
    project_id = _create_project(client, headers, name="相对路径电商闭环")
    spec = _make_ecommerce_spec()

    # 通过真实 HTTP API 保存并确认 5 实体电商规格（同步覆盖 Fix 1）
    put_resp = client.put(
        f"/api/projects/{project_id}/spec",
        json={"version": 0, "draft_revision": 0, "spec": spec.model_dump(mode="json")},
        headers=headers,
    )
    assert put_resp.status_code == 200, put_resp.text
    draft = put_resp.json()["data"]
    conf_resp = client.post(
        f"/api/projects/{project_id}/confirm",
        json={"version": draft["version"], "draft_revision": draft["draft_revision"]},
        headers=headers,
    )
    assert conf_resp.status_code == 200, conf_resp.text

    run_resp = client.post(
        f"/api/projects/{project_id}/runs",
        json={"mode": "factory"},
        headers={**headers, "Idempotency-Key": "relative-runner-check-1"},
    )
    assert run_resp.status_code == 202, run_resp.text
    run_id = int(run_resp.json()["data"]["id"])

    class _StrictRelativeRunner:
        """严格按真实 Runner security.resolve_artifact_dir 规则校验：拒收绝对路径并校验前置文件。"""

        def __init__(self, root: Path) -> None:
            self.root = root.resolve()
            self.verify_calls: list[tuple[str, list[str] | None]] = []
            self.accept_calls: list[str] = []

        def _assert_safe_relative(self, artifact_dir: str) -> Path:
            assert artifact_dir and not artifact_dir.startswith("/"), f"绝不允许绝对路径：{artifact_dir!r}"
            assert "\\" not in artifact_dir and ".." not in Path(artifact_dir).parts
            resolved = (self.root / artifact_dir).resolve()
            assert resolved.is_dir(), f"目录不存在：{resolved}"
            return resolved

        def verify(self, artifact_dir: str, *, checks: list[str] | None = None) -> dict[str, Any]:
            host_dir = self._assert_safe_relative(artifact_dir)
            self.verify_calls.append((artifact_dir, checks))
            # 核对前置文件在 /verify 前已就绪
            assert (host_dir / "asset-lock.json").is_file(), f"缺少 asset-lock.json: {host_dir}"
            assert (host_dir / "compose.yaml").is_file(), f"缺少 compose.yaml: {host_dir}"
            if checks is not None:
                return {
                    "overall": "PASS",
                    "checks": [{"name": c, "status": "PASS"} for c in checks],
                }
            assert (host_dir / "spec.json").is_file(), f"全栈验收前缺少 spec.json: {host_dir}"
            assert (host_dir / "frontend" / "package.json").is_file()
            return {
                "overall": "PASS",
                "checks": [
                    {"name": "static_manifest", "status": "PASS"},
                    {"name": "backend_test", "status": "PASS"},
                    {"name": "backend_build", "status": "PASS"},
                    {"name": "frontend_build", "status": "PASS"},
                ],
            }

        def accept(self, artifact_dir: str) -> dict[str, Any]:
            host_dir = self._assert_safe_relative(artifact_dir)
            self.accept_calls.append(artifact_dir)
            assert (host_dir / "backend" / "openapi.json").is_file()
            assert (host_dir / "compose.yaml").is_file()
            assert (host_dir / "asset-lock.json").is_file()
            return {
                "overall": "PASS",
                "cases": [{"name": "factory_health", "status": "PASS"}],
            }

    strict_runner = _StrictRelativeRunner(work_root)
    runtime = _build_scripted_factory_runtime(
        factory=factory,
        spec=spec,
        project_id=project_id,
        tmp_path=tmp_path,
    )
    run_worker(
        session_factory=factory,
        handler=build_generation_handler(factory, runner=strict_runner, factory_runtime=runtime),
        worker_id="worker-strict-rel",
        iterations=1,
        poll_interval=0.01,
    )

    with session_scope(factory) as db:
        run_row = db.get(Run, run_id)
        assert run_row is not None
        assert run_row.status == "SUCCEEDED"

    assert len(strict_runner.verify_calls) == 2
    be_dir, be_checks = strict_runner.verify_calls[0]
    fs_dir, fs_checks = strict_runner.verify_calls[1]
    assert be_dir == f"run-{run_id}/staging/backend_assembled"
    assert be_checks == ["backend_static_manifest", "backend_test", "backend_build"]
    assert fs_dir == f"run-{run_id}"
    assert fs_checks is None
    assert strict_runner.accept_calls == [f"run-{run_id}/staging/backend_assembled", f"run-{run_id}"]


def test_run_final_acceptance_verify_supports_both_checks_and_legacy_mock() -> None:
    """确保 run_final_acceptance_verify 同时兼容新版 checks 参数与旧 1 参 mock 客户端。"""

    class _LegacyRunner:
        def verify(self, artifact_dir: str) -> dict[str, Any]:
            return {"overall": "PASS", "dir": artifact_dir}

        def accept(self, artifact_dir: str) -> dict[str, Any]:
            return {"overall": "PASS"}

    class _ModernRunner:
        def verify(self, artifact_dir: str, *, checks: list[str] | None = None) -> dict[str, Any]:
            return {"overall": "PASS", "dir": artifact_dir, "checks": checks}

        def accept(self, artifact_dir: str) -> dict[str, Any]:
            return {"overall": "PASS"}

    assert run_final_acceptance_verify(_LegacyRunner(), "run-1", checks=["backend_test"]) == {
        "overall": "PASS",
        "dir": "run-1",
    }
    assert run_final_acceptance_verify(_ModernRunner(), "run-1", checks=["backend_test"]) == {
        "overall": "PASS",
        "dir": "run-1",
        "checks": ["backend_test"],
    }
