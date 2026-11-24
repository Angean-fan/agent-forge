"""FP08：前端多部门异步并行流水线、声明式路由自动聚合与前端总装大循环（``factory_frontend_stage.py``）。

核心职责：
1. ``aggregate_frontend_routes(deliverables) -> str``：纯确定性（0 模型调用）扫描各前端部门
   ``frontend/src/modules/*/routes.ts``，检测路由路径冲突与重复模块目录，并生成严格 TypeScript
   （``strict: true``）的只读 ``frontend/src/router/generated-routes.ts``；
2. 确定性总装前门禁（``merge_frontend_deliverables``）：
   - 核验每个前端部门均具备真实执行通过的测试证据（``evidence.passed``，杜绝口头放行）；
   - 核验每个前端部门暂存区对只读前端底座（``src/generated/*``、``src/core/*``、``package.json`` 等）零篡改；
   - 核验跨部门零文件路径重叠、零声明式路由冲突；
   - 核验前端源码调用的 ``/api/*`` 路径与 ``API_ENDPOINTS.<op>`` 全部真实存在于权威冻结 ``openapi.json``，
     且需要鉴权的业务模块未遗漏 ``requiresAuth: true`` 守卫；
   - 静态安全门禁：禁止硬编码真实密钥（复用 ``scan_for_secrets`` + JS/TS 密钥模式），禁止使用 ``v-html`` /
     ``innerHTML`` 渲染未转义用户输入（基础 XSS 防护）；
3. ``run_frontend_factory_stage(...)``：串联与后端完全同构的前端流水线：
   ``后端就绪门禁 → 前端多部门异步并行小循环（双信号量 + max_dept_rounds=3）→ 前端总装与路由聚合``
   ``→ 全量前端构建/类型检查/单测 → INFRASTRUCTURE 零返工即停 / 业务失败总管归因定向打回（上限 2 轮）``。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from agentforge_platform.agent_files import STAGING_DIRNAME, _within_allowed
from agentforge_platform.assembler import _EXCLUDED_COPY, _is_within, _validate_rel
from agentforge_platform.asset_cache import make_writable_tree, remove_readonly_tree
from agentforge_platform.blueprint_contracts import ArchitectureBlueprint
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.external_requirements import scan_for_secrets
from agentforge_platform.factory_base_builder import (
    READONLY_MANIFEST_NAME,
    BaseManifest,
    check_department_paths_against_base,
    load_base_manifest,
    verify_base_integrity,
)
from agentforge_platform.factory_chief import ChiefArchitect
from agentforge_platform.factory_department import API_FRAGMENT_FILENAME
from agentforge_platform.factory_frontend_base import (
    _HTTP_METHODS,
    GENERATED_ROUTES_REL,
    _operation_key,
    require_backend_ready_for_frontend,
)
from agentforge_platform.factory_scheduler import AsyncDepartmentPool
from agentforge_platform.factory_state import (
    DeptStatus,
    FactoryCancelledError,
    FactoryPhase,
    FactoryStateStore,
)
from agentforge_platform.factory_tickets import DefectTicket, DepartmentDeliverable
from agentforge_platform.module_verifier import (
    ModuleVerificationResult,
    ModuleVerifier,
    run_final_acceptance_verify,
    to_runner_artifact_dir,
)
from agentforge_platform.runner_client import RunnerClient
from agentforge_platform.spec import SoftwareSpecification

_STRICT = ConfigDict(extra="forbid")

MAX_INTEGRATION_ROUNDS_ENV = "AGENTFORGE_FACTORY_MAX_INTEGRATION_ROUNDS"
DEFAULT_MAX_INTEGRATION_ROUNDS = 2
FRONTEND_ASSEMBLED_DIRNAME = "frontend_assembled"
FRONTEND_INTEGRATION_REPORT_NAME = "frontend-integration-report.json"

_BASE_RESERVED_ROUTES: frozenset[str] = frozenset({"/", "/login"})
_MODULE_ROUTES_RE = re.compile(r"^frontend/src/modules/([a-z][a-z0-9_-]*)/routes\.ts$")
_ROUTE_PATH_RE = re.compile(r"""\bpath\s*:\s*(['"`])(/[^'"`\n]*)\1""")
_EXPORT_DEFAULT_RE = re.compile(r"\bexport\s+default\b")
_EXPORT_NAMED_ROUTES_RE = re.compile(r"\bexport\s+(?:const|let|var)\s+routes\b")
_REQUIRES_AUTH_TRUE_RE = re.compile(r"\brequiresAuth\s*:\s*true\b")
_REQUIRES_AUTH_FALSE_RE = re.compile(r"\brequiresAuth\s*:\s*false\b")

_API_LITERAL_RE = re.compile(r"""(['"`])(/api/[A-Za-z0-9_./:${}-]+)\1""")
_API_ENDPOINT_PROP_RE = re.compile(
    r"(?:\bAPI_ENDPOINTS(?:\s+as\s+[^)]+)?\)?)(?:\.([A-Za-z_][A-Za-z0-9_]*)|\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\])"
)
_TEMPLATE_EXPR_RE = re.compile(r"\$\{[^}]+\}")
_OPENAPI_PARAM_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")

# XSS 与前端硬编码密钥静态检测正则
_V_HTML_RE = re.compile(r"(?:^|[\s<])v-html(?:\s*=|\s|>)", re.MULTILINE)
_INNER_HTML_RE = re.compile(r"\.\s*innerHTML\s*=")
_JS_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "包含疑似模型/第三方 API Key（sk-*）"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs)_[A-Za-z0-9]{16,}\b"), "包含疑似 GitHub Token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"), "包含疑似 GitHub Fine-Grained Token"),
    (
        re.compile(
            r"""(?i)\b(?:api_?key|secret_?key|access_?secret|private_?key|client_?secret|jwt_?secret)\b\s*[:=]\s*['"`]([^'"`\s]{8,})['"`]"""
        ),
        "包含硬编码密钥常量",
    ),
    (
        re.compile(r"""Bearer\s+eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"""),
        "包含硬编码 Bearer JWT 令牌",
    ),
)

_INFRA_MARKERS = (
    "infrastructure",
    "docker",
    "connection refused",
    "cannot connect to the docker daemon",
    "image not found",
    "pull access denied",
    "runner 连接失败",
    "runner http 5",
    "command not found",
    "executable file not found",
    "enotfound",
    "econnrefused",
    "timeout",
    "timed out",
)

_FRONTEND_SCAN_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".vue", ".json", ".env"})
_FRONTEND_CODE_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".vue"})


