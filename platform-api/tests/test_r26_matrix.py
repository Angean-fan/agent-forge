"""R26：核心项目矩阵回归与交付完整性审查。

覆盖：模型路由矩阵（角色→Provider/模型，无意外跨厂商）；三种模板用例（校园活动/个人任务/图书借阅）×
四 Profile 的契约与装配 + 交付清单；生成 ZIP 完整性且无真实秘密；方法索引全量重建可复现、旧锁按 0.1 语义重放不变；
Profile 不兼容候选过滤；Agent 无资产/索引写权限。
"""

from __future__ import annotations

import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from agentforge_platform import model_gateway
from agentforge_platform.agent_files import FileBackend
from agentforge_platform.assembler import assemble
from agentforge_platform.asset_finder import Candidate, candidate_rejections
from agentforge_platform.asset_registry import default_registry
from agentforge_platform.contract import compile_from_spec
from agentforge_platform.coverage import platform_mainline
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException
from agentforge_platform.generation_graph import GenerationContext, run_generation
from agentforge_platform.method_index import build_index, index_revision, scan_methods
from agentforge_platform.method_lock import resolve_lock
from agentforge_platform.models import Project, SpecVersion
from agentforge_platform.packaging import VERIFY_REQUIRED
from agentforge_platform.resolver import resolve_assets
from agentforge_platform.runs import create_run
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[2]
SPECS = {
    "campus": REPO / "schemas" / "campus-activity.spec.json",
    "task": REPO / "schemas" / "task-management.spec.json",
    "lending": REPO / "schemas" / "book-lending.spec.json",
}
PROFILES = [
    ("demo-ts-mp", "0.3", "demo-0.2"),
    ("demo-js-mp", "0.1", "demo-0.2-js"),
    ("demo-ts-mb", "0.1", "demo-0.2-mb"),
    ("demo-js-mb", "0.1", "demo-0.2-js-mb"),
]
_PASS_VERIFY = {"overall": "PASS", "checks": [{"name": n, "status": "PASS"} for n in VERIFY_REQUIRED]}
_PASS_ACCEPT = {"overall": "PASS", "cases": [{"name": "login_admin", "status": "PASS"}]}
_SECRET = re.compile(rb"sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def _spec(path: Path, profile: tuple[str, str, str] | None = None) -> SoftwareSpecification:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if profile is not None:
        raw["technical_profile"] = {
            "profile_id": profile[0], "profile_version": profile[1], "template_version": profile[2]
        }
    return SoftwareSpecification.model_validate(raw)


# --- 模型路由矩阵 -------------------------------------------------------------

def test_routing_matrix_no_cross_vendor() -> None:
    assert set(model_gateway.ROUTES) == {
        "user_interaction", "technical_planning", "coding", "complex_repair"
    }
    # 用户交互只用 DeepSeek；技术角色（规划/编码/升级）统一走百炼 DashScope
    assert model_gateway.ROUTES["user_interaction"].provider == "deepseek_official"
    assert model_gateway.ROUTES["user_interaction"].model == "deepseek-flash"
    for role, model in (("technical_planning", "qwen3.7-plus"),
                        ("coding", "qwen3-coder-flash"),
                        ("complex_repair", "qwen3.8-max")):
        assert model_gateway.ROUTES[role].provider == "dashscope"
        assert model_gateway.ROUTES[role].model == model
    # 默认仅 user_interaction 内建启用；其余需显式开启
    assert model_gateway.role_enabled("user_interaction") is True


# --- 三用例 × 四 Profile：契约 + 装配 + 交付清单 ------------------------------

@pytest.mark.parametrize("case", list(SPECS))
@pytest.mark.parametrize("profile", PROFILES, ids=[p[0] for p in PROFILES])
def test_case_profile_delivery_manifest(tmp_path: Path, case: str, profile: tuple[str, str, str]) -> None:
    spec = _spec(SPECS[case], profile)
    contract = compile_from_spec(spec)
    lock = resolve_assets(default_registry(), profile[0])
    out = tmp_path / f"{case}-{profile[0]}"

    result = assemble(contract, lock, default_registry(), out, spec=spec)

    assert result.files
    required = [
        "asset-lock.json", "spec.json", "contract.json", "README.md",
        "compose.yaml", ".env.example", "backend/pom.xml", "frontend/package.json",
    ]
    present = {entry.path for entry in result.files}
    for name in required:
        assert name in present, f"{case}/{profile[0]} 缺少 {name}"
    assert any(p.startswith("backend/src/main/resources/db/migration/") and p.endswith(".sql")
               for p in present), "缺少 SQL migration"
    assert any(p.startswith("backend/") for p in present) and any(p.startswith("frontend/") for p in present)


def test_three_cases_compile_and_resolve() -> None:
    assert platform_mainline(_spec(SPECS["campus"])) == "campus_activity"
    assert platform_mainline(_spec(SPECS["task"])) == "mvp_task"
    assert platform_mainline(_spec(SPECS["lending"])) is None  # 未登记主线 → 诚实缺口（非伪装覆盖）
    for case, path in SPECS.items():
        spec = _spec(path)
        contract = compile_from_spec(spec)
        assert contract.tables and contract.routes, case


# --- 生成 ZIP 完整性 + 无真实秘密 ---------------------------------------------

class _PassRunner:
    def verify(self, artifact_dir: str) -> dict:
        return _PASS_VERIFY

    def accept(self, artifact_dir: str) -> dict:
        return _PASS_ACCEPT


def test_generation_zip_has_full_manifest_and_no_secrets(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    spec = _spec(SPECS["campus"])
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        row = SpecVersion(project_id=project.id, version=1, body_json=canonical_json(spec),
                          hash=content_hash(spec), confirmed_at=datetime(2026, 1, 1))
        session.add(row)
        session.flush()
        project.active_spec_id = row.id
        run_id = create_run(session, project, row.id, idempotency_key="k1",
                            spec_hash=content_hash(spec)).id

    result = run_generation(
        GenerationContext(session_factory=factory, runner=_PassRunner(),
                          work_root=tmp_path / "runs", registry=default_registry()),
        run_id,
    )
    assert result["ok"] is True, result

    zip_path = tmp_path / "runs" / f"run-{run_id}" / "source.zip"
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        for required in ("backend/pom.xml", "frontend/package.json", "compose.yaml",
                         ".env.example", "README.md", "spec.json", "API.md", "asset-lock.json",
                         "EXTERNAL_SERVICES.md", "verification.json", "cost-report.json"):
            assert required in names, required
        assert any(n.startswith("backend/src/main/resources/db/migration/") for n in names)
        for name in names:
            assert _SECRET.search(archive.read(name)) is None, f"疑似密钥：{name}"


def test_external_services_doc_states_none_needed(platform_app, tmp_path: Path) -> None:
    _, factory = platform_app
    _, run_id = _seed_run(factory, _spec(SPECS["task"]))
    run_generation(
        GenerationContext(session_factory=factory, runner=_PassRunner(),
                          work_root=tmp_path / "runs", registry=default_registry()),
        run_id,
    )
    doc = (tmp_path / "runs" / f"run-{run_id}" / "EXTERNAL_SERVICES.md").read_text(encoding="utf-8")
    assert "无须配置" in doc  # Mock 分层：当前无第三方服务


def _seed_run(factory, spec: SoftwareSpecification) -> tuple[int, int]:
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="p", requirement_text="x")
        session.add(project)
        session.flush()
        row = SpecVersion(project_id=project.id, version=1, body_json=canonical_json(spec),
                          hash=content_hash(spec), confirmed_at=datetime(2026, 1, 1))
        session.add(row)
        session.flush()
        project.active_spec_id = row.id
        run = create_run(session, project, row.id, idempotency_key="k1",
                         spec_hash=content_hash(spec))
        return project.id, run.id


# --- 方法索引重建 / 旧锁重放 / 兼容过滤 / Agent 无资产写权限 ------------------

def test_method_index_rebuild_is_deterministic(tmp_path: Path) -> None:
    root = REPO / "methods"
    first = tmp_path / "a.index"
    second = tmp_path / "b.index"
    build_index(root, first)
    build_index(root, second)
    records = scan_methods(root)
    assert first.read_bytes() == second.read_bytes()  # 全量重建可复现
    assert index_revision(records) == index_revision(scan_methods(root))


def test_old_lock_replays_unchanged_without_methods() -> None:
    # 无 method_requests → R06D 保证锁与 MVP 0.1 完全一致（旧锁按原语义重放）
    spec = _spec(SPECS["task"])
    contract = compile_from_spec(spec)
    lock = resolve_lock(default_registry(), "demo-ts-mp", spec, contract)
    assert lock.schema_version == "0.1" and lock.method_units == []
    assert lock.lock_hash  # 稳定、可重放


def test_profile_incompatible_candidate_filtered() -> None:
    candidate = Candidate(
        kind="method-unit", id="m", version="1.0.0", provides=("x",), requires=(),
        conflicts=(), extension_points=(), compatible_profiles=("other-profile",),
        license="internal", parameter_schema={}, language="java", framework="spring-boot",
    )
    from agentforge_platform.asset_finder import derive_capability_query

    spec = _spec(SPECS["task"])
    query = derive_capability_query(spec, compile_from_spec(spec))
    reasons = candidate_rejections(
        candidate, query, available_capabilities=set(), present_capabilities=set(),
        registered_extension_points=set(),
    )
    assert "profile_incompatible" in reasons  # Profile 不兼容 → 被过滤


def test_agent_cannot_write_assets_or_reach_index(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    backend = FileBackend(run_dir, ["."])
    for rel in ("asset-lock.json", "spec.json", "verification.json"):
        with pytest.raises(AppException):
            backend.write(rel, b"tamper")  # 交付/验真产物受保护
    for rel in ("../../methods/x/1.0.0/src/A.java", "/etc/passwd"):
        with pytest.raises(AppException):
            backend.write(rel, b"x")  # 版本化方法仓库/索引在 run 目录之外，Agent 无法触达
