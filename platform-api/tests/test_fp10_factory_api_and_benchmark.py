"""FP10：GET /api/runs/{run_id}/factory 只读泳道接口、脱敏审计与电商沙箱标杆冷启动验收测试。"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentforge_platform.asset_cache import make_writable_tree, remove_readonly_tree
from agentforge_platform.db import session_scope
from agentforge_platform.factory_pipeline import FACTORY_PIPELINE_ENV
from agentforge_platform.factory_state import FactoryPhase, FactoryStateStore
from agentforge_platform.worker import build_generation_handler, run_worker

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

# 复用 FP09 完整 5 部门电商沙箱脚本化夹具（绝不复制脆弱副本）
from test_fp09_fullstack_factory_delivery import (
    ALICE_CREDS,
    FIVE_BE_DEPTS,
    FIVE_FE_DEPTS,
    _build_scripted_factory_runtime,
    _configure_models,
    _make_ecommerce_spec,
    _MockDockerRunnerClient,
    _seed_confirmed_ecommerce_project,
)

BOB_CREDS = {"username": "bob", "password": "bob-secret"}


def test_factory_endpoint_ownership_sanitization_and_full_ecommerce_benchmark_cold_start(
    platform_app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """覆盖 FP10 后端全部 WhenDone：

    1. 归属权校验：Alice 可查自己运行的 ``GET /api/runs/{run_id}/factory``，Bob 越权访问被拒绝（403/404）；
    2. 完整返回：当前阶段、冻结蓝图摘要、5 后端 + 5 前端部门泳道状态/轮次/耗时/测试结果/成本明细、
       未解决与已解决缺陷单摘要、自定义模型路由脱敏快照；
    3. 脱敏审计：环境变量注入真实格式密钥、缺陷单注入疑似密钥与 PG 密码 → HTTP 响应体 0 泄漏；
    4. 电商 5 部门沙箱 ZIP 解压后核验全部交付文档，并在本机有 Node/npm 时对解压后的 ``frontend/``
       真实执行 ``npm ci --offline && npm run type-check && npm test`` 冷启动验证。
    """
    app, factory = platform_app
    _configure_models(tmp_path, monkeypatch)
    secret_env_value = "sk-real-secret-do-not-ever-leak-999999"
    monkeypatch.setenv("FAKE_FP09_KEY", secret_env_value)
    monkeypatch.setenv(FACTORY_PIPELINE_ENV, "1")
    work_root = tmp_path / "work-fp10"
    monkeypatch.setenv("RUNNER_WORK_ROOT", str(work_root))

    spec = _make_ecommerce_spec()
    project_id = _seed_confirmed_ecommerce_project(factory, spec)

    alice = TestClient(app)
    assert alice.post("/api/auth/login", json=ALICE_CREDS).status_code == 200
    bob = TestClient(app)
    assert bob.post("/api/auth/login", json=BOB_CREDS).status_code == 200

    # 1. 提交工厂模式运行（显式传 mode="factory"）
    create_resp = alice.post(
        f"/api/projects/{project_id}/runs",
        json={"mode": "factory"},
        headers={
            "X-CSRF-Token": alice.cookies.get("af_csrf"),
            "Idempotency-Key": "fp10-ecommerce-benchmark",
        },
    )
    assert create_resp.status_code == 202, create_resp.text
    run_id = create_resp.json()["data"]["id"]

    # 2. 跑通完整 5 部门后端 + 5 部门前端 + 全栈总装 + Docker Runner 验收 + ZIP 打包
    docker_runner = _MockDockerRunnerClient(work_root=work_root)
    runtime = _build_scripted_factory_runtime(
        factory=factory,
        spec=spec,
        project_id=project_id,
        tmp_path=tmp_path,
    )
    run_worker(
        session_factory=factory,
        handler=build_generation_handler(factory, runner=docker_runner, factory_runtime=runtime),
        worker_id="worker-fp10",
        iterations=1,
        poll_interval=0.01,
    )

    # 故意向数据库写入一条含敏感凭据的已解决缺陷单 + 一条未解决缺陷单，审计脱敏拦截
    store = FactoryStateStore(factory)
    leaked_pg_pw = "pg-super-secret-pw-8888"
    leaked_api_key = "sk-leak-in-ticket-abcdef1234567890"
    t_resolved = store.open_defect_ticket(
        run_id,
        FactoryPhase.FRONTEND,
        "catalog-ui",
        scope="department",
        round_num=1,
        error_class="TS_TYPE_ERROR",
        payload={
            "summary": f"ProductDto 缺少字段（调试串 postgresql://admin:{leaked_pg_pw}@127.0.0.1:5432/af）",
            "log_excerpt": f"TS2339: api_key={leaked_api_key}",
            "affected_paths": ["frontend/src/modules/catalog-ui/service.ts"],
        },
    )
    store.resolve_defect_ticket(t_resolved.id)
    store.open_defect_ticket(
        run_id,
        FactoryPhase.BACKEND,
        "payment",
        scope="integration",
        round_num=1,
        error_class="SANDBOX_NOTICE",
        payload={
            "summary": "沙箱支付已隔离为本地 Mock 实现",
            "log_excerpt": "sandbox_only=true",
            "affected_paths": ["backend/src/main/java/com/agentforge/template/modules/payment/"],
        },
    )

    try:
        # 3. 归属权校验：Bob 访问 Alice 的 /factory 必须被拒（403/404）
        bob_resp = bob.get(f"/api/runs/{run_id}/factory")
        assert bob_resp.status_code in (403, 404)

        # 未登录访问必须 401
        anon = TestClient(app)
        assert anon.get(f"/api/runs/{run_id}/factory").status_code == 401

        # 4. Alice 访问 GET /api/runs/{run_id}/factory
        ok_resp = alice.get(f"/api/runs/{run_id}/factory")
        assert ok_resp.status_code == 200, ok_resp.text
        raw_body = ok_resp.text

        # 脱敏审计：响应体绝不能出现环境变量真实密钥、缺陷单内密钥或 PG 密码
        assert secret_env_value not in raw_body
        assert leaked_pg_pw not in raw_body
        assert leaked_api_key not in raw_body

        data = ok_resp.json()["data"]
        assert data["run_id"] == run_id
        assert data["project_id"] == project_id
        assert data["run_status"] == "SUCCEEDED"
        assert data["factory_active"] is True
        assert data["current_phase"] == "frontend"
        assert data["integration_rounds"]["backend"] >= 1
        assert data["integration_rounds"]["frontend"] >= 1

        # 冻结蓝图摘要（后端 + 前端）
        bp_phases = {bp["phase"]: bp for bp in data["blueprints"]}
        assert set(bp_phases) == {"backend", "frontend"}
        assert bp_phases["backend"]["module_count"] == 5
        assert bp_phases["frontend"]["module_count"] == 5
        assert bp_phases["backend"]["status"] == "APPROVED_FOR_BUILD"
        assert bp_phases["frontend"]["status"] == "APPROVED_FOR_BUILD"

        # 5 后端 + 5 前端泳道卡片数据完整
        assert [d["dept_id"] for d in data["backend_departments"]] == sorted(FIVE_BE_DEPTS)
        assert [d["dept_id"] for d in data["frontend_departments"]] == sorted(FIVE_FE_DEPTS)
        for lane in data["backend_departments"] + data["frontend_departments"]:
            assert lane["status"] == "PASSED"
            assert lane["current_round"] >= 1
            assert lane["test_status"] == "PASS"
            assert lane["test_executed"] is True
            assert lane["test_exit_code"] == 0
            assert lane["calls"] >= 1

        # 缺陷单分类正确且已脱敏
        assert len(data["open_defects"]) == 1
        assert data["open_defects"][0]["dept_id"] == "payment"
        assert len(data["resolved_defects"]) == 1
        assert data["resolved_defects"][0]["dept_id"] == "catalog-ui"
        assert "***" in data["resolved_defects"][0]["summary"]
        assert "***" in data["resolved_defects"][0]["log_excerpt"]

        # 自定义模型路由脱敏快照：仅暴露域名与环境变量名，不暴露真实 Key
        routing = {r["role"]: r for r in data["model_routing_snapshot"]}
        assert {"chief_architect", "dept_lead", "dept_coder", "dept_tester"} <= set(routing)
        for role_view in routing.values():
            assert role_view["base_url_host"] == "api.fake-llm.example"
            assert role_view["api_key_env"] == "FAKE_FP09_KEY"
            assert role_view["api_key_present"] is True

        # 5. 下载电商沙箱 ZIP 并解压冷启动核验
        detail = alice.get(f"/api/runs/{run_id}").json()["data"]
        zip_art = next(a for a in detail["artifacts"] if a["kind"] == "zip")
        dl = alice.get(f"/api/runs/{run_id}/artifacts/{zip_art['id']}")
        assert dl.status_code == 200

        extract_dir = tmp_path / "unzipped-ecommerce"
        with zipfile.ZipFile(io.BytesIO(dl.content), "r") as zf:
            zf.extractall(extract_dir)
        make_writable_tree(extract_dir)

        assert (extract_dir / "compose.yaml").is_file()
        assert (extract_dir / "README.md").is_file()
        assert (extract_dir / "backend" / "openapi.json").is_file()
        assert (extract_dir / "factory-blueprint.json").is_file()
        assert (extract_dir / "cost-report.json").is_file()
        assert (extract_dir / "department-cost-report.json").is_file()
        assert (extract_dir / "verification.json").is_file()
        assert (extract_dir / "EXTERNAL_SERVICES.md").is_file()

        # 若本机已安装 Node/npm，对解压出的前端目录真实运行 npm ci --offline + type-check + test
        npm = shutil.which("npm")
        if npm:
            fe_dir = extract_dir / "frontend"
            inst = subprocess.run(
                [npm, "ci", "--offline", "--no-audit", "--no-fund"],
                cwd=fe_dir,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if inst.returncode == 0:
                tc = subprocess.run(
                    [npm, "run", "type-check"],
                    cwd=fe_dir,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                assert tc.returncode == 0, f"解压前端 vue-tsc 失败：\n{tc.stdout}\n{tc.stderr}"
                ut = subprocess.run(
                    [npm, "test"],
                    cwd=fe_dir,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                assert ut.returncode == 0, f"解压前端 vitest 失败：\n{ut.stdout}\n{ut.stderr}"
    finally:
        for p in (tmp_path / "asset-cache", work_root, tmp_path / "unzipped-ecommerce"):
            if p.exists():
                remove_readonly_tree(p)


def test_factory_endpoint_returns_inactive_for_standard_run(platform_app) -> None:
    """标准装配运行调用 GET /api/runs/{run_id}/factory 时返回 factory_active=false，不影响简洁界面。"""
    from agentforge_platform.models import Project

    app, factory = platform_app
    with session_scope(factory) as db:
        p = Project(owner_id=1, name="std-proj", requirement_text="demo", active_spec_id=1)
        db.add(p)
        db.flush()
        pid = p.id

    alice = TestClient(app)
    assert alice.post("/api/auth/login", json=ALICE_CREDS).status_code == 200
    run_id = alice.post(
        f"/api/projects/{pid}/runs",
        json={},
        headers={
            "X-CSRF-Token": alice.cookies.get("af_csrf"),
            "Idempotency-Key": "fp10-standard-run",
        },
    ).json()["data"]["id"]

    resp = alice.get(f"/api/runs/{run_id}/factory")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["factory_active"] is False
    assert data["current_phase"] == "idle"
    assert data["backend_departments"] == []
    assert data["frontend_departments"] == []