def max_frontend_integration_rounds_limit(override: int | None = None) -> int:
    """前端总装大循环上限：默认 2，可经环境变量收紧（区间 [1, 2]）。"""
    if override is not None:
        return max(1, min(int(override), DEFAULT_MAX_INTEGRATION_ROUNDS))
    raw = os.environ.get(MAX_INTEGRATION_ROUNDS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_INTEGRATION_ROUNDS
    try:
        return max(1, min(int(raw), DEFAULT_MAX_INTEGRATION_ROUNDS))
    except ValueError:
        return DEFAULT_MAX_INTEGRATION_ROUNDS


class FrontendRouteEntry(BaseModel):
    """单个前端业务部门声明的路由模块元信息。"""

    model_config = _STRICT

    dept_id: str
    module_dir: str
    rel_path: str
    import_ident: str
    import_specifier: str
    export_style: Literal["default", "named_routes"] = "default"
    declared_paths: list[str] = []
    has_requires_auth_true: bool = False
    has_requires_auth_false: bool = False


class FrontendMergeResult(BaseModel):
    """``merge_frontend_deliverables`` 返回的结构化前端总装结果。"""

    model_config = _STRICT

    ok: bool
    output_dir: str
    merged_files: list[str] = []
    generated_routes_path: str | None = None
    generated_routes_ts: str = ""
    route_entries: list[FrontendRouteEntry] = []
    conflict_tickets: list[DefectTicket] = []
    manifest_hash: str | None = None


class FrontendStageResult(BaseModel):
    """前端工厂阶段（多部门并发 + 总装路由聚合 + 构建验证 + 归因大循环）最终结果。"""

    model_config = _STRICT

    run_id: int
    ok: bool
    status: Literal["PASSED", "FAILED", "INFRASTRUCTURE", "CANCELLED", "PAUSED_BUDGET"]
    integration_rounds_used: int = 0
    rework_rounds: int = 0
    assembled_dir: str
    report_path: str | None = None
    generated_routes_path: str | None = None
    generated_routes_ts: str = ""
    verify_report: dict[str, Any] = {}
    reworked_depts_history: list[list[str]] = []
    triage_tickets: list[DefectTicket] = []
    deliverables: dict[str, DepartmentDeliverable] = {}
    error_code: str | None = None


def _normalize_deliverables(
    deliverables: Mapping[str, DepartmentDeliverable] | Sequence[DepartmentDeliverable],
) -> dict[str, DepartmentDeliverable]:
    if isinstance(deliverables, Mapping):
        return {str(k): v for k, v in deliverables.items()}
    return {d.dept_id: d for d in deliverables}


def _camel_ident(module_dir: str) -> str:
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", module_dir) if t]
    if not tokens:
        return "moduleRoutes"
    first = tokens[0].lower()
    rest = "".join(t[:1].upper() + t[1:] for t in tokens[1:])
    ident = f"{first}{rest}Routes"
    if ident[0].isdigit():
        ident = f"mod{ident}"
    return ident


def _normalize_route_path(raw: str) -> str:
    cleaned = raw.strip()
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    cleaned = re.sub(r"/+", "/", cleaned)
    if len(cleaned) > 1 and cleaned.endswith("/"):
        cleaned = cleaned[:-1]
    # 将动态参数 :id / :orderId 归一化为 :param 以便冲突检测
    return re.sub(r":[A-Za-z_][A-Za-z0-9_]*", ":param", cleaned)


def _iter_dept_candidate_files(
    dept_staging: Path,
    base_file_set: frozenset[str],
) -> list[str]:
    owned: list[str] = []
    for path in sorted(dept_staging.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(dept_staging).as_posix()
        parts = PurePosixPath(rel).parts
        if any(p in _EXCLUDED_COPY or p == STAGING_DIRNAME for p in parts):
            continue
        if rel in (API_FRAGMENT_FILENAME, READONLY_MANIFEST_NAME):
            continue
        if rel in base_file_set:
            continue
        owned.append(rel)
    return owned


def scan_frontend_routes(
    deliverables: Mapping[str, DepartmentDeliverable] | Sequence[DepartmentDeliverable],
) -> tuple[list[FrontendRouteEntry], list[tuple[str, str, list[str]]]]:
    """确定性扫描全部前端部门 ``src/modules/*/routes.ts`` 并检测目录/路径冲突。

    返回 ``(route_entries, conflicts)``，其中每条冲突为 ``(dept_id, summary, affected_paths)``。
    """
    by_dept = _normalize_deliverables(deliverables)
    entries: list[FrontendRouteEntry] = []
    conflicts: list[tuple[str, str, list[str]]] = []
    seen_module_dirs: dict[str, str] = {}
    seen_route_paths: dict[str, tuple[str, str]] = {}

    for dept_id in sorted(by_dept):
        deliverable = by_dept[dept_id]
        staging_root = Path(deliverable.staging_dir) if deliverable.staging_dir else None
        if staging_root is None or not staging_root.is_dir():
            continue

        modules_root = staging_root / "frontend" / "src" / "modules"
        if not modules_root.is_dir():
            continue

        for route_file in sorted(modules_root.glob("*/routes.ts")):
            if not route_file.is_file() or route_file.is_symlink():
                continue
            rel = route_file.relative_to(staging_root).as_posix()
            match = _MODULE_ROUTES_RE.fullmatch(rel)
            if not match:
                continue
            module_dir = match.group(1)

            if module_dir in seen_module_dirs and seen_module_dirs[module_dir] != dept_id:
                other = seen_module_dirs[module_dir]
                conflicts.append(
                    (
                        dept_id,
                        f"前端路由模块目录冲突：src/modules/{module_dir}/routes.ts 同时被部门 {other} 与 {dept_id} 产出",
                        [rel],
                    )
                )
                continue
            seen_module_dirs[module_dir] = dept_id

            try:
                content = route_file.read_text(encoding="utf-8")
            except OSError as exc:
                conflicts.append(
                    (dept_id, f"无法读取路由文件 {rel}：{exc}", [rel])
                )
                continue

            if _EXPORT_DEFAULT_RE.search(content):
                export_style: Literal["default", "named_routes"] = "default"
            elif _EXPORT_NAMED_ROUTES_RE.search(content):
                export_style = "named_routes"
            else:
                conflicts.append(
                    (
                        dept_id,
                        f"路由文件 {rel} 缺少 `export default` 或 `export const routes` 导出",
                        [rel],
                    )
                )
                continue

            declared_paths: list[str] = []
            for m in _ROUTE_PATH_RE.finditer(content):
                raw_path = m.group(2).strip()
                norm_path = _normalize_route_path(raw_path)
                declared_paths.append(raw_path)
                if norm_path in _BASE_RESERVED_ROUTES:
                    conflicts.append(
                        (
                            dept_id,
                            f"前端路由冲突：{rel} 声明的路由 {raw_path!r} 与只读底座保留路由冲突",
                            [rel],
                        )
                    )
                    continue
                if norm_path in seen_route_paths:
                    other_dept, other_rel = seen_route_paths[norm_path]
                    if other_dept != dept_id or other_rel != rel:
                        conflicts.append(
                            (
                                dept_id,
                                f"前端路由路径冲突：{raw_path!r} 同时在 {other_dept}（{other_rel}）与 {dept_id}（{rel}）声明",
                                [rel],
                            )
                        )
                        continue
                seen_route_paths[norm_path] = (dept_id, rel)

            entries.append(
                FrontendRouteEntry(
                    dept_id=dept_id,
                    module_dir=module_dir,
                    rel_path=rel,
                    import_ident=_camel_ident(module_dir),
                    import_specifier=f"../modules/{module_dir}/routes",
                    export_style=export_style,
                    declared_paths=declared_paths,
                    has_requires_auth_true=bool(_REQUIRES_AUTH_TRUE_RE.search(content)),
                    has_requires_auth_false=bool(_REQUIRES_AUTH_FALSE_RE.search(content)),
                )
            )

    entries.sort(key=lambda e: e.module_dir)
    return entries, conflicts


def _render_generated_routes_ts(entries: Sequence[FrontendRouteEntry]) -> str:
    """将已校验无冲突的路由入口渲染为确定性、严格 TypeScript 的 ``generated-routes.ts``。"""
    sorted_entries = sorted(entries, key=lambda e: e.module_dir)
    lines: list[str] = [
        "/* eslint-disable */",
        "/**",
        " * AUTO-GENERATED READ-ONLY FILE by AgentForge FP08 declarative route aggregator.",
        " * Aggregated deterministically from frontend/src/modules/<module>/routes.ts.",
        " */",
        "import type { RouteRecordRaw } from 'vue-router';",
    ]
    if sorted_entries:
        lines.append("")
        used_idents: set[str] = set()
        resolved: list[tuple[str, FrontendRouteEntry]] = []
        for entry in sorted_entries:
            ident = entry.import_ident
            suffix = 2
            while ident in used_idents:
                ident = f"{entry.import_ident}{suffix}"
                suffix += 1
            used_idents.add(ident)
            resolved.append((ident, entry))
            if entry.export_style == "named_routes":
                lines.append(
                    f"import {{ routes as {ident} }} from {json.dumps(entry.import_specifier)};"
                )
            else:
                lines.append(f"import {ident} from {json.dumps(entry.import_specifier)};")
        lines.extend(
            [
                "",
                "function normalizeRoutes(entry: RouteRecordRaw | readonly RouteRecordRaw[]): RouteRecordRaw[] {",
                "  return Array.isArray(entry) ? [...(entry as readonly RouteRecordRaw[])] : [entry as RouteRecordRaw];",
                "}",
                "",
                "export const generatedModuleRoutes: RouteRecordRaw[] = [",
            ]
        )
        for ident, _entry in resolved:
            lines.append(f"  ...normalizeRoutes({ident}),")
        lines.extend(
            [
                "];",
                "",
                "export default generatedModuleRoutes;",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "export const generatedModuleRoutes: RouteRecordRaw[] = [];",
                "",
                "export default generatedModuleRoutes;",
                "",
            ]
        )
    return "\n".join(lines)


def aggregate_frontend_routes(
    deliverables: Mapping[str, DepartmentDeliverable] | Sequence[DepartmentDeliverable],
) -> str:
    """FP08 核心公开接口：确定性扫描所有部门 ``src/modules/*/routes.ts`` 并生成 ``generated-routes.ts``。

    若发现重复模块目录、缺少路由导出或任意路由路径冲突，直接抛出 ``ASSET_CONFLICT``。
    """
    entries, conflicts = scan_frontend_routes(deliverables)
    if conflicts:
        detail = "；".join(summary for _, summary, _ in conflicts)
        raise AppException(
            ErrorCode.ASSET_CONFLICT,
            f"前端路由聚合检测到冲突：{detail}",
            status_code=422,
        )
    return _render_generated_routes_ts(entries)


def _build_openapi_lookup(
    openapi_doc: dict[str, Any],
) -> tuple[list[tuple[str, re.Pattern[str], bool]], dict[str, tuple[str, bool]]]:
    """从权威 ``openapi.json`` 构建路径正则表与 ``API_ENDPOINTS`` 操作键表。

    返回：
    - ``path_matchers``: ``[(openapi_path, compiled_regex, requires_auth), ...]``
    - ``op_keys``: ``{op_key: (openapi_path, requires_auth)}``
    """
    paths = openapi_doc.get("paths") if isinstance(openapi_doc.get("paths"), dict) else {}
    path_matchers: list[tuple[str, re.Pattern[str], bool]] = []
    op_keys: dict[str, tuple[str, bool]] = {}
    used_keys: set[str] = set()

    for path_str in sorted(paths):
        path_item = paths[path_str]
        if not isinstance(path_item, dict):
            continue
        any_public = False
        any_op = False
        for method in _HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            any_op = True
            auth_list = op.get("x-auth") if isinstance(op.get("x-auth"), list) else []
            is_public = "PUBLIC" in {str(a).upper() for a in auth_list}
            if is_public:
                any_public = True
            op_key = _operation_key(method, path_str, op)
            while op_key in used_keys:
                op_key += "Op"
            used_keys.add(op_key)
            op_keys[op_key] = (path_str, not is_public)

        if any_op:
            escaped = re.escape(path_str)
            pattern_str = re.sub(r"\\\{[A-Za-z_][A-Za-z0-9_]*\\\}", r"[^/?#]+", escaped)
            path_matchers.append((path_str, re.compile(f"^{pattern_str}$"), not any_public))

    return path_matchers, op_keys


def _match_api_literal(
    raw_url: str,
    path_matchers: Sequence[tuple[str, re.Pattern[str], bool]],
) -> tuple[str, bool] | None:
    """将前端源码中的 ``/api/...`` 字面量（含模板插值 ``${...}``）匹配到权威 OpenAPI 路径。"""
    cleaned = raw_url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if not cleaned or cleaned == "/api":
        return None
    normalized = _TEMPLATE_EXPR_RE.sub("__param__", cleaned)
    normalized = _OPENAPI_PARAM_RE.sub("__param__", normalized)
    for oa_path, regex, req_auth in path_matchers:
        if regex.fullmatch(normalized):
            return oa_path, req_auth
    return None


def _make_frontend_ticket(
    *,
    run_id: int,
    dept_id: str,
    integration_round: int,
    error_class: str,
    summary: str,
    affected_paths: list[str],
    idx: int,
) -> DefectTicket:
    return DefectTicket(
        ticket_id=f"fe-merge-{run_id}-{dept_id}-r{integration_round}-{idx}",
        run_id=run_id,
        phase=FactoryPhase.FRONTEND.value,
        dept_id=dept_id,
        scope="integration",
        round=integration_round,
        error_class=error_class,
        summary=summary,
        affected_paths=affected_paths,
        log_excerpt=summary,
        verifier_kind="local_process",
    )


def audit_frontend_department_security_and_contracts(
    *,
    dept_id: str,
    dept_staging: Path,
    candidate_files: Sequence[str],
    module_test_paths: Sequence[str],
    openapi_doc: dict[str, Any],
    route_entries: Sequence[FrontendRouteEntry],
    run_id: int,
    integration_round: int,
    ticket_start_idx: int = 1,
) -> list[DefectTicket]:
    """确定性扫描单个前端部门产物的安全红线（密钥 / XSS ``v-html``）与权威 OpenAPI 契约对齐。"""
    path_matchers, op_keys = _build_openapi_lookup(openapi_doc)
    tickets: list[DefectTicket] = []
    next_idx = ticket_start_idx
    consumed_auth_flags: list[bool] = []

    for rel in candidate_files:
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix not in _FRONTEND_SCAN_SUFFIXES:
            continue
        file_path = dept_staging / rel
        try:
            text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        # 1. 硬编码真实密钥扫描（覆盖源码与测试文件）
        secret_hits = scan_for_secrets(text)
        for pattern, desc in _JS_SECRET_PATTERNS:
            if pattern.search(text):
                secret_hits.append(desc)
        if secret_hits:
            tickets.append(
                _make_frontend_ticket(
                    run_id=run_id,
                    dept_id=dept_id,
                    integration_round=integration_round,
                    error_class="SECURITY_VIOLATION",
                    summary=f"前端部门 {dept_id} 文件 {rel} 命中硬编码密钥安全红线："
                    + "；".join(sorted(set(secret_hits))),
                    affected_paths=[rel],
                    idx=next_idx,
                )
            )
            next_idx += 1

        is_test_file = bool(module_test_paths) and _within_allowed(rel, list(module_test_paths))
        if is_test_file or suffix not in _FRONTEND_CODE_SUFFIXES:
            continue

        # 2. XSS 基础静态检查：禁止 v-html 与 .innerHTML =
        if _V_HTML_RE.search(text) or _INNER_HTML_RE.search(text):
            tickets.append(
                _make_frontend_ticket(
                    run_id=run_id,
                    dept_id=dept_id,
                    integration_round=integration_round,
                    error_class="SECURITY_VIOLATION",
                    summary=f"前端部门 {dept_id} 文件 {rel} 使用了禁止的 `v-html` / `innerHTML`（XSS 安全红线）",
                    affected_paths=[rel],
                    idx=next_idx,
                )
            )
            next_idx += 1

        # 3. 权威 OpenAPI 端点对齐核对（字面量 /api/* 与 API_ENDPOINTS.<key>）
        for m in _API_LITERAL_RE.finditer(text):
            raw_url = m.group(2)
            matched = _match_api_literal(raw_url, path_matchers)
            if matched is None:
                tickets.append(
                    _make_frontend_ticket(
                        run_id=run_id,
                        dept_id=dept_id,
                        integration_round=integration_round,
                        error_class="CONTRACT_MISMATCH",
                        summary=f"前端部门 {dept_id} 文件 {rel} 调用了权威 openapi.json 中不存在的端点：{raw_url}",
                        affected_paths=[rel],
                        idx=next_idx,
                    )
                )
                next_idx += 1
            else:
                consumed_auth_flags.append(matched[1])

        for m in _API_ENDPOINT_PROP_RE.finditer(text):
            op_key = m.group(1) or m.group(2)
            if not op_key:
                continue
            if op_key not in op_keys:
                tickets.append(
                    _make_frontend_ticket(
                        run_id=run_id,
                        dept_id=dept_id,
                        integration_round=integration_round,
                        error_class="CONTRACT_MISMATCH",
                        summary=f"前端部门 {dept_id} 文件 {rel} 引用了未知契约端点：API_ENDPOINTS.{op_key}",
                        affected_paths=[rel],
                        idx=next_idx,
                    )
                )
                next_idx += 1
            else:
                consumed_auth_flags.append(op_keys[op_key][1])

        # 也支持识别从 generated/api-endpoints 导入的强类型请求函数名
        if "generated/api-endpoints" in text:
            for op_key, (_oa_path, req_auth) in op_keys.items():
                if re.search(rf"\b{re.escape(op_key)}\b", text):
                    consumed_auth_flags.append(req_auth)

    # 4. 路由权限守卫核对：若本部门消费的 OpenAPI 端点全部需要鉴权，则 routes.ts 必须声明 requiresAuth: true
    dept_routes = [e for e in route_entries if e.dept_id == dept_id]
    if dept_routes and consumed_auth_flags and all(consumed_auth_flags):
        for entry in dept_routes:
            if entry.has_requires_auth_false or not entry.has_requires_auth_true:
                tickets.append(
                    _make_frontend_ticket(
                        run_id=run_id,
                        dept_id=dept_id,
                        integration_round=integration_round,
                        error_class="CONTRACT_MISMATCH",
                        summary=(
                            f"前端部门 {dept_id} 路由文件 {entry.rel_path} 消费的后端端点均需鉴权，"
                            "但遗漏 `meta: { requiresAuth: true }` 权限守卫（或显式设为 false）"
                        ),
                        affected_paths=[entry.rel_path],
                        idx=next_idx,
                    )
                )
                next_idx += 1

    return tickets


def merge_frontend_deliverables(
    base_dir: str | Path,
    deliverables: Mapping[str, DepartmentDeliverable] | Sequence[DepartmentDeliverable],
    output_dir: str | Path,
    *,
    blueprint: ArchitectureBlueprint,
    openapi_doc: dict[str, Any],
    run_id: int = 0,
    integration_round: int = 1,
) -> FrontendMergeResult:
    """FP08 确定性前端总装（0 模型调用）：安全/契约/底座/路由全检 → 合并源码 → 生成 ``generated-routes.ts``。"""
    base_root = Path(base_dir).resolve()
    out_root = Path(output_dir).resolve()
    manifest_file = base_root / READONLY_MANIFEST_NAME
    base_manifest: BaseManifest = load_base_manifest(manifest_file)
    base_file_set = frozenset(entry.path for entry in base_manifest.files)

    by_dept = _normalize_deliverables(deliverables)
    modules_by_id = {m.module_id: m for m in blueprint.modules}
    conflicts: list[DefectTicket] = []
    dept_files_map: dict[str, list[str]] = {}
    file_owners: dict[str, str] = {}

    # 1. 预扫所有前端部门 routes.ts 并检测路由冲突
    route_entries, route_conflicts = scan_frontend_routes(by_dept)
    for bad_dept, summary, affected in route_conflicts:
        conflicts.append(
            _make_frontend_ticket(
                run_id=run_id,
                dept_id=bad_dept,
                integration_round=integration_round,
                error_class="CONTRACT_MISMATCH",
                summary=summary,
                affected_paths=affected,
                idx=len(conflicts) + 1,
            )
        )

    # 2. 逐部门核验测试证据、只读底座完整性、路径隔离、安全红线与 OpenAPI 对齐
    for dept_id in sorted(modules_by_id):
        mod = modules_by_id[dept_id]
        deliverable = by_dept.get(dept_id)
        if deliverable is None or deliverable.status != "PASSED":
            conflicts.append(
                _make_frontend_ticket(
                    run_id=run_id,
                    dept_id=dept_id,
                    integration_round=integration_round,
                    error_class="DEPT_NOT_PASSED",
                    summary=f"前端部门 {dept_id} 尚未 PASSED，禁止进入前端总装",
                    affected_paths=list(mod.owned_paths),
                    idx=len(conflicts) + 1,
                )
            )
            continue

        if deliverable.evidence is None or not deliverable.evidence.passed:
            conflicts.append(
                _make_frontend_ticket(
                    run_id=run_id,
                    dept_id=dept_id,
                    integration_round=integration_round,
                    error_class="TEST_NOT_EXECUTED",
                    summary=f"前端部门 {dept_id} 缺少真实执行通过的编译/单测证据，禁止标 PASSED 或总装",
                    affected_paths=list(mod.test_paths),
                    idx=len(conflicts) + 1,
                )
            )
            continue

        dept_staging = Path(deliverable.staging_dir).resolve()
        if not dept_staging.is_dir():
            conflicts.append(
                _make_frontend_ticket(
                    run_id=run_id,
                    dept_id=dept_id,
                    integration_round=integration_round,
                    error_class="CONTRACT_MISMATCH",
                    summary=f"前端部门 {dept_id} 暂存目录不存在：{dept_staging}",
                    affected_paths=list(mod.owned_paths),
                    idx=len(conflicts) + 1,
                )
            )
            continue

        # 2a. 只读底座与契约类型防篡改核验
        integrity_issues = verify_base_integrity(dept_staging, base_manifest)
        for issue in integrity_issues:
            conflicts.append(
                _make_frontend_ticket(
                    run_id=run_id,
                    dept_id=dept_id,
                    integration_round=integration_round,
                    error_class="BASE_TAMPERED",
                    summary=f"前端部门 {dept_id} 篡改只读前端底座/TS 契约：{issue}",
                    affected_paths=[],
                    idx=len(conflicts) + 1,
                )
            )

        # 2b. 部门自有文件边界与零重叠核验
        candidate_files = _iter_dept_candidate_files(dept_staging, base_file_set)
        dept_files_map[dept_id] = candidate_files

        base_path_issues = check_department_paths_against_base(
            candidate_files, base_manifest
        )
        for issue in base_path_issues:
            conflicts.append(
                _make_frontend_ticket(
                    run_id=run_id,
                    dept_id=dept_id,
                    integration_round=integration_round,
                    error_class="CONTRACT_MISMATCH",
                    summary=issue,
                    affected_paths=[],
                    idx=len(conflicts) + 1,
                )
            )

        allowed_dept_paths = list(mod.owned_paths) + list(mod.test_paths)
        for rel in candidate_files:
            if not _within_allowed(rel, allowed_dept_paths):
                conflicts.append(
                    _make_frontend_ticket(
                        run_id=run_id,
                        dept_id=dept_id,
                        integration_round=integration_round,
                        error_class="CONTRACT_MISMATCH",
                        summary=f"前端部门 {dept_id} 产出越界文件（不在 owned_paths/test_paths 内）：{rel}",
                        affected_paths=[rel],
                        idx=len(conflicts) + 1,
                    )
                )
                continue
            if rel in file_owners and file_owners[rel] != dept_id:
                other = file_owners[rel]
                conflicts.append(
                    _make_frontend_ticket(
                        run_id=run_id,
                        dept_id=dept_id,
                        integration_round=integration_round,
                        error_class="CONTRACT_MISMATCH",
                        summary=f"跨前端部门文件路径冲突：{rel} 同时由 {other} 与 {dept_id} 产出",
                        affected_paths=[rel],
                        idx=len(conflicts) + 1,
                    )
                )
            else:
                file_owners[rel] = dept_id

        # 2c. 安全红线（密钥 / v-html XSS）与权威 OpenAPI 端点对齐核验
        audit_tickets = audit_frontend_department_security_and_contracts(
            dept_id=dept_id,
            dept_staging=dept_staging,
            candidate_files=candidate_files,
            module_test_paths=list(mod.test_paths),
            openapi_doc=openapi_doc,
            route_entries=route_entries,
            run_id=run_id,
            integration_round=integration_round,
            ticket_start_idx=len(conflicts) + 1,
        )
        conflicts.extend(audit_tickets)

    if conflicts:
        return FrontendMergeResult(
            ok=False,
            output_dir=str(out_root),
            route_entries=route_entries,
            conflict_tickets=conflicts,
            manifest_hash=base_manifest.manifest_hash,
        )

    # 3. 确定性生成聚合路由文件 generated-routes.ts
    generated_routes_ts = _render_generated_routes_ts(route_entries)

    # 4. 原子组装完整前端工程目录
    out_root.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_root.parent / f".{out_root.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if tmp_dir.exists():
        remove_readonly_tree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        src_fe = base_root / "frontend"
        if not src_fe.is_dir():
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"前端只读底座缺少 frontend/ 目录：{base_root}",
                status_code=422,
            )
        dst_fe = tmp_dir / "frontend"
        shutil.copytree(src_fe, dst_fe, ignore=shutil.ignore_patterns("node_modules"))
        make_writable_tree(dst_fe)
        src_nm = src_fe / "node_modules"
        dst_nm = dst_fe / "node_modules"
        if src_nm.exists() and not dst_nm.exists():
            os.symlink(src_nm.resolve(), dst_nm, target_is_directory=True)

        merged_files: list[str] = []
        for dept_id in sorted(dept_files_map):
            dept_staging = Path(by_dept[dept_id].staging_dir).resolve()
            for rel in dept_files_map[dept_id]:
                _validate_rel(rel)
                target = tmp_dir / rel
                if not _is_within(tmp_dir, target):
                    raise AppException(
                        ErrorCode.VALIDATION_ERROR,
                        f"前端总装路径越界：{rel}",
                        status_code=422,
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dept_staging / rel, target)
                merged_files.append(rel)

        # 写入聚合后的只读路由清单 frontend/src/router/generated-routes.ts
        routes_target = tmp_dir / GENERATED_ROUTES_REL
        routes_target.parent.mkdir(parents=True, exist_ok=True)
        routes_target.write_text(generated_routes_ts, encoding="utf-8")
        if GENERATED_ROUTES_REL not in merged_files:
            merged_files.append(GENERATED_ROUTES_REL)

        if out_root.exists():
            remove_readonly_tree(out_root)
        os.replace(tmp_dir, out_root)

        return FrontendMergeResult(
            ok=True,
            output_dir=str(out_root),
            merged_files=sorted(merged_files),
            generated_routes_path=str(out_root / GENERATED_ROUTES_REL),
            generated_routes_ts=generated_routes_ts,
            route_entries=route_entries,
            conflict_tickets=[],
            manifest_hash=base_manifest.manifest_hash,
        )
    except Exception:
        if tmp_dir.exists():
            remove_readonly_tree(tmp_dir)
        raise


def _is_infrastructure_report(report: dict[str, Any] | None) -> bool:
    if not isinstance(report, dict):
        return False
    overall = str(report.get("overall") or "").strip().upper()
    err = str(report.get("error_code") or report.get("category") or "").strip().upper()
    if overall == "INFRASTRUCTURE" or err == "INFRASTRUCTURE":
        return True
    for key in ("checks", "scenarios"):
        items = report.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item_cls = str(
                item.get("error_class") or item.get("category") or item.get("error_code") or ""
            ).strip().upper()
            if item_cls == "INFRASTRUCTURE":
                return True
            if str(item.get("status") or "").strip().upper() == "NOT_RUN":
                blob = f"{item.get('detail') or ''} {item.get('log_excerpt') or ''}".lower()
                if any(marker in blob for marker in _INFRA_MARKERS):
                    return True
    return False


def _run_assembled_frontend_verification(
    *,
    assembled_dir: Path,
    runner_client: RunnerClient | None,
    module_verifier: ModuleVerifier | None,
    work_root: str | Path | None = None,
) -> tuple[bool, dict[str, Any]]:
    """执行前端总装后的全量构建/类型检查/单测验证（杜绝未真跑即放行）。"""
    if runner_client is not None:
        runner_dir = to_runner_artifact_dir(assembled_dir, work_root)
        report = run_final_acceptance_verify(
            runner_client,
            runner_dir,
            checks=["frontend_build"],
        )
        if _is_infrastructure_report(report):
            raise _FrontendInfraStopError("Runner 前端总装验证报 INFRASTRUCTURE")
        checks = {
            str(c.get("name")): str(c.get("status"))
            for c in report.get("checks", [])
            if isinstance(c, dict)
        }
        passed = (
            report.get("overall") == "PASS"
            and bool(checks)
            and all(v == "PASS" for v in checks.values())
        )
        return passed, report

    if module_verifier is not None:
        verifier_dir: str | Path = (
            to_runner_artifact_dir(assembled_dir, work_root)
            if module_verifier.verifier_kind == "docker_runner"
            else assembled_dir
        )
        res: ModuleVerificationResult = module_verifier.verify_module(
            artifact_dir=verifier_dir,
            module_id="frontend-all",
            stack="frontend",
            test_selector="test",
        )
        executed = res.status != "NOT_RUN" and res.exit_code is not None
        report = {
            "overall": (
                "INFRASTRUCTURE"
                if res.error_code == "INFRASTRUCTURE"
                else ("PASS" if res.status == "PASS" and executed else "FAIL")
            ),
            "verifier_kind": res.verifier_kind,
            "error_code": res.error_code,
            "checks": [
                {
                    "name": "frontend_build",
                    "status": res.status,
                    "exit_code": res.exit_code,
                    "error_code": res.error_code,
                    "detail": res.detail,
                    "log_excerpt": res.log_excerpt,
                    "command": res.command,
                }
            ],
        }
        if res.error_code == "INFRASTRUCTURE":
            raise _FrontendInfraStopError("前端全量验证工具报 INFRASTRUCTURE")
        return (res.status == "PASS" and executed), report

    raise AppException(
        ErrorCode.CONFIG_REQUIRED,
        "前端阶段总装验证必须提供 runner_client 或 module_verifier，禁止无验证放行",
        status_code=422,
    )


def _write_frontend_integration_report(run_root: Path, payload: dict[str, Any]) -> str:
    report_file = run_root / FRONTEND_INTEGRATION_REPORT_NAME
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return str(report_file)


async def run_frontend_factory_stage(
    *,
    run_id: int,
    blueprint: ArchitectureBlueprint,
    spec: SoftwareSpecification,
    openapi_doc: dict[str, Any],
    run_dir: str | Path,
    base_dir: str | Path,
    pool: AsyncDepartmentPool,
    chief: ChiefArchitect,
    state_store: FactoryStateStore,
    runner_client: RunnerClient | None = None,
    module_verifier: ModuleVerifier | None = None,
    deliverables: Mapping[str, DepartmentDeliverable] | None = None,
    openapi_sha256: str | None = None,
    max_integration_rounds: int | None = None,
    work_root: str | Path | None = None,
) -> FrontendStageResult:
    """FP08 主入口：执行前端多部门异步并行开发、声明式路由聚合与全量构建契约核对大循环。"""
    if blueprint.phase != FactoryPhase.FRONTEND.value:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"run_frontend_factory_stage 仅接受 phase='frontend' 蓝图，收到：{blueprint.phase!r}",
            status_code=422,
        )
    if runner_client is None and module_verifier is None:
        raise AppException(
            ErrorCode.CONFIG_REQUIRED,
            "run_frontend_factory_stage 必须提供 runner_client 或 module_verifier",
            status_code=422,
        )

    # 1. 硬前置门禁：后端全部部门必须 PASSED 且权威 openapi.json 已冻结
    require_backend_ready_for_frontend(
        openapi_doc,
        expected_openapi_sha256=openapi_sha256,
        state_store=state_store,
        run_id=run_id,
    )

    run_root = Path(run_dir).resolve()
    base_root = Path(base_dir).resolve()
    assembled_dir = run_root / FRONTEND_ASSEMBLED_DIRNAME
    effective_max_rounds = max_frontend_integration_rounds_limit(max_integration_rounds)

    active_deliverables: dict[str, DepartmentDeliverable] = dict(deliverables or {})
    reworked_history: list[list[str]] = []
    all_triage_tickets: list[DefectTicket] = []
    rework_rounds = 0
    rounds_used = 0
    last_verify: dict[str, Any] = {}
    last_routes_path: str | None = None
    last_routes_ts = ""

    try:
        # 2. 若未预置部门产物，启动同构异步并发调度池（双信号量 + max_dept_rounds=3）
        if not active_deliverables:
            state_store.raise_if_cancelled(run_id, boundary="frontend_stage_before_pool")
            pool_res = await pool.run_phase(
                run_id=run_id,
                blueprint=blueprint,
                spec=spec,
                run_dir=run_root,
                base_dir=base_root,
                phase=FactoryPhase.FRONTEND,
            )
            active_deliverables = dict(pool_res.deliverables)
            if not pool_res.ok:
                report_path = _write_frontend_integration_report(
                    run_root,
                    {
                        "run_id": run_id,
                        "status": pool_res.status,
                        "error_code": pool_res.error_code,
                        "failed_depts": pool_res.failed_depts,
                        "cancelled_depts": pool_res.cancelled_depts,
                    },
                )
                return FrontendStageResult(
                    run_id=run_id,
                    ok=False,
                    status=pool_res.status,
                    integration_rounds_used=0,
                    rework_rounds=0,
                    assembled_dir=str(assembled_dir),
                    report_path=report_path,
                    deliverables=active_deliverables,
                    error_code=pool_res.error_code,
                )

        # 3. 前端总装与全量构建/契约核对大循环（硬上限 max_integration_rounds = 2）
        for integration_round in range(1, effective_max_rounds + 1):
            rounds_used = integration_round
            state_store.raise_if_cancelled(
                run_id,
                boundary=f"frontend_integration_round_{integration_round}",
            )

            merge_res: FrontendMergeResult = merge_frontend_deliverables(
                base_root,
                active_deliverables,
                assembled_dir,
                blueprint=blueprint,
                openapi_doc=openapi_doc,
                run_id=run_id,
                integration_round=integration_round,
            )

            # 3a. 确定性门禁失败（路由冲突 / 越权篡改 / 假端点 / 漏鉴权守卫 / XSS / 密钥）
            if not merge_res.ok:
                persisted_conflicts: list[DefectTicket] = []
                for t in merge_res.conflict_tickets:
                    row = state_store.open_defect_ticket(
                        run_id,
                        FactoryPhase.FRONTEND,
                        t.dept_id,
                        scope="integration",
                        round_num=integration_round,
                        error_class=t.error_class,
                        payload=t.model_dump(mode="json"),
                    )
                    persisted_conflicts.append(t.model_copy(update={"ticket_id": str(row.id)}))
                all_triage_tickets.extend(persisted_conflicts)

                primary_err = persisted_conflicts[0].error_class
                if integration_round >= effective_max_rounds:
                    report_path = _write_frontend_integration_report(
                        run_root,
                        {
                            "run_id": run_id,
                            "status": "FAILED",
                            "error_code": primary_err,
                            "integration_rounds_used": rounds_used,
                            "rework_rounds": rework_rounds,
                            "conflicts": [t.model_dump(mode="json") for t in persisted_conflicts],
                        },
                    )
                    return FrontendStageResult(
                        run_id=run_id,
                        ok=False,
                        status="FAILED",
                        integration_rounds_used=rounds_used,
                        rework_rounds=rework_rounds,
                        assembled_dir=str(assembled_dir),
                        report_path=report_path,
                        reworked_depts_history=reworked_history,
                        triage_tickets=all_triage_tickets,
                        deliverables=active_deliverables,
                        error_code=primary_err,
                    )

                blamed = sorted({t.dept_id for t in persisted_conflicts})
                rework_result = await _rework_frontend_departments(
                    run_id=run_id,
                    blamed_depts=blamed,
                    tickets=persisted_conflicts,
                    blueprint=blueprint,
                    spec=spec,
                    run_root=run_root,
                    base_root=base_root,
                    pool=pool,
                    state_store=state_store,
                    active_deliverables=active_deliverables,
                )
                reworked_history.append(blamed)
                rework_rounds += 1
                if not rework_result["ok"]:
                    return _build_frontend_stop_result(
                        run_id=run_id,
                        status=rework_result["status"],
                        error_code=rework_result["error_code"],
                        rounds_used=rounds_used,
                        rework_rounds=rework_rounds,
                        assembled_dir=assembled_dir,
                        run_root=run_root,
                        reworked_history=reworked_history,
                        all_triage_tickets=all_triage_tickets,
                        active_deliverables=active_deliverables,
                        last_verify=last_verify,
                    )
                continue

            last_routes_path = merge_res.generated_routes_path
            last_routes_ts = merge_res.generated_routes_ts

            # 3b. 全量前端编译/类型检查/单测验证（vue-tsc + vitest / Runner）
            try:
                passed, last_verify = _run_assembled_frontend_verification(
                    assembled_dir=assembled_dir,
                    runner_client=runner_client,
                    module_verifier=module_verifier,
                    work_root=work_root,
                )
            except (_FrontendInfraStopError, RuntimeError, OSError, TimeoutError) as infra_exc:
                last_verify = last_verify or {
                    "overall": "INFRASTRUCTURE",
                    "error_code": "INFRASTRUCTURE",
                    "detail": str(infra_exc),
                }
                state_store.record_event(
                    run_id,
                    "factory_infra_failed",
                    {
                        "phase": FactoryPhase.FRONTEND.value,
                        "integration_round": integration_round,
                        "detail": str(infra_exc)[:300],
                    },
                )
                report_path = _write_frontend_integration_report(
                    run_root,
                    {
                        "run_id": run_id,
                        "status": "INFRASTRUCTURE",
                        "error_code": "INFRASTRUCTURE",
                        "integration_rounds_used": rounds_used,
                        "rework_rounds": rework_rounds,
                        "verify_report": last_verify,
                    },
                )
                return FrontendStageResult(
                    run_id=run_id,
                    ok=False,
                    status="INFRASTRUCTURE",
                    integration_rounds_used=rounds_used,
                    rework_rounds=rework_rounds,
                    assembled_dir=str(assembled_dir),
                    report_path=report_path,
                    generated_routes_path=last_routes_path,
                    generated_routes_ts=last_routes_ts,
                    verify_report=last_verify,
                    reworked_depts_history=reworked_history,
                    triage_tickets=all_triage_tickets,
                    deliverables=active_deliverables,
                    error_code="INFRASTRUCTURE",
                )

            if passed:
                state_store.record_event(
                    run_id,
                    "factory_frontend_assembled",
                    {
                        "phase": FactoryPhase.FRONTEND.value,
                        "integration_round": integration_round,
                        "module_count": len(merge_res.route_entries),
                        "generated_routes_path": last_routes_path,
                    },
                )
                report_path = _write_frontend_integration_report(
                    run_root,
                    {
                        "run_id": run_id,
                        "status": "PASSED",
                        "integration_rounds_used": rounds_used,
                        "rework_rounds": rework_rounds,
                        "route_modules": [e.module_dir for e in merge_res.route_entries],
                        "reworked_depts_history": reworked_history,
                        "verify_report": last_verify,
                    },
                )
                return FrontendStageResult(
                    run_id=run_id,
                    ok=True,
                    status="PASSED",
                    integration_rounds_used=rounds_used,
                    rework_rounds=rework_rounds,
                    assembled_dir=str(assembled_dir),
                    report_path=report_path,
                    generated_routes_path=last_routes_path,
                    generated_routes_ts=last_routes_ts,
                    verify_report=last_verify,
                    reworked_depts_history=reworked_history,
                    triage_tickets=all_triage_tickets,
                    deliverables=active_deliverables,
                )

            # 3c. 全量构建/类型/测试失败：达上限落 FAILED，否则总管归因定向打回责任前端部门
            if integration_round >= effective_max_rounds:
                report_path = _write_frontend_integration_report(
                    run_root,
                    {
                        "run_id": run_id,
                        "status": "FAILED",
                        "error_code": "INTEGRATION_FAILED",
                        "integration_rounds_used": rounds_used,
                        "rework_rounds": rework_rounds,
                        "reworked_depts_history": reworked_history,
                        "verify_report": last_verify,
                    },
                )
                return FrontendStageResult(
                    run_id=run_id,
                    ok=False,
                    status="FAILED",
                    integration_rounds_used=rounds_used,
                    rework_rounds=rework_rounds,
                    assembled_dir=str(assembled_dir),
                    report_path=report_path,
                    generated_routes_path=last_routes_path,
                    generated_routes_ts=last_routes_ts,
                    verify_report=last_verify,
                    reworked_depts_history=reworked_history,
                    triage_tickets=all_triage_tickets,
                    deliverables=active_deliverables,
                    error_code="INTEGRATION_FAILED",
                )

            triage_tickets = chief.triage_integration_failures(
                run_id=run_id,
                blueprint=blueprint,
                failure_report={"verify_report": last_verify},
                phase=FactoryPhase.FRONTEND,
                integration_round=integration_round,
            )
            all_triage_tickets.extend(triage_tickets)
            blamed = sorted({t.dept_id for t in triage_tickets})

            rework_result = await _rework_frontend_departments(
                run_id=run_id,
                blamed_depts=blamed,
                tickets=triage_tickets,
                blueprint=blueprint,
                spec=spec,
                run_root=run_root,
                base_root=base_root,
                pool=pool,
                state_store=state_store,
                active_deliverables=active_deliverables,
            )
            reworked_history.append(blamed)
            rework_rounds += 1
            if not rework_result["ok"]:
                return _build_frontend_stop_result(
                    run_id=run_id,
                    status=rework_result["status"],
                    error_code=rework_result["error_code"],
                    rounds_used=rounds_used,
                    rework_rounds=rework_rounds,
                    assembled_dir=assembled_dir,
                    run_root=run_root,
                    reworked_history=reworked_history,
                    all_triage_tickets=all_triage_tickets,
                    active_deliverables=active_deliverables,
                    last_verify=last_verify,
                )

    except FactoryCancelledError:
        state_store.cancel_incomplete_departments(
            run_id, FactoryPhase.FRONTEND, reason="frontend_stage_cancelled"
        )
        report_path = _write_frontend_integration_report(
            run_root,
            {
                "run_id": run_id,
                "status": "CANCELLED",
                "error_code": "RUN_CANCELLED",
                "integration_rounds_used": rounds_used,
                "rework_rounds": rework_rounds,
            },
        )
        return FrontendStageResult(
            run_id=run_id,
            ok=False,
            status="CANCELLED",
            integration_rounds_used=rounds_used,
            rework_rounds=rework_rounds,
            assembled_dir=str(assembled_dir),
            report_path=report_path,
            generated_routes_path=last_routes_path,
            generated_routes_ts=last_routes_ts,
            verify_report=last_verify,
            reworked_depts_history=reworked_history,
            triage_tickets=all_triage_tickets,
            deliverables=active_deliverables,
            error_code="RUN_CANCELLED",
        )
    except AppException as exc:
        if exc.code is ErrorCode.BUDGET_EXCEEDED:
            report_path = _write_frontend_integration_report(
                run_root,
                {
                    "run_id": run_id,
                    "status": "PAUSED_BUDGET",
                    "error_code": ErrorCode.BUDGET_EXCEEDED.value,
                    "integration_rounds_used": rounds_used,
                    "rework_rounds": rework_rounds,
                },
            )
            return FrontendStageResult(
                run_id=run_id,
                ok=False,
                status="PAUSED_BUDGET",
                integration_rounds_used=rounds_used,
                rework_rounds=rework_rounds,
                assembled_dir=str(assembled_dir),
                report_path=report_path,
                generated_routes_path=last_routes_path,
                generated_routes_ts=last_routes_ts,
                verify_report=last_verify,
                reworked_depts_history=reworked_history,
                triage_tickets=all_triage_tickets,
                deliverables=active_deliverables,
                error_code=ErrorCode.BUDGET_EXCEEDED.value,
            )
        raise

    raise AppException(
        ErrorCode.INTERNAL_ERROR,
        f"前端阶段异常退出大循环（run_id={run_id}）",
        status_code=500,
    )


