"""A03：本地只读物化缓存（AssetMaterializer）与面向子 Agent 的资产检索/读取/暂存工具测试。"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agentforge_platform.agent_asset_tools import (
    DEFAULT_MAX_READ_BYTES,
    DEFAULT_MAX_READ_LINES,
    apply_asset_to_staging,
    build_agent_asset_tools,
    read_asset_file,
    search_assets,
)
from agentforge_platform.agent_files import FileBackend
from agentforge_platform.assembler import assemble
from agentforge_platform.asset_cache import (
    AssetMaterializer,
    make_writable_tree,
    remove_readonly_tree,
)
from agentforge_platform.asset_registry import REPO_ROOT, default_registry
from agentforge_platform.asset_store_pg import PgAssetStore
from agentforge_platform.contract import compile_from_spec
from agentforge_platform.db import (
    is_postgres,
    make_engine,
    make_session_factory,
    read_local_env_var,
    session_scope,
)
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.patch_engine import (
    ASSET_READONLY_TOOLS,
    FORBIDDEN_TOOL_NAMES,
    RESTRICTED_TOOLS,
    LedgerBinding,
    build_patch_engine,
)
from agentforge_platform.resolver import AssetLock, resolve_assets
from agentforge_platform.seed_assets import run_seed_assets
from agentforge_platform.spec import SoftwareSpecification

TASK_SPEC_PATH = REPO_ROOT / "schemas" / "task-management.spec.json"
CAMPUS_SPEC_PATH = REPO_ROOT / "schemas" / "campus-activity.spec.json"
R27_LOCK_PATH = REPO_ROOT / "schemas" / "asset-lock.r27.sample.json"


class _ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    model_name: str = "qwen3-coder-flash"

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _get_ls_params(self, **_kwargs):
        return {"ls_provider": "openai", "ls_model_name": self.model_name}

    def bind_tools(self, _tools, **_kwargs):
        return self

    def _generate(self, _messages, stop=None, run_manager=None, **_kwargs):
        message = self.responses.pop(0) if self.responses else AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_materialize_from_db_matches_git_assembly_manifest_hash_byte_for_byte(
    sqlite_url: str, tmp_path: Path
) -> None:
    run_seed_assets(database_url=sqlite_url)
    cache_root = tmp_path / ".cache" / "assets"

    spec = SoftwareSpecification.model_validate_json(
        CAMPUS_SPEC_PATH.read_text(encoding="utf-8")
    )
    contract = compile_from_spec(spec)
    git_registry = default_registry()
    lock = resolve_assets(git_registry, spec.technical_profile.profile_id)

    # 1. 直接从 Git 根目录装配
    git_out = tmp_path / "out-git"
    git_result = assemble(contract, lock, git_registry, git_out, spec=spec)

    # 2. 从数据库物化到本地只读缓存目录 .cache/assets/<kind>/<id>@<version>/，再交由 assembler.assemble 装配
    engine = make_engine(sqlite_url)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            materializer = AssetMaterializer(session, cache_root=cache_root)
            cached_registry = materializer.materialize_lock(lock)

            tpl_dir = materializer.release_cache_dir("template", "base-template", "demo-0.2")
            assert tpl_dir.is_dir()
            assert materializer.verify_cached_release("template", "base-template", "demo-0.2") is True

            # 验证缓存文件与目录均为只读权限（不可直接改写）
            sample_file = tpl_dir / "backend" / "pom.xml"
            assert sample_file.is_file()
            mode = stat.S_IMODE(sample_file.stat().st_mode)
            assert (mode & stat.S_IWUSR) == 0

            cache_out = tmp_path / "out-cache"
            cache_result = assemble(contract, lock, cached_registry, cache_out, spec=spec)

            # 3. 逐字节比对 manifest_hash 与全量文件列表
            assert cache_result.manifest_hash == git_result.manifest_hash
            assert cache_result.lock_hash == git_result.lock_hash
            assert [(f.path, f.sha256, f.size) for f in cache_result.files] == [
                (f.path, f.sha256, f.size) for f in git_result.files
            ]

            # 4. 模拟缓存目录被外部破坏 -> verify_cached_release 返回 False -> 再次 materialize 自动修复
            make_writable_tree(tpl_dir)
            sample_file.write_text("corrupted!", encoding="utf-8")
            assert (
                materializer.verify_cached_release("template", "base-template", "demo-0.2")
                is False
            )
            repaired_dir = materializer.materialize_release("template", "base-template", "demo-0.2")
            assert repaired_dir == tpl_dir
            assert (
                materializer.verify_cached_release("template", "base-template", "demo-0.2")
                is True
            )
    finally:
        remove_readonly_tree(cache_root)
        engine.dispose()


def test_search_and_read_asset_file_with_bounds_and_negative_cases(
    sqlite_url: str,
) -> None:
    run_seed_assets(database_url=sqlite_url)
    engine = make_engine(sqlite_url)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            store = PgAssetStore(session)

            # 1. search_assets 召回方法单元及符号摘要
            hits = search_assets("DateNormalizer", kind="method-unit", store=store)
            assert len(hits) == 1
            assert hits[0]["asset_ref"] == "method-unit/date-normalizer@1.0.0"
            assert any("DateNormalizer" in f["symbol_summary"] for f in hits[0]["matched_files"])

            # 2. read_asset_file 按行号精准读取片段
            rel_path = hits[0]["matched_files"][0]["rel_path"]
            snippet = read_asset_file(
                "method-unit/date-normalizer@1.0.0",
                rel_path,
                start_line=1,
                end_line=5,
                store=store,
            )
            assert snippet["start_line"] == 1
            assert snippet["end_line"] <= 5
            assert snippet["total_lines"] >= snippet["end_line"]
            assert "DateNormalizer" in snippet["content"] or "package" in snippet["content"]

            # 3. 验证单次行数/字节数上限截断保护（≤ 300 行 / 16 KB）
            assert DEFAULT_MAX_READ_LINES == 300
            assert DEFAULT_MAX_READ_BYTES == 16 * 1024
            clipped_lines = read_asset_file(
                "method-unit/date-normalizer@1.0.0",
                rel_path,
                start_line=1,
                end_line=999,
                store=store,
                max_lines=2,
            )
            assert clipped_lines["end_line"] == 2
            assert clipped_lines["truncated"] is True

            clipped_bytes = read_asset_file(
                "method-unit/date-normalizer@1.0.0",
                rel_path,
                start_line=1,
                end_line=999,
                store=store,
                max_bytes=20,
            )
            assert clipped_bytes["truncated"] is True
            assert len(clipped_bytes["content"].encode("utf-8")) <= 20

            # 4. 负例拦截：越界路径、未认证资产、非法行号
            for bad_path in ("../manifest.json", "/etc/passwd", "src\\Evil.java"):
                with pytest.raises(AppException) as exc_info:
                    read_asset_file("method-unit/date-normalizer@1.0.0", bad_path, store=store)
                assert exc_info.value.code == ErrorCode.BAD_REQUEST

            with pytest.raises(AppException) as exc_info:
                read_asset_file(
                    "method-unit/uncertified-ghost@1.0.0",
                    "src/Ghost.java",
                    store=store,
                )
            assert exc_info.value.code == ErrorCode.ASSET_NOT_FOUND

            with pytest.raises(AppException) as exc_info:
                read_asset_file(
                    "method-unit/date-normalizer@1.0.0",
                    rel_path,
                    start_line=5,
                    end_line=2,
                    store=store,
                )
            assert exc_info.value.code == ErrorCode.BAD_REQUEST
    finally:
        engine.dispose()


def test_apply_asset_to_staging_enforces_allowed_and_protected_paths(
    sqlite_url: str, tmp_path: Path
) -> None:
    run_seed_assets(database_url=sqlite_url)
    run_dir = tmp_path / "run-a03"
    allowed_util_dir = "backend/src/main/java/com/agentforge/template/util"
    fb = FileBackend(run_dir, allowed_paths=[allowed_util_dir])

    engine = make_engine(sqlite_url)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            store = PgAssetStore(session)

            # 1. 合法授权路径：成功写入 staging 暂存区，未直接污染 run_dir
            applied = apply_asset_to_staging(
                "method-unit/field-validator@1.0.0",
                allowed_util_dir,
                fb,
                store=store,
            )
            assert len(applied["staged_files"]) >= 1
            staged_rel = applied["staged_files"][0]
            assert (fb.staging / staged_rel).is_file()
            assert not (fb.run_dir / staged_rel).exists()

            # 2. 越界路径（不在 allowed_paths 内）-> 403 FORBIDDEN
            with pytest.raises(AppException) as exc_info:
                apply_asset_to_staging(
                    "method-unit/field-validator@1.0.0",
                    "backend/src/main/java/com/agentforge/template/controller",
                    fb,
                    store=store,
                )
            assert exc_info.value.code == ErrorCode.FORBIDDEN
            assert exc_info.value.status_code == 403

            # 3. 受保护路径（PROTECTED_PREFIXES，即使 allowed_paths=["."] 放开也必须被拒）-> 403 FORBIDDEN
            fb_wide = FileBackend(tmp_path / "run-wide", allowed_paths=["."])
            for protected_dir in (
                "backend/src/main/java/com/agentforge/template/security",
                "backend/src/test/java/com/agentforge/template",
            ):
                with pytest.raises(AppException) as exc_info:
                    apply_asset_to_staging(
                        "method-unit/field-validator@1.0.0",
                        protected_dir,
                        fb_wide,
                        store=store,
                    )
                assert exc_info.value.code == ErrorCode.FORBIDDEN
                assert exc_info.value.status_code == 403

            # 4. 企图向 .cache 只读目录写入 -> 403 FORBIDDEN
            with pytest.raises(AppException) as exc_info:
                apply_asset_to_staging(
                    "method-unit/field-validator@1.0.0",
                    ".cache/assets",
                    fb_wide,
                    store=store,
                )
            assert exc_info.value.code == ErrorCode.FORBIDDEN
    finally:
        engine.dispose()


def test_patch_engine_asset_tools_whitelist_opt_in_and_agent_execution(
    sqlite_url: str, tmp_path: Path
) -> None:
    run_seed_assets(database_url=sqlite_url)
    engine = make_engine(sqlite_url)
    factory = make_session_factory(engine)
    run_dir = tmp_path / "run-agent"
    allowed_dir = "backend/src/main/java/com/agentforge/template/util"
    fb = FileBackend(run_dir, allowed_paths=[allowed_dir])

    try:
        tools = build_agent_asset_tools(file_backend=fb, session_factory=factory)

        # 1. 默认 allow_asset_tools=False 时，传入额外资产工具会被 assert_restricted_tools 拒绝（保持旧行为零回归）
        with pytest.raises(AppException) as exc_info:
            build_patch_engine(
                ledger_binding=LedgerBinding(
                    session_factory=factory, project_id=1, run_id=1, task_id="t1"
                ),
                file_backend=fb,
                system_prompt="default-restricted",
                model=_ScriptedModel(responses=[]),
                extra_tools=tools,
                allow_asset_tools=False,
            )
        assert exc_info.value.code == ErrorCode.FORBIDDEN

        # 2. 工厂模式显式开启 allow_asset_tools=True 时，6 个文件工具 + 3 个只读资产工具通过审计，且绝不含 execute/task
        scripted = _ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "search_assets",
                            "args": {"query": "StatusGuard", "kind": "method-unit"},
                            "id": "call_s1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "apply_asset_to_staging",
                            "args": {
                                "asset_ref": "method-unit/status-guard@1.0.0",
                                "target_subdir": allowed_dir,
                            },
                            "id": "call_a1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
        pe = build_patch_engine(
            ledger_binding=LedgerBinding(
                session_factory=factory, project_id=1, run_id=1, task_id="t1"
            ),
            file_backend=fb,
            system_prompt="factory-coder",
            model=scripted,
            extra_tools=tools,
            allow_asset_tools=True,
        )
        assert pe.tool_names == frozenset((*RESTRICTED_TOOLS, *ASSET_READONLY_TOOLS))
        assert not (pe.tool_names & FORBIDDEN_TOOL_NAMES)

        res = pe.run("reuse status-guard from PG asset store")
        changes = res["change_set"]["changes"]
        assert len(changes) >= 1
        assert any("StatusGuard.java" in c["path"] for c in changes)
    finally:
        engine.dispose()


def test_live_postgres_materialize_assemble_and_agent_tools(tmp_path: Path) -> None:
    pg_url = read_local_env_var("AGENTFORGE_DATABASE_URL") or ""
    if not pg_url or not is_postgres(pg_url):
        pytest.skip("NOT_RUN: 未配置 AGENTFORGE_DATABASE_URL 真实 PostgreSQL 环境")

    spec = SoftwareSpecification.model_validate_json(
        TASK_SPEC_PATH.read_text(encoding="utf-8")
    )
    contract = compile_from_spec(spec)
    lock = AssetLock.model_validate_json(R27_LOCK_PATH.read_text(encoding="utf-8"))

    git_out = tmp_path / "pg-vs-git-git"
    git_res = assemble(contract, lock, default_registry(), git_out, spec=spec)

    cache_root = tmp_path / "pg-cache"
    engine = make_engine(pg_url)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            materializer = AssetMaterializer(session, cache_root=cache_root)
            cached_registry = materializer.materialize_lock(lock)
            tpl_cache_dir = materializer.release_cache_dir(
                "template", "base-template", "demo-0.2"
            )
            assert tpl_cache_dir.is_dir()
            methods_cache_dir = materializer.materialize_release(
                "method-unit", "field-validator", "1.0.0"
            )
            assert methods_cache_dir.is_dir()

            # 直接对比标准锁（从真实 PG 物化到本地只读缓存 vs 从 Git 根目录直接装配，100% 逐字节一致）
            std_lock = resolve_assets(default_registry(), spec.technical_profile.profile_id)
            std_git_res = assemble(
                contract, std_lock, default_registry(), tmp_path / "std-git", spec=spec
            )
            std_pg_res = assemble(
                contract, std_lock, cached_registry, tmp_path / "std-pg", spec=spec
            )
            assert std_pg_res.manifest_hash == std_git_res.manifest_hash
            assert len(git_res.files) > 0

            # 同时在真实 PG 上验证 search_assets -> read_asset_file
            hits = search_assets("TaskController", kind="capability-pack", db=session)
            assert len(hits) >= 1
            first_hit = hits[0]
            first_rel = first_hit["matched_files"][0]["rel_path"]
            file_slice = read_asset_file(
                first_hit["asset_ref"],
                first_rel,
                start_line=1,
                end_line=20,
                db=session,
            )
            assert file_slice["start_line"] == 1
            assert file_slice["end_line"] <= 20
            assert len(file_slice["content"]) > 0
    finally:
        remove_readonly_tree(cache_root)
        engine.dispose()
