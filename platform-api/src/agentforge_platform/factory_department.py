"""FP04：部门三角色最小职责引擎（``DeptLead`` + ``DeptCoder`` + ``DeptTester``）与 ``DefectTicket``。

核心设计（严守最小职责与读写隔离）：
1. ``DeptLead``（``dept_lead`` 角色）：只读、**零文件写入权限**，首轮产出 :class:`DepartmentPlan`，
   返工轮接收脱敏且 ≤ 4 KB 的 :class:`DefectTicket` 并产出局部 :class:`FixDirective`；
2. ``DeptCoder``（``dept_coder`` 角色）：运行于受限 ``PatchEngine`` + :class:`CoderFileBackend`，
   仅可写本部门 ``staging/<phase>/<dept_id>/`` 下的 ``owned_paths`` 与 ``api-fragment.json``；
   可自由使用只读 PG 资产工具（``search_assets`` / ``read_asset_file`` / ``apply_asset_to_staging``），
   **禁止写 ``test_paths``、只读底座、``api/*Facade.java``、他部门目录，且严禁向 PG ``asset_release`` 写入**；
3. ``DeptTester``（``dept_tester`` 角色）：运行于受限 ``PatchEngine`` + :class:`TesterFileBackend`，
   仅可写本部门 ``test_paths``，**禁止修改业务源码与底座基座测试**；完成后必须驱动真实
   :class:`ModuleVerifier` 执行编译与单测，测试是否 ``PASSED`` 100% 由工具退出码与结果决定，
   绝不凭模型口头审查放行；
4. 启动 ``DeptCoder`` 与 ``DeptTester`` 前均强制调用 ``audit_tool_table()`` 与 ``assert_restricted_tools()``，
   确保无 ``execute``（shell）与 ``task``（无控子代理）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform.agent_asset_tools import build_agent_asset_tools
from agentforge_platform.agent_files import (
    PROTECTED_FILES,
    PROTECTED_PREFIXES,
    FileBackend,
    FileChangeSet,
    _is_protected,
    _rel_parts,
    _within_allowed,
)
from agentforge_platform.asset_cache import make_writable_tree
from agentforge_platform.blueprint_contracts import ArchitectureBlueprint
from agentforge_platform.budget_pg import PgBudgetLedger
from agentforge_platform.clients import ModelClient, ProviderPreflightError
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.factory_base_builder import (
    READONLY_MANIFEST_NAME,
    BaseManifest,
    assert_base_integrity,
    load_base_manifest,
)
from agentforge_platform.factory_state import (
    DeptStatus,
    FactoryCancelledError,
    FactoryPhase,
    FactoryStateStore,
)
from agentforge_platform.factory_tickets import (
    MAX_DEFECT_LOG_BYTES,
    DefectTicket,
    DepartmentDeliverable,
    DepartmentPlan,
    FixDirective,
    TestEvidence,
    sanitize_defect_log,
)
from agentforge_platform.model_gateway import Route, lookup_price, resolve_route, role_enabled
from agentforge_platform.models import Run
from agentforge_platform.module_registry import Module
from agentforge_platform.module_verifier import ModuleVerificationResult, ModuleVerifier
from agentforge_platform.patch_engine import (
    FORBIDDEN_TOOL_NAMES,
    LedgerBinding,
    PatchEngine,
    assert_restricted_tools,
    audit_tool_table,
    build_patch_engine,
)
from agentforge_platform.production_routing import build_client_for_role, build_ledger_model
from agentforge_platform.spec import SoftwareSpecification

DEPT_LEAD_ROLE = "dept_lead"
DEPT_CODER_ROLE = "dept_coder"
DEPT_TESTER_ROLE = "dept_tester"
API_FRAGMENT_FILENAME = "api-fragment.json"
DEFAULT_DEPT_MAX_ROUNDS = 3

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL | re.IGNORECASE)

CODER_SYSTEM_PROMPT = (
    "你是 AgentForge 异步工厂的【部门编码子 Agent（DeptCoder）】。"
    "你只允许在当前部门暂存区的 owned_paths 内编写业务代码与 api-fragment.json；"
    "你可以自主调用 search_assets / read_asset_file / apply_asset_to_staging 查 PG 资产库复用或直接编写。"
    "严禁修改 test_paths、只读底座、跨模块 api/*Facade.java 或他部门目录；严禁执行 shell 命令。"
)

TESTER_SYSTEM_PROMPT = (
    "你是 AgentForge 异步工厂的【部门测试子 Agent（DeptTester）】。"
    "你只允许在本部门 test_paths 内编写单元测试与跨模块 Facade 的 Mockito/Mock 契约测试。"
    "严禁修改任何业务源码、只读底座、api/*Facade.java 或他部门测试文件；严禁执行 shell 命令。"
    "测试是否通过将由系统程序调用真实编译与单测工具判定。"
)

CoderModelFactory = Callable[[LedgerBinding, int], BaseChatModel]
TesterModelFactory = Callable[[LedgerBinding, int], BaseChatModel]


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if not text:
        raise ValueError("DeptLead 返回为空，无法解析结构化 JSON")
    fenced = _CODE_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    elif not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise TypeError("DeptLead 响应顶层必须为 JSON 对象")
    return parsed


def _is_module_api_java(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    if "modules" in parts and "api" in parts and relative.endswith(".java"):
        mod_idx = parts.index("modules")
        return mod_idx + 2 < len(parts) and parts[mod_idx + 2] == "api"
    return False


# --------------------------------------------------------------------------- #
# 严格角色隔离的文件后端：CoderFileBackend & TesterFileBackend
# --------------------------------------------------------------------------- #
class CoderFileBackend(FileBackend):
    """``DeptCoder`` 专用文件后端：仅可写本部门 ``owned_paths`` 与 ``api-fragment.json``。"""

    def __init__(
        self,
        staging_root: str | Path,
        *,
        owned_paths: list[str],
        test_paths: list[str],
        base_manifest: BaseManifest | None = None,
    ) -> None:
        allowed = list(owned_paths)
        if API_FRAGMENT_FILENAME not in allowed:
            allowed.append(API_FRAGMENT_FILENAME)
        super().__init__(staging_root, allowed)
        self._test_paths = list(test_paths)
        self._base_files: frozenset[str] = (
            frozenset(entry.path for entry in base_manifest.files)
            if base_manifest is not None
            else frozenset()
        )
        self._base_prefixes: tuple[str, ...] = (
            tuple(base_manifest.protected_prefixes) if base_manifest is not None else ()
        )

    def _require_writable(self, relative: str) -> None:
        _rel_parts(relative)
        if self._test_paths and _within_allowed(relative, self._test_paths):
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"DeptCoder 禁止修改测试路径 test_paths：{relative}",
                status_code=403,
            )
        if relative in self._base_files or any(
            relative.startswith(prefix) for prefix in self._base_prefixes
        ):
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"DeptCoder 禁止修改只读底座或跨模块 Facade：{relative}",
                status_code=403,
            )
        if _is_protected(relative):
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"DeptCoder 禁止修改受保护路径：{relative}",
                status_code=403,
            )
        if not _within_allowed(relative, self.allowed_paths):
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"DeptCoder 越权写入非本部门 owned_paths 路径：{relative}",
                status_code=403,
            )


class TesterFileBackend(FileBackend):
    """``DeptTester`` 专用文件后端：仅可写本部门 ``test_paths``，严禁修改任何业务源码或底座。"""

    __test__ = False

    def __init__(
        self,
        staging_root: str | Path,
        *,
        test_paths: list[str],
        owned_paths: list[str],
        base_manifest: BaseManifest | None = None,
    ) -> None:
        if not test_paths:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                "DeptTester 要求模块必须声明至少一条 test_paths",
                status_code=422,
            )
        super().__init__(staging_root, list(test_paths))
        self._owned_paths = list(owned_paths)
        self._base_files: frozenset[str] = (
            frozenset(entry.path for entry in base_manifest.files)
            if base_manifest is not None
            else frozenset()
        )

    def _require_writable(self, relative: str) -> None:
        _rel_parts(relative)
        if PurePosixPath(relative).name in PROTECTED_FILES or relative == API_FRAGMENT_FILENAME:
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"DeptTester 禁止修改受保护产物或接口片段：{relative}",
                status_code=403,
            )
        if _is_module_api_java(relative):
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"DeptTester 禁止修改跨模块 api/*Facade.java：{relative}",
                status_code=403,
            )
        if relative in self._base_files:
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"DeptTester 禁止修改底座或基座固定测试文件：{relative}",
                status_code=403,
            )
        if self._owned_paths and _within_allowed(relative, self._owned_paths):
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"DeptTester 禁止修改业务源码（owned_paths）：{relative}",
                status_code=403,
            )
        if relative.startswith(("backend/src/main/", "frontend/src/")):
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"DeptTester 禁止修改业务源码目录：{relative}",
                status_code=403,
            )
        for prefix in PROTECTED_PREFIXES:
            if prefix == "backend/src/test/":
                continue  # 本部门专属 test_paths 位于 backend/src/test/... 下，由下方白名单放行
            if relative.startswith(prefix):
                raise AppException(
                    ErrorCode.FORBIDDEN,
                    f"DeptTester 禁止修改受保护路径：{relative}",
                    status_code=403,
                )
        if not _within_allowed(relative, self.allowed_paths):
            raise AppException(
                ErrorCode.FORBIDDEN,
                f"DeptTester 越权写入非本部门 test_paths 路径：{relative}",
                status_code=403,
            )


def _prepare_department_staging(
    run_dir: Path,
    *,
    phase: FactoryPhase,
    dept_id: str,
    base_dir: Path | None,
) -> tuple[Path, BaseManifest | None]:
    """初始化部门独立暂存目录 ``staging/<phase>/<dept_id>/``（从只读底座复制编译视图，绝不改底座原件）。"""
    dept_staging = (run_dir / "staging" / phase.value / dept_id).resolve()
    base_manifest: BaseManifest | None = None

    if base_dir is not None:
        resolved_base = base_dir.resolve()
        manifest_file = resolved_base / READONLY_MANIFEST_NAME
        if manifest_file.is_file():
            base_manifest = load_base_manifest(manifest_file)
        if not dept_staging.exists():
            dept_staging.mkdir(parents=True, exist_ok=True)
            for child_name in ("backend", "frontend"):
                src_sub = resolved_base / child_name
                if src_sub.is_dir():
                    dst_sub = dept_staging / child_name
                    shutil.copytree(
                        src_sub,
                        dst_sub,
                        ignore=shutil.ignore_patterns("node_modules"),
                    )
                    make_writable_tree(dst_sub)
                    src_nm = src_sub / "node_modules"
                    dst_nm = dst_sub / "node_modules"
                    if child_name == "frontend" and src_nm.exists() and not dst_nm.exists():
                        os.symlink(src_nm.resolve(), dst_nm, target_is_directory=True)
    else:
        dept_staging.mkdir(parents=True, exist_ok=True)

    return dept_staging, base_manifest


def _discover_test_files(dept_staging: Path, test_paths: list[str]) -> list[str]:
    found: list[str] = []
    for raw in test_paths:
        rel = raw.strip().strip("/")
        if not rel:
            continue
        target = dept_staging / rel
        if target.is_file():
            found.append(target.relative_to(dept_staging).as_posix())
        elif target.is_dir():
            for path in sorted(target.rglob("*")):
                if path.is_file():
                    found.append(path.relative_to(dept_staging).as_posix())
    return sorted(set(found))


def _derive_test_selector(
    *,
    dept_staging: Path,
    module: Module,
    blueprint: ArchitectureBlueprint,
    stack: str,
    explicit_selector: str | None,
) -> tuple[str, list[str]]:
    test_files = _discover_test_files(dept_staging, module.test_paths)
    if explicit_selector:
        return explicit_selector, test_files

    if stack == "java":
        class_names: list[str] = []
        for rel in test_files:
            if rel.endswith(".java"):
                stem = PurePosixPath(rel).stem
                if stem.endswith(("Test", "Tests")) and stem not in class_names:
                    class_names.append(stem)
        if class_names:
            return ",".join(class_names), test_files
        return f"{blueprint.base_package}.modules.{module.module_id}.*Test", test_files

    # frontend：vitest 在 frontend/ 子目录运行，选择器需去除前导 frontend/
    if len(test_files) == 1:
        raw_sel = test_files[0]
    elif module.test_paths:
        raw_sel = module.test_paths[0].rstrip("/")
    elif test_files:
        raw_sel = test_files[0]
    else:
        raw_sel = f"test/{module.module_id}"
    raw_sel = raw_sel.removeprefix("frontend/")
    return raw_sel, test_files


def _load_api_fragment(dept_staging: Path) -> tuple[str | None, dict[str, Any]]:
    frag_path = dept_staging / API_FRAGMENT_FILENAME
    if not frag_path.is_file():
        return None, {}
    try:
        parsed = json.loads(frag_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return API_FRAGMENT_FILENAME, {}
    return API_FRAGMENT_FILENAME, parsed if isinstance(parsed, dict) else {}


def _build_lead_plan_prompt(
    *,
    module: Module,
    blueprint: ArchitectureBlueprint,
    spec: SoftwareSpecification,
    phase: FactoryPhase,
    round_num: int,
) -> str:
    contracts = [
        c.model_dump(mode="json")
        for c in blueprint.frozen_contracts
        if c.owner_module_id == module.module_id or c.capability_id in module.readable_contract_refs
    ]
    return "\n".join(
        [
            f"你是【{module.name}（{module.module_id}）】的部门副总管（DeptLead，只读规划角色）。",
            "你不直接编写源码，只负责输出严格的 DepartmentPlan JSON，指导 DeptCoder 与 DeptTester 工作。",
            f"- phase: {phase.value}",
            f"- dept_id: {module.module_id}",
            f"- round: {round_num}",
            f"- owned_paths: {json.dumps(module.owned_paths, ensure_ascii=False)}",
            f"- test_paths: {json.dumps(module.test_paths, ensure_ascii=False)}",
            f"- migration_range: {module.migration_range}",
            f"- goal: {spec.goal}",
            f"- related_contracts: {json.dumps(contracts, ensure_ascii=False)}",
            "请仅输出一个符合 DepartmentPlan Schema 的 JSON 对象。",
        ]
    )


def _build_lead_fix_prompt(
    *,
    module: Module,
    phase: FactoryPhase,
    round_num: int,
    ticket: DefectTicket,
) -> str:
    # 再次兜底确保传给 DeptLead 的缺陷单日志已脱敏且 <= 4 KB
    safe_ticket = ticket.model_copy(
        update={"log_excerpt": sanitize_defect_log(ticket.log_excerpt, max_bytes=MAX_DEFECT_LOG_BYTES)}
    )
    return "\n".join(
        [
            f"你是【{module.name}（{module.module_id}）】的部门副总管（DeptLead，只读返工拆解角色）。",
            "请依据以下脱敏 DefectTicket 输出严格的 FixDirective JSON，精准指出根因与最小修复范围，不直接写代码。",
            f"- phase: {phase.value}",
            f"- dept_id: {module.module_id}",
            f"- round: {round_num}",
            f"- owned_paths: {json.dumps(module.owned_paths, ensure_ascii=False)}",
            f"- defect_ticket: {safe_ticket.model_dump_json()}",
            "请仅输出一个符合 FixDirective Schema 的 JSON 对象。",
        ]
    )


def _build_coder_prompt(
    *,
    module: Module,
    plan: DepartmentPlan | None,
    directive: FixDirective | None,
    round_num: int,
) -> str:
    lines = [
        f"部门：{module.module_id}（第 {round_num} 轮）",
        f"允许写入的业务路径（owned_paths）：{json.dumps(module.owned_paths, ensure_ascii=False)}",
        f"接口片段输出路径：{API_FRAGMENT_FILENAME}",
        "你可自主调用 search_assets / read_asset_file / apply_asset_to_staging 查 PG 资产库或直接编写。",
        "严禁修改 test_paths、底座文件、api/*Facade.java 或他部门目录。",
    ]
    if directive is not None:
        lines.append(f"【返工修复指令 FixDirective】{directive.model_dump_json()}")
    elif plan is not None:
        lines.append(f"【部门实施方案 DepartmentPlan】{plan.model_dump_json()}")
    return "\n".join(lines)


def _build_tester_prompt(
    *,
    module: Module,
    plan: DepartmentPlan | None,
    directive: FixDirective | None,
    coder_changes: FileChangeSet,
    round_num: int,
) -> str:
    lines = [
        f"部门：{module.module_id}（第 {round_num} 轮测试任务）",
        f"唯一允许写入的测试路径（test_paths）：{json.dumps(module.test_paths, ensure_ascii=False)}",
        "请针对本部门业务实现编写单元测试，并对跨模块依赖 Facade 使用 Mockito/Mock 按冻结契约隔离。",
        "严禁修改任何业务源码与底座文件。",
        f"本轮编码变更：{json.dumps(coder_changes.to_dict(), ensure_ascii=False)}",
    ]
    if directive is not None:
        lines.append(f"【返工测试关注点】{json.dumps(directive.test_adjustments, ensure_ascii=False)}")
    elif plan is not None:
        lines.append(f"【规划测试关注点】{json.dumps(plan.test_focus, ensure_ascii=False)}")
    return "\n".join(lines)


def _audit_engine_tools(engine: PatchEngine, *, allow_asset_tools: bool) -> frozenset[str]:
    """强制前置审计真实工具表：确认无 ``execute``/``task`` 且仅含受限白名单工具。"""
    names = audit_tool_table(engine.agent)
    forbidden = names & FORBIDDEN_TOOL_NAMES
    if forbidden:
        raise AppException(
            ErrorCode.FORBIDDEN,
            f"子 Agent 暴露禁止工具：{sorted(forbidden)}",
            status_code=403,
        )
    assert_restricted_tools(engine.agent, allow_asset_tools=allow_asset_tools)
    return frozenset(names)


class DepartmentRunner:
    """FP04：单部门三角色最小职责小循环执行引擎（``DeptLead → DeptCoder → DeptTester + ModuleVerifier``）。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        verifier: ModuleVerifier,
        lead_client: ModelClient | None = None,
        coder_model_factory: CoderModelFactory | None = None,
        tester_model_factory: TesterModelFactory | None = None,
        ledger: PgBudgetLedger | None = None,
        state_store: FactoryStateStore | None = None,
        lead_role: str = DEPT_LEAD_ROLE,
        coder_role: str = DEPT_CODER_ROLE,
        tester_role: str = DEPT_TESTER_ROLE,
        round_3_coder_role: str | None = None,
        reserve_micro: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._verifier = verifier
        self._lead_client = lead_client
        self._coder_model_factory = coder_model_factory
        self._tester_model_factory = tester_model_factory
        self._ledger = ledger or PgBudgetLedger(session_factory)
        self._state_store = state_store or FactoryStateStore(session_factory)
        self._lead_role = lead_role
        self._coder_role = coder_role
        self._tester_role = tester_role
        self._round_3_coder_role = round_3_coder_role
        self._reserve_micro = reserve_micro

    def _resolve_project_id(self, run_id: int) -> int:
        with session_scope(self._session_factory) as db:
            run = db.get(Run, run_id)
            if run is None:
                raise AppException(ErrorCode.RUN_NOT_FOUND, f"run {run_id} 不存在", status_code=404)
            return int(run.project_id)

    def _invoke_dept_lead(
        self,
        *,
        project_id: int,
        run_id: int,
        phase: FactoryPhase,
        dept_id: str,
        round_num: int,
        prompt: str,
    ) -> str:
        route: Route = resolve_route(self._lead_role)
        client = (
            self._lead_client
            if self._lead_client is not None
            else build_client_for_role(self._lead_role)
        )
        call_id = f"factory:{run_id}:{phase.value}:{dept_id}:lead:r{round_num}:{uuid.uuid4().hex[:8]}"
        price = route.price or lookup_price(route.model)
        max_out = route.max_output_tokens or 4096
        max_in = max(1, len(prompt) // 2)

        self._ledger.reserve_atomic(
            call_id=call_id,
            project_id=project_id,
            run_id=run_id,
            model_role=self._lead_role,
            provider=route.provider,
            model=route.model,
            reserve_micro=self._reserve_micro,
            max_input_tokens=max_in,
            max_output_tokens=max_out,
            dept_id=dept_id,
            phase=phase.value,
            round_num=round_num,
            price=price,
        )
        try:
            completion = client.complete(
                model=route.model,
                prompt=prompt,
                json_mode=True,
                max_output_tokens=max_out,
            )
        except ProviderPreflightError as exc:
            self._ledger.release_atomic(call_id, reason="ProviderPreflightError")
            raise AppException(
                ErrorCode.CONFIG_REQUIRED,
                f"角色 {self._lead_role} 前置配置不足：{exc}",
                status_code=422,
            ) from exc
        except Exception as exc:
            self._ledger.settle_idempotent(call_id, usage=None, error_class=type(exc).__name__)
            raise AppException(
                ErrorCode.MODEL_UNAVAILABLE,
                f"部门副总管调用模型失败（{type(exc).__name__}）",
                status_code=503,
                retryable=True,
            ) from exc

        self._ledger.settle_idempotent(
            call_id,
            usage=completion.usage,
            request_id=completion.request_id,
        )
        return completion.content

    def _build_coder_engine(
        self,
        *,
        project_id: int,
        run_id: int,
        phase: FactoryPhase,
        dept_id: str,
        round_num: int,
        coder_fb: CoderFileBackend,
    ) -> PatchEngine:
        active_role = self._coder_role
        if (
            round_num >= 3
            and self._round_3_coder_role
            and role_enabled(self._round_3_coder_role)
        ):
            active_role = self._round_3_coder_role
        binding = LedgerBinding(
            session_factory=self._session_factory,
            project_id=project_id,
            run_id=run_id,
            task_id=f"{phase.value}:{dept_id}:coder:r{round_num}",
            model_role=active_role,
            dept_id=dept_id,
            phase=phase.value,
            round_num=round_num,
        )
        model = (
            self._coder_model_factory(binding, round_num)
            if self._coder_model_factory is not None
            else build_ledger_model(active_role, binding)
        )
        asset_tools = build_agent_asset_tools(
            file_backend=coder_fb,
            session_factory=self._session_factory,
        )
        engine = build_patch_engine(
            ledger_binding=binding,
            file_backend=coder_fb,
            system_prompt=CODER_SYSTEM_PROMPT,
            model=model,
            extra_tools=asset_tools,
            allow_asset_tools=True,
        )
        _audit_engine_tools(engine, allow_asset_tools=True)
        return engine

    def _build_tester_engine(
        self,
        *,
        project_id: int,
        run_id: int,
        phase: FactoryPhase,
        dept_id: str,
        round_num: int,
        tester_fb: TesterFileBackend,
    ) -> PatchEngine:
        binding = LedgerBinding(
            session_factory=self._session_factory,
            project_id=project_id,
            run_id=run_id,
            task_id=f"{phase.value}:{dept_id}:tester:r{round_num}",
            model_role=self._tester_role,
            dept_id=dept_id,
            phase=phase.value,
            round_num=round_num,
        )
        model = (
            self._tester_model_factory(binding, round_num)
            if self._tester_model_factory is not None
            else build_ledger_model(self._tester_role, binding)
        )
        engine = build_patch_engine(
            ledger_binding=binding,
            file_backend=tester_fb,
            system_prompt=TESTER_SYSTEM_PROMPT,
            model=model,
            extra_tools=(),
            allow_asset_tools=False,
        )
        _audit_engine_tools(engine, allow_asset_tools=False)
        return engine

    def _build_evidence_and_ticket(
        self,
        *,
        run_id: int,
        phase: FactoryPhase,
        dept_id: str,
        round_num: int,
        stack: str,
        test_selector: str,
        test_files: list[str],
        verification: ModuleVerificationResult | None,
    ) -> tuple[TestEvidence, DefectTicket | None]:
        if verification is None:
            evidence = TestEvidence(
                dept_id=dept_id,
                phase=phase.value,
                round=round_num,
                verifier_kind=self._verifier.verifier_kind,
                stack="java" if stack == "java" else "frontend",
                test_selector=test_selector,
                status="NOT_RUN",
                executed=False,
                exit_code=None,
                test_files=test_files,
                error_code="TEST_NOT_EXECUTED",
                log_excerpt="测试验证工具未执行，拒绝放行。",
            )
            ticket = DefectTicket(
                ticket_id=f"ticket-{run_id}-{dept_id}-r{round_num}",
                run_id=run_id,
                phase=phase.value,
                dept_id=dept_id,
                scope="department",
                round=round_num,
                error_class="TEST_NOT_EXECUTED",
                summary="单模块测试工具未实际执行，拒绝标记 PASSED",
                affected_paths=list(test_files),
                log_excerpt=evidence.log_excerpt,
                verifier_kind=evidence.verifier_kind,
                exit_code=None,
            )
            return evidence, ticket

        executed = verification.status != "NOT_RUN" and verification.exit_code is not None
        evidence = TestEvidence(
            dept_id=dept_id,
            phase=phase.value,
            round=round_num,
            verifier_kind=verification.verifier_kind,
            stack=verification.stack,
            test_selector=verification.test_selector,
            status=verification.status,
            executed=executed,
            exit_code=verification.exit_code,
            duration_ms=verification.duration_ms,
            command=list(verification.command),
            test_files=test_files,
            error_code=verification.error_code,
            log_excerpt=verification.log_excerpt or verification.detail or "",
        )
        if evidence.passed:
            return evidence, None

        error_class = verification.error_code or (
            "TEST_NOT_EXECUTED" if not executed else "UNIT_TEST_FAILURE"
        )
        summary = (
            verification.detail
            or f"单模块验证未通过（status={verification.status}, exit_code={verification.exit_code}）"
        )
        ticket = DefectTicket(
            ticket_id=f"ticket-{run_id}-{dept_id}-r{round_num}",
            run_id=run_id,
            phase=phase.value,
            dept_id=dept_id,
            scope="department",
            round=round_num,
            error_class=error_class,
            summary=summary,
            failing_tests=[test_selector] if test_selector else [],
            affected_paths=list(test_files),
            log_excerpt=verification.log_excerpt or summary,
            verifier_kind=verification.verifier_kind,
            exit_code=verification.exit_code,
        )
        return evidence, ticket

    def run_department(
        self,
        run_id: int,
        module: Module,
        *,
        blueprint: ArchitectureBlueprint,
        spec: SoftwareSpecification,
        run_dir: str | Path,
        base_dir: str | Path | None = None,
        phase: FactoryPhase | str = FactoryPhase.BACKEND,
        max_rounds: int = DEFAULT_DEPT_MAX_ROUNDS,
        initial_ticket: DefectTicket | None = None,
        test_selector: str | None = None,
    ) -> DepartmentDeliverable:
        """执行单个部门的完整最小职责小循环（最多 ``max_rounds`` 轮）。"""
        resolved_phase = FactoryPhase(phase)
        dept_id = module.module_id
        project_id = self._resolve_project_id(run_id)
        stack = "java" if resolved_phase is FactoryPhase.BACKEND else "frontend"

        dept_staging, base_manifest = _prepare_department_staging(
            Path(run_dir).resolve(),
            phase=resolved_phase,
            dept_id=dept_id,
            base_dir=Path(base_dir).resolve() if base_dir is not None else None,
        )

        self._state_store.register_department(
            run_id,
            resolved_phase,
            dept_id,
            max_rounds=max_rounds,
            staging_dir=str(dept_staging),
        )

        coder_fb = CoderFileBackend(
            dept_staging,
            owned_paths=list(module.owned_paths),
            test_paths=list(module.test_paths),
            base_manifest=base_manifest,
        )
        tester_fb = TesterFileBackend(
            dept_staging,
            test_paths=list(module.test_paths),
            owned_paths=list(module.owned_paths),
            base_manifest=base_manifest,
        )

        plan: DepartmentPlan | None = None
        directives: list[FixDirective] = []
        tickets: list[DefectTicket] = []
        all_coder_changes: list[dict[str, Any]] = []
        all_tester_changes: list[dict[str, Any]] = []
        last_evidence: TestEvidence | None = None
        pending_ticket: DefectTicket | None = initial_ticket
        rounds_used = 0

        try:
            for round_num in range(1, max_rounds + 1):
                rounds_used = round_num
                self._state_store.raise_if_cancelled(
                    run_id, boundary=f"dept:{dept_id}:round_{round_num}:start"
                )

                # 1. PLANNING：DeptLead 只读规划（首次）或接 DefectTicket 出 FixDirective（返工）
                self._state_store.transition_department(
                    run_id,
                    resolved_phase,
                    dept_id,
                    DeptStatus.PLANNING,
                    current_round=round_num,
                    staging_dir=str(dept_staging),
                )
                current_directive: FixDirective | None = None
                if pending_ticket is None:
                    lead_prompt = _build_lead_plan_prompt(
                        module=module,
                        blueprint=blueprint,
                        spec=spec,
                        phase=resolved_phase,
                        round_num=round_num,
                    )
                    lead_raw = self._invoke_dept_lead(
                        project_id=project_id,
                        run_id=run_id,
                        phase=resolved_phase,
                        dept_id=dept_id,
                        round_num=round_num,
                        prompt=lead_prompt,
                    )
                    try:
                        payload = _extract_json_object(lead_raw)
                        payload.setdefault("dept_id", dept_id)
                        payload.setdefault("phase", resolved_phase.value)
                        payload.setdefault("round", round_num)
                        plan = DepartmentPlan.model_validate(payload)
                    except (ValueError, TypeError, ValidationError) as exc:
                        raise AppException(
                            ErrorCode.VALIDATION_ERROR,
                            f"DeptLead 规划 JSON 校验失败：{exc}",
                            status_code=422,
                        ) from exc
                else:
                    lead_prompt = _build_lead_fix_prompt(
                        module=module,
                        phase=resolved_phase,
                        round_num=round_num,
                        ticket=pending_ticket,
                    )
                    lead_raw = self._invoke_dept_lead(
                        project_id=project_id,
                        run_id=run_id,
                        phase=resolved_phase,
                        dept_id=dept_id,
                        round_num=round_num,
                        prompt=lead_prompt,
                    )
                    try:
                        payload = _extract_json_object(lead_raw)
                        payload.setdefault("dept_id", dept_id)
                        payload.setdefault("phase", resolved_phase.value)
                        payload.setdefault("round", round_num)
                        payload.setdefault("ticket_id", pending_ticket.ticket_id)
                        current_directive = FixDirective.model_validate(payload)
                        directives.append(current_directive)
                    except (ValueError, TypeError, ValidationError) as exc:
                        raise AppException(
                            ErrorCode.VALIDATION_ERROR,
                            f"DeptLead FixDirective JSON 校验失败：{exc}",
                            status_code=422,
                        ) from exc

                # 2. CODING：DeptCoder 仅写 owned_paths 与 api-fragment.json（启动前强制审计工具表）
                self._state_store.transition_department(
                    run_id,
                    resolved_phase,
                    dept_id,
                    DeptStatus.CODING,
                    current_round=round_num,
                )
                coder_engine = self._build_coder_engine(
                    project_id=project_id,
                    run_id=run_id,
                    phase=resolved_phase,
                    dept_id=dept_id,
                    round_num=round_num,
                    coder_fb=coder_fb,
                )
                coder_prompt = _build_coder_prompt(
                    module=module,
                    plan=plan,
                    directive=current_directive,
                    round_num=round_num,
                )
                coder_engine.run(coder_prompt)
                coder_cs = coder_fb.commit()
                all_coder_changes.extend(coder_cs.to_dict()["changes"])

                if base_manifest is not None:
                    assert_base_integrity(dept_staging, base_manifest)

                # 3. TESTING：DeptTester 仅写 test_paths + 真跑 ModuleVerifier
                self._state_store.transition_department(
                    run_id,
                    resolved_phase,
                    dept_id,
                    DeptStatus.TESTING,
                    current_round=round_num,
                )
                tester_engine = self._build_tester_engine(
                    project_id=project_id,
                    run_id=run_id,
                    phase=resolved_phase,
                    dept_id=dept_id,
                    round_num=round_num,
                    tester_fb=tester_fb,
                )
                tester_prompt = _build_tester_prompt(
                    module=module,
                    plan=plan,
                    directive=current_directive,
                    coder_changes=coder_cs,
                    round_num=round_num,
                )
                tester_engine.run(tester_prompt)
                tester_cs = tester_fb.commit()
                all_tester_changes.extend(tester_cs.to_dict()["changes"])

                if base_manifest is not None:
                    assert_base_integrity(dept_staging, base_manifest)

                selector, test_files = _derive_test_selector(
                    dept_staging=dept_staging,
                    module=module,
                    blueprint=blueprint,
                    stack=stack,
                    explicit_selector=test_selector,
                )
                verification = self._verifier.verify_module(
                    artifact_dir=dept_staging,
                    module_id=dept_id,
                    stack=stack,
                    test_selector=selector,
                )
                evidence, ticket = self._build_evidence_and_ticket(
                    run_id=run_id,
                    phase=resolved_phase,
                    dept_id=dept_id,
                    round_num=round_num,
                    stack=stack,
                    test_selector=selector,
                    test_files=test_files,
                    verification=verification,
                )
                last_evidence = evidence

                if evidence.passed:
                    self._state_store.resolve_department_tickets(
                        run_id, resolved_phase, dept_id, scope="department"
                    )
                    frag_path, frag_body = _load_api_fragment(dept_staging)
                    self._state_store.transition_department(
                        run_id,
                        resolved_phase,
                        dept_id,
                        DeptStatus.PASSED,
                        current_round=round_num,
                        api_fragment_json=json.dumps(frag_body, ensure_ascii=False, sort_keys=True)
                        if frag_path
                        else None,
                        evidence_json=evidence.model_dump_json(),
                    )
                    return DepartmentDeliverable(
                        run_id=run_id,
                        phase=resolved_phase.value,
                        dept_id=dept_id,
                        status="PASSED",
                        rounds_used=round_num,
                        staging_dir=str(dept_staging),
                        plan=plan,
                        fix_directives=directives,
                        change_set={"changes": [*all_coder_changes, *all_tester_changes]},
                        coder_changes=all_coder_changes,
                        tester_changes=all_tester_changes,
                        api_fragment_path=frag_path,
                        api_fragment=frag_body,
                        evidence=evidence,
                        defect_tickets=tickets,
                        reused_assets=list(plan.reused_assets) if plan else [],
                    )

                # 验证失败：持久化脱敏 DefectTicket，未达轮次上限则打回 REWORK，超限落 FAILED
                assert ticket is not None
                row = self._state_store.open_defect_ticket(
                    run_id,
                    resolved_phase,
                    dept_id,
                    scope=ticket.scope,
                    round_num=round_num,
                    error_class=ticket.error_class,
                    payload=ticket.model_dump(mode="json"),
                )
                persisted_ticket = ticket.model_copy(update={"ticket_id": str(row.id)})
                tickets.append(persisted_ticket)
                pending_ticket = persisted_ticket

                if round_num < max_rounds:
                    self._state_store.transition_department(
                        run_id,
                        resolved_phase,
                        dept_id,
                        DeptStatus.REWORK,
                        current_round=round_num,
                        evidence_json=evidence.model_dump_json(),
                        reason=ticket.error_class,
                    )
                else:
                    self._state_store.transition_department(
                        run_id,
                        resolved_phase,
                        dept_id,
                        DeptStatus.FAILED,
                        current_round=round_num,
                        evidence_json=evidence.model_dump_json(),
                        error_code=ticket.error_class,
                        reason="max_rounds_exceeded",
                    )
                    frag_path, frag_body = _load_api_fragment(dept_staging)
                    return DepartmentDeliverable(
                        run_id=run_id,
                        phase=resolved_phase.value,
                        dept_id=dept_id,
                        status="FAILED",
                        rounds_used=round_num,
                        staging_dir=str(dept_staging),
                        plan=plan,
                        fix_directives=directives,
                        change_set={"changes": [*all_coder_changes, *all_tester_changes]},
                        coder_changes=all_coder_changes,
                        tester_changes=all_tester_changes,
                        api_fragment_path=frag_path,
                        api_fragment=frag_body,
                        evidence=last_evidence,
                        defect_tickets=tickets,
                        reused_assets=list(plan.reused_assets) if plan else [],
                        error_code=ticket.error_class,
                    )
        except FactoryCancelledError:
            self._state_store.cancel_incomplete_departments(
                run_id, resolved_phase, reason="dept_cancelled"
            )
            frag_path, frag_body = _load_api_fragment(dept_staging)
            return DepartmentDeliverable(
                run_id=run_id,
                phase=resolved_phase.value,
                dept_id=dept_id,
                status="CANCELLED",
                rounds_used=rounds_used,
                staging_dir=str(dept_staging),
                plan=plan,
                fix_directives=directives,
                change_set={"changes": [*all_coder_changes, *all_tester_changes]},
                coder_changes=all_coder_changes,
                tester_changes=all_tester_changes,
                api_fragment_path=frag_path,
                api_fragment=frag_body,
                evidence=last_evidence,
                defect_tickets=tickets,
                reused_assets=list(plan.reused_assets) if plan else [],
                error_code="RUN_CANCELLED",
            )
        except AppException as exc:
            dept_row = self._state_store.get_department(run_id, resolved_phase, dept_id)
            if dept_row is not None and DeptStatus(dept_row.status) not in {
                DeptStatus.PASSED,
                DeptStatus.FAILED,
                DeptStatus.CANCELLED,
            }:
                self._state_store.transition_department(
                    run_id,
                    resolved_phase,
                    dept_id,
                    DeptStatus.FAILED,
                    current_round=rounds_used,
                    error_code=exc.code.value,
                    check_cancel=False,
                )
            raise

        # 理论不可达（max_rounds >= 1），兜底保护
        raise AppException(
            ErrorCode.INTERNAL_ERROR,
            f"部门 {dept_id} 异常退出循环",
            status_code=500,
        )


__all__ = [
    "API_FRAGMENT_FILENAME",
    "CODER_SYSTEM_PROMPT",
    "DEFAULT_DEPT_MAX_ROUNDS",
    "DEPT_CODER_ROLE",
    "DEPT_LEAD_ROLE",
    "DEPT_TESTER_ROLE",
    "TESTER_SYSTEM_PROMPT",
    "CoderFileBackend",
    "DepartmentRunner",
    "TesterFileBackend",
]