class _FrontendInfraStopError(Exception):
    """前端验证基础设施异常标记（零返工即停）。"""


async def _rework_frontend_departments(
    *,
    run_id: int,
    blamed_depts: list[str],
    tickets: list[DefectTicket],
    blueprint: ArchitectureBlueprint,
    spec: SoftwareSpecification,
    run_root: Path,
    base_root: Path,
    pool: AsyncDepartmentPool,
    state_store: FactoryStateStore,
    active_deliverables: dict[str, DepartmentDeliverable],
) -> dict[str, Any]:
    """仅定向打回被点名责任前端部门进入 ``REWORK`` 并重跑，其余未出错前端部门绝不重跑。"""
    ticket_by_dept: dict[str, DefectTicket] = {}
    for t in tickets:
        ticket_by_dept.setdefault(t.dept_id, t)

    for dept_id in blamed_depts:
        row = state_store.get_department(run_id, FactoryPhase.FRONTEND, dept_id)
        if row is not None and DeptStatus(row.status) is DeptStatus.PASSED:
            state_store.transition_department(
                run_id,
                FactoryPhase.FRONTEND,
                dept_id,
                DeptStatus.REWORK,
                allow_integration_rework=True,
                reason="frontend_integration_triage",
            )

    rework_res = await pool.run_phase(
        run_id=run_id,
        blueprint=blueprint,
        spec=spec,
        run_dir=run_root,
        base_dir=base_root,
        phase=FactoryPhase.FRONTEND,
        target_depts=blamed_depts,
        rework_tickets=ticket_by_dept,
    )
    for dept_id in blamed_depts:
        if dept_id in rework_res.deliverables:
            active_deliverables[dept_id] = rework_res.deliverables[dept_id]
        if rework_res.ok:
            state_store.resolve_department_tickets(
                run_id, FactoryPhase.FRONTEND, dept_id, scope="integration"
            )
    return {
        "ok": rework_res.ok,
        "status": rework_res.status,
        "error_code": rework_res.error_code,
    }


