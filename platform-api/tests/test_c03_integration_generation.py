"""C03：Integration Pack 接入生成链路（S1/E03/E04）。

覆盖（**确定性、无模型、无网络**）：

- **零漂移**：开关关闭时标准 Task 仍为 0.1 锁、无集成包、无外部指南、就绪不落库；
- **接入闭环**：开关开启且规格请求集成包时 → 0.3 集成锁 → 装配 COPY Port/Adapter/Mock →
  ZIP 内含 `EXTERNAL_SERVICES.md`/`.env.example`（真实配置键）→ `external_readiness` 落库（待配置）；
- **Mock 就绪**：配置环境变量齐全 → `MOCK_ONLY`（Mock 通过≠Live）；
- **负例**：全程无外部网络；ZIP 秘密扫描（`sk-`/PEM/AKIA）命中即拒；未配置不伪称 Live。

真实 Runner 执行集成包 Java 契约测试不在本步（无真实 Runner）→ NOT_RUN。
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from agentforge_platform import packaging
from agentforge_platform.asset_registry import default_registry
from agentforge_platform.db import session_scope
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.integration_generation import (
    FLAG_ENV,
    build_integration_lock,
    integration_generation_enabled,
    requested_packs,
)
from agentforge_platform.models import ModelCall, Project, Run, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "schemas" / "task-management.spec.json"
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}
PACK = "weather-lookup@0.3.0"
PORT_DIR = "backend/src/main/java/com/agentforge/integration/"


class _PassRunner:
    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


def _spec(*, integrations=None) -> SoftwareSpecification:
    spec = SoftwareSpecification.model_validate_json(TASK.read_text(encoding="utf-8"))
    if integrations is not None:
        spec = spec.model_copy(update={"integrations": integrations})
    return spec


def _seed(factory, spec: SoftwareSpecification) -> int:
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        row = SpecVersion(project_id=project.id, version=1, body_json=canonical_json(spec),
                          hash=content_hash(spec), confirmed_at=datetime(2026, 1, 1))
        session.add(row)
        session.flush()
        project.active_spec_id = row.id
        return create_run(session, project, row.id, idempotency_key="c03",
                          spec_hash=content_hash(spec)).id


def _context(factory, tmp_path: Path) -> GenerationContext:
    return GenerationContext(
        session_factory=factory, runner=_PassRunner(), work_root=tmp_path / "runs",
        registry=default_registry(),
    )


def _zip_names(run_dir: Path) -> set[str]:
    with zipfile.ZipFile(run_dir / "source.zip") as archive:
        return set(archive.namelist())


def _zip_text(run_dir: Path, name: str) -> str:
    with zipfile.ZipFile(run_dir / "source.zip") as archive:
        return archive.read(name).decode("utf-8")


# --- 零漂移（开关关闭） -------------------------------------------------------

def test_flag_off_standard_task_zero_drift(platform_app, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(FLAG_ENV, raising=False)
    assert integration_generation_enabled() is False
    _, factory = platform_app
    run_id = _seed(factory, _spec())
    result = run_generation(_context(factory, tmp_path), run_id)

    assert result["ok"] is True, result
    run_dir = tmp_path / "runs" / f"run-{run_id}"
    lock = json.loads((run_dir / "asset-lock.json").read_text("utf-8"))
    assert lock["schema_version"] == "0.1" and lock["integration_packs"] == []
    names = _zip_names(run_dir)
    assert not any(n.startswith(PORT_DIR) for n in names)  # 未 COPY 集成包
    with session_scope(factory) as session:
        assert session.get(Run, run_id).external_readiness is None  # 普通 Task 不落库


# --- 接入闭环（开关开启 + 显式请求） ------------------------------------------

def test_flag_on_lock_has_integration_pack(monkeypatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "1")
    spec = _spec(integrations=[PACK])
    assert requested_packs(spec) == [PACK]
    lock = build_integration_lock(spec)
    assert lock is not None and lock.schema_version == "0.3"
    assert [(u.id, u.version) for u in lock.integration_packs] == [("weather-lookup", "0.3.0")]


def test_flag_on_assembles_pack_and_delivers_guide(
    platform_app, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(FLAG_ENV, "1")
    for key in ("WEATHER_APP_ID", "WEATHER_API_BASE_URL", "WEATHER_API_KEY"):
        monkeypatch.delenv(key, raising=False)  # 未配置 → 待配置
    _, factory = platform_app
    run_id = _seed(factory, _spec(integrations=[PACK]))
    result = run_generation(_context(factory, tmp_path), run_id)

    assert result["ok"] is True, result
    run_dir = tmp_path / "runs" / f"run-{run_id}"
    names = _zip_names(run_dir)
    # 生成的 Service 依赖内部 Port；Mock 已装配进工程
    assert PORT_DIR + "port/WeatherPort.java" in names
    assert PORT_DIR + "mock/MockWeatherAdapter.java" in names
    assert PORT_DIR + "adapter/HttpWeatherAdapter.java" in names
    # 指南与 .env.example 进入 ZIP，且指向实际配置键
    assert "EXTERNAL_SERVICES.md" in names and ".env.example" in names
    env = _zip_text(run_dir, ".env.example")
    for name in ("WEATHER_APP_ID", "WEATHER_API_BASE_URL", "WEATHER_API_KEY"):
        assert name in env
    assert "DB_NAME" in env  # 模板原有块保留
    assert _zip_text(run_dir, "EXTERNAL_SERVICES.md").strip()  # 非占位空文本
    # 就绪落库：缺必需 Live 配置 → 待配置（不是 MOCK_ONLY，也不伪称 Live）
    with session_scope(factory) as session:
        assert session.get(Run, run_id).external_readiness == "AWAITING_USER_CONFIG"
    # 零模型调用
    with session_scope(factory) as session:
        assert session.query(ModelCall).count() == 0


def test_configured_pack_reports_mock_only(platform_app, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "1")
    monkeypatch.setenv("WEATHER_APP_ID", "app")
    monkeypatch.setenv("WEATHER_API_BASE_URL", "https://api.example.invalid")
    monkeypatch.setenv("WEATHER_API_KEY", "redacted")
    _, factory = platform_app
    run_id = _seed(factory, _spec(integrations=[PACK]))
    result = run_generation(_context(factory, tmp_path), run_id)

    assert result["ok"] is True, result
    with session_scope(factory) as session:
        # 配置齐全且未跑 Live → MOCK_ONLY（Mock 通过 ≠ Live 验证）
        assert session.get(Run, run_id).external_readiness == "MOCK_ONLY"


# --- 负例 ---------------------------------------------------------------------

def test_no_external_network(platform_app, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "1")
    import urllib.request

    def _boom(*_args, **_kwargs):  # 任何外部网络调用都应使测试失败
        raise AssertionError("C03 路径不得发起外部网络请求")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    _, factory = platform_app
    run_id = _seed(factory, _spec(integrations=[PACK]))
    assert run_generation(_context(factory, tmp_path), run_id)["ok"] is True


def test_zip_secret_scan_blocks_leaked_key(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "leak.txt").write_text("key=sk-" + "A" * 24 + "\n", encoding="utf-8")
    with pytest.raises(packaging.PackagingError) as exc:
        packaging.collect_files(run_dir, packaging.package_limits())
    assert exc.value.code == "SECRET_DETECTED"