def _build_frontend_stop_result(
    *,
    run_id: int,
    status: Literal["PASSED", "FAILED", "CANCELLED", "PAUSED_BUDGET"],
    error_code: str | None,
    rounds_used: int,
    rework_rounds: int,
    assembled_dir: Path,
    run_root: Path,
    reworked_history: list[list[str]],
    all_triage_tickets: list[DefectTicket],
    active_deliverables: dict[str, DepartmentDeliverable],
    last_verify: dict[str, Any],
) -> FrontendStageResult:
    report_path = _write_frontend_integration_report(
        run_root,
        {
            "run_id": run_id,
            "status": status,
            "error_code": error_code,
            "integration_rounds_used": rounds_used,
            "rework_rounds": rework_rounds,
            "reworked_depts_history": reworked_history,
            "verify_report": last_verify,
        },
    )
    return FrontendStageResult(
        run_id=run_id,
        ok=False,
        status=status,
        integration_rounds_used=rounds_used,
        rework_rounds=rework_rounds,
        assembled_dir=str(assembled_dir),
        report_path=report_path,
        verify_report=last_verify,
        reworked_depts_history=reworked_history,
        triage_tickets=all_triage_tickets,
        deliverables=active_deliverables,
        error_code=error_code,
    )


__all__ = [
    "DEFAULT_MAX_INTEGRATION_ROUNDS",
    "FRONTEND_ASSEMBLED_DIRNAME",
    "FRONTEND_INTEGRATION_REPORT_NAME",
    "FrontendMergeResult",
    "FrontendRouteEntry",
    "FrontendStageResult",
    "aggregate_frontend_routes",
    "audit_frontend_department_security_and_contracts",
    "max_frontend_integration_rounds_limit",
    "merge_frontend_deliverables",
    "run_frontend_factory_stage",
    "scan_frontend_routes",
]
