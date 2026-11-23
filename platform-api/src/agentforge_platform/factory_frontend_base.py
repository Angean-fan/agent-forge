"""FP07：只读前端底座装配与基于权威 ``openapi.json`` 的 TypeScript 契约生成（``factory_frontend_base.py``）。

核心职责（全程确定性执行，**0 模型调用**）：
1. 前置门禁 :func:`require_backend_ready_for_frontend`：校验后端阶段已全部 ``PASSED``、权威
   ``openapi.json`` 已冻结（``x-agentforge-frozen=true``）且通过 E06 :func:`validate_openapi` 与 SHA-256 核验；
2. 蓝图硬校验 :func:`validate_frontend_blueprint`：校验前端部门数 ≤ 8、目录零交集、路由前缀无冲突、
   受保护路径不可被单部门宣称、全部冻结页面需求已覆盖、且声明的 HTTP 端点均存在于权威 ``openapi.json``；
3. :func:`generate_ts_api_types` 与 :func:`generate_ts_api_endpoints`：从权威 ``openapi.json``
   确定性生成无语法错误、严格 TypeScript（``strict: true``）的只读 ``frontend/src/generated/api-types.ts``
   与 ``frontend/src/generated/api-endpoints.ts``；
4. :func:`build_frontend_base`：通过 A03 :class:`AssetMaterializer` 物化认证前端模板，原子搭建
   ``run_dir/frontend_base/``（含全局布局/样式、受控 HTTP 拦截器 ``src/core/http.ts``、Auth Store、
   声明式路由聚合入口 ``src/router/generated-routes.ts`` 与只读 TS 契约），写入 ``readonly-manifest.json``
   并设为只读权限（``0o444``）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict

from agentforge_platform.agent_files import PROTECTED_PREFIXES
from agentforge_platform.assembler import (
    _EXCLUDED_COPY,
    AssemblyError,
    AssemblyLimits,
    _is_within,
    _validate_rel,
)
from agentforge_platform.asset_cache import AssetMaterializer, remove_readonly_tree
from agentforge_platform.blueprint_contracts import ArchitectureBlueprint
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.factory_base_builder import (
    READONLY_MANIFEST_NAME,
    BaseManifest,
    BaseManifestEntry,
    _compute_manifest_hash,
    _sha256_bytes,
)
from agentforge_platform.factory_state import DeptStatus, FactoryPhase, FactoryStateStore
from agentforge_platform.interface_registry import KIND_HTTP
from agentforge_platform.module_registry import registry_issues as module_registry_issues
from agentforge_platform.openapi_client import validate_openapi
from agentforge_platform.resolver import AssetLock, verify_lock_hash
from agentforge_platform.scope_policy import factory_max_modules
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import content_hash

_STRICT = ConfigDict(extra="forbid")

FRONTEND_BASE_DIRNAME = "frontend_base"
GENERATED_TYPES_REL = "frontend/src/generated/api-types.ts"
GENERATED_ENDPOINTS_REL = "frontend/src/generated/api-endpoints.ts"
GENERATED_ROUTES_REL = "frontend/src/router/generated-routes.ts"

FRONTEND_PROTECTED_PREFIXES: tuple[str, ...] = (
    "frontend/src/generated/",
    "frontend/src/core/",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/tsconfig.json",
    "frontend/vite.config.ts",
    "frontend/vitest.config.ts",
    "frontend/index.html",
    "frontend/src/main.ts",
    "frontend/src/App.vue",
    "frontend/src/api/client.ts",
    "frontend/src/api/auth.ts",
    "frontend/src/stores/auth.ts",
    "frontend/src/router/index.ts",
    "frontend/src/views/LoginView.vue",
)

_HTTP_METHODS = ("get", "post", "put", "delete", "patch")
_SAFE_TS_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATH_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class FrontendBaseBuildResult(BaseModel):
    """``build_frontend_base`` 返回的结构化结果。"""

    model_config = _STRICT

    base_dir: str
    frontend_project_dir: str
    manifest_path: str
    blueprint_hash: str
    openapi_sha256: str
    template_ref: str
    manifest_hash: str
    generated_files: list[str]
    protected_prefixes: list[str]
    files: list[BaseManifestEntry]


def canonical_openapi_sha256(openapi_doc: dict[str, Any]) -> str:
    payload = (json.dumps(openapi_doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def require_backend_ready_for_frontend(
    openapi_doc: dict[str, Any],
    *,
    expected_openapi_sha256: str | None = None,
    state_store: FactoryStateStore | None = None,
    run_id: int | None = None,
) -> str:
    """前端流水线启动硬门禁：校验后端阶段全 ``PASSED`` 且权威 ``openapi.json`` 已冻结、哈希有效。"""
    if state_store is not None and run_id is not None:
        backend_depts = state_store.list_departments(run_id, FactoryPhase.BACKEND)
        if not backend_depts:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"后端阶段尚未执行任何部门（run_id={run_id}），禁止启动前端流水线",
                status_code=409,
            )
        not_passed = [
            f"{d.dept_id}({d.status})"
            for d in backend_depts
            if DeptStatus(d.status) is not DeptStatus.PASSED
        ]
        if not_passed:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"后端阶段尚未全部 PASSED（未通过：{', '.join(not_passed)}），禁止启动前端流水线",
                status_code=409,
            )

    if not isinstance(openapi_doc, dict) or not openapi_doc:
        raise AppException(
            ErrorCode.OPENAPI_REJECTED,
            "缺少有效的权威 openapi.json 文档，禁止启动前端流水线",
            status_code=422,
        )
    if openapi_doc.get("x-agentforge-frozen") is not True:
        raise AppException(
            ErrorCode.OPENAPI_REJECTED,
            "权威 openapi.json 尚未在后端总验收全 PASS 后冻结（x-agentforge-frozen != true），禁止启动前端流水线",
            status_code=409,
        )

    # 复用 E06 OpenAPI 结构、规模与嵌套深度校验（本地权威文档无外部 servers）
    doc_for_check = {k: v for k, v in openapi_doc.items() if k != "servers"}
    oa_issues = validate_openapi(json.dumps(doc_for_check, ensure_ascii=False))
    if oa_issues:
        raise AppException(
            ErrorCode.OPENAPI_REJECTED,
            "权威 openapi.json 未通过结构校验：" + "；".join(oa_issues),
            status_code=422,
        )

    actual_sha = canonical_openapi_sha256(openapi_doc)
    if expected_openapi_sha256 is not None:
        clean_expected = expected_openapi_sha256.strip().lower()
        if len(clean_expected) != 64 or not re.fullmatch(r"[0-9a-f]{64}", clean_expected):
            raise AppException(
                ErrorCode.OPENAPI_REJECTED,
                f"非法 openapi_sha256 哈希格式：{expected_openapi_sha256!r}",
                status_code=422,
            )
        # 同时兼容直接文件字节哈希（sort_keys canonical）比对
        if actual_sha != clean_expected:
            compact_sha = hashlib.sha256(
                json.dumps(openapi_doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            if compact_sha != clean_expected:
                raise AppException(
                    ErrorCode.ASSET_CHECKSUM_MISMATCH,
                    f"权威 openapi.json 哈希不匹配（expected={clean_expected[:12]}, actual={actual_sha[:12]}）",
                    status_code=409,
                )
    return actual_sha


def validate_frontend_blueprint(
    blueprint: ArchitectureBlueprint,
    spec: SoftwareSpecification,
    openapi_doc: dict[str, Any],
) -> list[str]:
    """FP07 前端架构蓝图硬校验（部门上限 ≤ 8、目录零交集、路由前缀无冲突、保护路径零侵占、页面全覆盖、端点对齐 OpenAPI）。"""
    issues: list[str] = []
    if blueprint.phase != FactoryPhase.FRONTEND.value:
        issues.append(f"前端蓝图 phase 必须为 'frontend'，实际为 {blueprint.phase!r}")

    if not blueprint.modules:
        return [*issues, "前端架构蓝图缺少业务部门定义（modules 不能为空）"]

    max_mods = factory_max_modules()
    if len(blueprint.modules) > max_mods:
        issues.append(
            f"前端蓝图部门数超过工厂上限：最多 {max_mods} 个，当前 {len(blueprint.modules)} 个"
        )

    expected_spec_hash = content_hash(spec)
    if blueprint.based_on_spec_hash and blueprint.based_on_spec_hash != expected_spec_hash:
        issues.append(
            f"前端蓝图 spec_hash 不匹配：blueprint={blueprint.based_on_spec_hash[:12]} "
            f"!= spec={expected_spec_hash[:12]}"
        )

    # 1. 目录零交集 + 依赖无环
    issues.extend(module_registry_issues(blueprint.to_module_registry()))

    module_ids = {m.module_id for m in blueprint.modules}

    # 2. 独占路径与受保护路径隔离校验
    for mod in blueprint.modules:
        expected_mod_prefix = f"frontend/src/modules/{mod.module_id}/"
        for owned in mod.owned_paths:
            norm = owned.lstrip("/")
            if any(
                norm == p or norm.startswith(p)
                for p in FRONTEND_PROTECTED_PREFIXES
            ):
                issues.append(
                    f"前端部门 {mod.module_id} 禁止将受保护底座/契约路径纳入 owned_paths：{owned}"
                )
            elif not norm.startswith(expected_mod_prefix):
                issues.append(
                    f"前端部门 {mod.module_id} 的 owned_paths 必须位于 {expected_mod_prefix} 下：{owned}"
                )
        if not mod.test_paths:
            issues.append(f"前端部门 {mod.module_id} 缺少独占测试目录 test_paths")
        else:
            expected_test_prefix = f"frontend/test/{mod.module_id}"
            for tp in mod.test_paths:
                norm_tp = tp.lstrip("/")
                if not norm_tp.startswith(expected_test_prefix):
                    issues.append(
                        f"前端部门 {mod.module_id} 的 test_paths 必须位于 {expected_test_prefix}/ 下：{tp}"
                    )

    # 3. 路由前缀零冲突 + 声明的 HTTP 端点对齐权威 openapi.json
    oa_paths = openapi_doc.get("paths") if isinstance(openapi_doc.get("paths"), dict) else {}
    route_owners: dict[str, str] = {}
    for cap in blueprint.frozen_contracts:
        if cap.owner_module_id not in module_ids:
            issues.append(f"前端契约 {cap.capability_id} 所有者未知：{cap.owner_module_id}")
        if cap.interface_kind == KIND_HTTP and cap.http_path:
            raw_path = cap.http_path.strip()
            if raw_path.startswith("/api/"):
                # 引用后端 API 端点：必须真实存在于权威 openapi.json
                oa_item = oa_paths.get(raw_path)
                method = (cap.http_method or "GET").lower()
                if not isinstance(oa_item, dict) or method not in {
                    k.lower() for k in oa_item
                }:
                    issues.append(
                        f"前端部门 {cap.owner_module_id} 声明的后端端点 {method.upper()} {raw_path} "
                        "不存在于权威 openapi.json"
                    )
            else:
                # 前端页面路由挂载点：禁止前缀冲突
                norm_route = "/" + raw_path.strip("/")
                if norm_route in ("/", "/login"):
                    # 模板保留根路由与登录路由，允许 auth-ui / home-ui 唯一认领但不可重复认领
                    pass
                for existing_route, existing_owner in route_owners.items():
                    if existing_owner == cap.owner_module_id:
                        continue
                    if (
                        norm_route == existing_route
                        or (
                            norm_route != "/"
                            and existing_route != "/"
                            and (
                                norm_route.startswith(existing_route + "/")
                                or existing_route.startswith(norm_route + "/")
                            )
                        )
                    ):
                        issues.append(
                            f"前端路由前缀冲突：部门 {existing_owner}（{existing_route}）与 "
                            f"部门 {cap.owner_module_id}（{norm_route}）重叠"
                        )
                route_owners[norm_route] = cap.owner_module_id

    # 4. 全部业务页面需求覆盖（covered_pages）
    covered_page_set: set[str] = set()
    for mod_id, pages in blueprint.covered_pages.items():
        if mod_id not in module_ids:
            issues.append(f"covered_pages 引用了未知前端部门：{mod_id}")
        covered_page_set.update(p.strip().lower() for p in pages if p.strip())

    if spec.pages:
        missing_pages = [
            page.name for page in spec.pages if page.name.strip().lower() not in covered_page_set
        ]
        if missing_pages:
            issues.append("前端蓝图未覆盖全部冻结页面需求，遗漏：" + "、".join(missing_pages))

    return issues


# --------------------------------------------------------------------------- #
# 确定性 OpenAPI → TypeScript 契约类型与端点调用生成器
# --------------------------------------------------------------------------- #
def _pascal_case(raw: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+", raw)
    if not tokens:
        return "Unnamed"
    out = "".join(tok[0].upper() + tok[1:] for tok in tokens)
    if out[0].isdigit():
        out = "Api" + out
    return out


def _camel_case(raw: str) -> str:
    pascal = _pascal_case(raw)
    return pascal[0].lower() + pascal[1:]


def _schema_to_ts(schema: Any, *, known_schemas: set[str], depth: int = 0) -> str:
    if depth > 8 or not isinstance(schema, dict):
        return "unknown"

    if "$ref" in schema:
        ref = str(schema["$ref"])
        name = ref.rsplit("/", 1)[-1]
        safe_name = _pascal_case(name) if not _SAFE_TS_IDENT.fullmatch(name) else name
        ts = safe_name if safe_name in known_schemas else "Record<string, unknown>"
        return f"{ts} | null" if schema.get("nullable") else ts

    enum_vals = schema.get("enum")
    if isinstance(enum_vals, list) and enum_vals:
        literals = [json.dumps(v, ensure_ascii=False) for v in enum_vals]
        ts = " | ".join(literals)
        return f"({ts}) | null" if schema.get("nullable") else ts

    schema_type = str(schema.get("type") or "").lower()
    if schema_type == "string":
        ts = "string"
    elif schema_type in {"integer", "number"}:
        ts = "number"
    elif schema_type == "boolean":
        ts = "boolean"
    elif schema_type == "array":
        item_ts = _schema_to_ts(schema.get("items"), known_schemas=known_schemas, depth=depth + 1)
        ts = f"Array<{item_ts}>"
    elif schema_type == "object" or isinstance(schema.get("properties"), dict):
        props = schema.get("properties")
        if isinstance(props, dict) and props:
            req = set(schema.get("required") or [])
            parts: list[str] = []
            for prop_name, prop_schema in sorted(props.items()):
                key = prop_name if _SAFE_TS_IDENT.fullmatch(prop_name) else json.dumps(prop_name)
                opt = "" if prop_name in req else "?"
                prop_ts = _schema_to_ts(prop_schema, known_schemas=known_schemas, depth=depth + 1)
                parts.append(f"{key}{opt}: {prop_ts}")
            ts = "{ " + "; ".join(parts) + " }"
        else:
            ts = "Record<string, unknown>"
    else:
        ts = "unknown"

    return f"({ts}) | null" if schema.get("nullable") else ts


def _operation_key(method: str, path: str, op: dict[str, Any]) -> str:
    raw_op_id = str(op.get("operationId") or "").strip()
    if raw_op_id:
        return _camel_case(raw_op_id)
    return _camel_case(f"{method}_{path}")


def _extract_json_schema(container: Any) -> dict[str, Any] | None:
    if not isinstance(container, dict):
        return None
    content = container.get("content")
    if not isinstance(content, dict):
        return None
    json_media = content.get("application/json")
    if isinstance(json_media, dict) and isinstance(json_media.get("schema"), dict):
        return json_media["schema"]
    for media in content.values():
        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
            return media["schema"]
    return None


def _extract_success_response_schema(op: dict[str, Any]) -> dict[str, Any] | None:
    responses = op.get("responses")
    if not isinstance(responses, dict):
        return None
    for code in ("200", "201", "202", "default"):
        if code in responses:
            return _extract_json_schema(responses[code])
    return None


def generate_ts_api_types(openapi_doc: dict[str, Any]) -> str:
    """FP07：从权威 ``openapi.json`` 确定性生成只读 ``frontend/src/generated/api-types.ts``。"""
    doc_for_check = {k: v for k, v in openapi_doc.items() if k != "servers"}
    oa_issues = validate_openapi(json.dumps(doc_for_check, ensure_ascii=False))
    if oa_issues:
        raise AppException(
            ErrorCode.OPENAPI_REJECTED,
            "无法生成 TS 契约类型，OpenAPI 校验失败：" + "；".join(oa_issues),
            status_code=422,
        )

    components = openapi_doc.get("components")
    raw_schemas = (
        components.get("schemas")
        if isinstance(components, dict) and isinstance(components.get("schemas"), dict)
        else {}
    )
    known_schemas = {
        (_pascal_case(name) if not _SAFE_TS_IDENT.fullmatch(name) else name)
        for name in raw_schemas
    }

    lines: list[str] = [
        "/* eslint-disable */",
        "/**",
        " * AUTO-GENERATED READ-ONLY FILE from authoritative backend/openapi.json (AgentForge FP07).",
        " * DO NOT MODIFY inside any frontend department staging directory.",
        " */",
        "",
    ]

    # 1. 组件 DTO 接口定义
    for raw_name in sorted(raw_schemas):
        schema = raw_schemas[raw_name]
        ts_name = _pascal_case(raw_name) if not _SAFE_TS_IDENT.fullmatch(raw_name) else raw_name
        if isinstance(schema, dict) and (
            schema.get("type") == "object" or isinstance(schema.get("properties"), dict)
        ):
            props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            req = set(schema.get("required") or [])
            lines.append(f"export interface {ts_name} {{")
            for prop_name in sorted(props):
                key = prop_name if _SAFE_TS_IDENT.fullmatch(prop_name) else json.dumps(prop_name)
                opt = "" if prop_name in req else "?"
                prop_ts = _schema_to_ts(props[prop_name], known_schemas=known_schemas)
                lines.append(f"  {key}{opt}: {prop_ts};")
            lines.append("}")
            lines.append("")
        else:
            aliased = _schema_to_ts(schema, known_schemas=known_schemas)
            lines.append(f"export type {ts_name} = {aliased};")
            lines.append("")

    # 2. 按路径/操作生成的请求参数、请求体与响应类型
    paths = openapi_doc.get("paths") or {}
    emitted_names: set[str] = set(known_schemas)
    used_op_keys: set[str] = set()
    op_type_entries: list[tuple[str, str, str, str, str, str, str]] = []

    def _reserve_name(base: str) -> str:
        cand = base
        suffix = 2
        while cand in emitted_names:
            cand = f"{base}{suffix}"
            suffix += 1
        emitted_names.add(cand)
        return cand

    for path_str in sorted(paths):
        path_item = paths[path_str]
        if not isinstance(path_item, dict):
            continue
        path_params = _PATH_PARAM_RE.findall(path_str)
        for method in _HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            op_key = _operation_key(method, path_str, op)
            while op_key in used_op_keys:
                op_key += "Op"
            used_op_keys.add(op_key)
            op_pascal = _pascal_case(op_key)

            # Path & Query 参数
            raw_params = op.get("parameters") if isinstance(op.get("parameters"), list) else []
            param_schema_map: dict[str, dict[str, Any]] = {}
            query_params: list[tuple[str, bool, str]] = []
            for p in raw_params:
                if not isinstance(p, dict):
                    continue
                p_name = str(p.get("name") or "").strip()
                p_in = str(p.get("in") or "").strip().lower()
                if not p_name:
                    continue
                if p_in == "path":
                    param_schema_map[p_name] = p.get("schema") if isinstance(p.get("schema"), dict) else {"type": "string"}
                elif p_in == "query":
                    p_ts = _schema_to_ts(p.get("schema") or {"type": "string"}, known_schemas=known_schemas)
                    query_params.append((p_name, bool(p.get("required", False)), p_ts))

            params_type = "undefined"
            if path_params:
                params_type = _reserve_name(f"{op_pascal}PathParams")
                lines.append(f"export interface {params_type} {{")
                for pname in path_params:
                    pts = _schema_to_ts(param_schema_map.get(pname, {"type": "string"}), known_schemas=known_schemas)
                    lines.append(f"  {pname}: {pts};")
                lines.append("}")
                lines.append("")

            query_type = "undefined"
            if query_params:
                query_type = _reserve_name(f"{op_pascal}QueryParams")
                lines.append(f"export interface {query_type} {{")
                for qname, qreq, qts in query_params:
                    key = qname if _SAFE_TS_IDENT.fullmatch(qname) else json.dumps(qname)
                    opt = "" if qreq else "?"
                    lines.append(f"  {key}{opt}: {qts};")
                lines.append("}")
                lines.append("")

            req_schema = _extract_json_schema(op.get("requestBody"))
            if req_schema is not None:
                req_ts = _schema_to_ts(req_schema, known_schemas=known_schemas)
                req_candidate = f"{op_pascal}Request"
                if req_ts == req_candidate and req_candidate in emitted_names:
                    req_type = req_candidate
                else:
                    req_type = _reserve_name(req_candidate)
                    lines.append(f"export type {req_type} = {req_ts};")
                    lines.append("")
            else:
                req_type = "undefined"

            resp_schema = _extract_success_response_schema(op)
            resp_ts = (
                _schema_to_ts(resp_schema, known_schemas=known_schemas)
                if resp_schema is not None
                else "Record<string, unknown>"
            )
            resp_candidate = f"{op_pascal}Response"
            if resp_ts == resp_candidate and resp_candidate in emitted_names:
                resp_type = resp_candidate
            else:
                resp_type = _reserve_name(resp_candidate)
                lines.append(f"export type {resp_type} = {resp_ts};")
                lines.append("")

            op_type_entries.append((op_key, method.upper(), path_str, params_type, query_type, req_type, resp_type))

    lines.append("export interface ApiOperationContracts {")
    for op_key, method_upper, path_str, params_type, query_type, req_type, resp_type in op_type_entries:
        lines.append(f"  {op_key}: {{")
        lines.append(f"    method: {json.dumps(method_upper)};")
        lines.append(f"    path: {json.dumps(path_str)};")
        lines.append(f"    params: {params_type};")
        lines.append(f"    query: {query_type};")
        lines.append(f"    request: {req_type};")
        lines.append(f"    response: {resp_type};")
        lines.append("  };")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def generate_ts_api_endpoints(openapi_doc: dict[str, Any]) -> str:
    """FP07：从权威 ``openapi.json`` 确定性生成只读 ``frontend/src/generated/api-endpoints.ts``。"""
    doc_for_check = {k: v for k, v in openapi_doc.items() if k != "servers"}
    oa_issues = validate_openapi(json.dumps(doc_for_check, ensure_ascii=False))
    if oa_issues:
        raise AppException(
            ErrorCode.OPENAPI_REJECTED,
            "无法生成 TS 端点清单，OpenAPI 校验失败：" + "；".join(oa_issues),
            status_code=422,
        )

    info = openapi_doc.get("info") if isinstance(openapi_doc.get("info"), dict) else {}
    paths = openapi_doc.get("paths") or {}

    imported_types: list[str] = ["ApiOperationContracts"]
    endpoint_defs: list[tuple[str, str, str, str, bool, list[str], bool, bool, str]] = []
    used_keys: set[str] = set()

    for path_str in sorted(paths):
        path_item = paths[path_str]
        if not isinstance(path_item, dict):
            continue
        path_params = _PATH_PARAM_RE.findall(path_str)
        for method in _HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            op_key = _operation_key(method, path_str, op)
            while op_key in used_keys:
                op_key += "Op"
            used_keys.add(op_key)

            dept = str(op.get("x-department") or "backend")
            auth_list = op.get("x-auth") if isinstance(op.get("x-auth"), list) else []
            requires_auth = "PUBLIC" not in {str(a).upper() for a in auth_list}
            has_body = _extract_json_schema(op.get("requestBody")) is not None
            raw_params = op.get("parameters") if isinstance(op.get("parameters"), list) else []
            has_query = any(
                isinstance(p, dict) and str(p.get("in") or "").lower() == "query"
                for p in raw_params
            )
            endpoint_defs.append(
                (op_key, method.upper(), path_str, dept, requires_auth, path_params, has_query, has_body, op_key)
            )

    lines: list[str] = [
        "/* eslint-disable */",
        "/**",
        " * AUTO-GENERATED READ-ONLY FILE from authoritative backend/openapi.json (AgentForge FP07).",
        " * All frontend departments MUST call backend APIs via these frozen definitions.",
        " */",
        "",
        "import { coreRequest } from '../core/http';",
        f"import type {{ {', '.join(imported_types)} }} from './api-types';",
        "",
        "export const AUTHORITATIVE_OPENAPI_META = {",
        f"  title: {json.dumps(str(info.get('title') or ''), ensure_ascii=False)},",
        f"  version: {json.dumps(str(info.get('version') or ''), ensure_ascii=False)},",
        f"  blueprintVersion: {int(openapi_doc.get('x-agentforge-blueprint-version') or 1)},",
        f"  blueprintHash: {json.dumps(str(openapi_doc.get('x-agentforge-blueprint-hash') or ''))},",
        f"  frozen: {'true' if openapi_doc.get('x-agentforge-frozen') is True else 'false'},",
        "} as const;",
        "",
        "export const API_ENDPOINTS = {",
    ]

    for op_key, method_upper, path_str, dept, requires_auth, *_ in endpoint_defs:
        lines.append(
            f"  {op_key}: {{ method: {json.dumps(method_upper)}, path: {json.dumps(path_str)}, "
            f"department: {json.dumps(dept)}, requiresAuth: {'true' if requires_auth else 'false'} }},"
        )
    lines.append("} as const;")
    lines.append("")
    lines.append("export type ApiEndpointKey = keyof typeof API_ENDPOINTS;")
    lines.append("")
    lines.append(
        "function buildPath(template: string, params?: Record<string, string | number>): string {"
    )
    lines.append("  if (!params) return template;")
    lines.append("  return template.replace(/\\{([A-Za-z_][A-Za-z0-9_]*)\\}/g, (_, key: string) =>")
    lines.append("    encodeURIComponent(String(params[key] ?? '')),")
    lines.append("  );")
    lines.append("}")
    lines.append("")
    lines.append(
        "function appendQuery(path: string, query?: Record<string, unknown>): string {"
    )
    lines.append("  if (!query) return path;")
    lines.append("  const search = new URLSearchParams();")
    lines.append("  for (const [k, v] of Object.entries(query)) {")
    lines.append("    if (v !== undefined && v !== null) search.append(k, String(v));")
    lines.append("  }")
    lines.append("  const qs = search.toString();")
    lines.append("  return qs ? `${path}?${qs}` : path;")
    lines.append("}")
    lines.append("")

    for op_key, method_upper, path_str, _dept, _req_auth, path_params, has_query, has_body, _ in endpoint_defs:
        sig_parts: list[str] = []
        if path_params:
            sig_parts.append(f"params: ApiOperationContracts[{json.dumps(op_key)}]['params']")
        if has_body:
            sig_parts.append(f"body: ApiOperationContracts[{json.dumps(op_key)}]['request']")
        if has_query:
            sig_parts.append(f"query?: ApiOperationContracts[{json.dumps(op_key)}]['query']")
        sig = ", ".join(sig_parts)
        lines.append(
            f"export function {op_key}({sig}): Promise<ApiOperationContracts[{json.dumps(op_key)}]['response']> {{"
        )
        path_expr = (
            f"buildPath({json.dumps(path_str)}, params as unknown as Record<string, string | number>)"
            if path_params
            else json.dumps(path_str)
        )
        if has_query:
            path_expr = f"appendQuery({path_expr}, query as Record<string, unknown> | undefined)"
        if has_body:
            lines.append(
                f"  return coreRequest<ApiOperationContracts[{json.dumps(op_key)}]['response']>({path_expr}, {{"
            )
            lines.append(f"    method: {json.dumps(method_upper)},")
            lines.append("    body: JSON.stringify(body),")
            lines.append("  });")
        else:
            lines.append(
                f"  return coreRequest<ApiOperationContracts[{json.dumps(op_key)}]['response']>({path_expr}, {{"
            )
            lines.append(f"    method: {json.dumps(method_upper)},")
            lines.append("  });")
        lines.append("}")
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 只读前端核心底座文件渲染（零额外 npm 依赖，100% 兼容模板锁定 package.json）
# --------------------------------------------------------------------------- #
def _render_core_http_ts() -> str:
    return (
        "/**\n"
        " * READ-ONLY factory frontend core HTTP layer (AgentForge FP07).\n"
        " * Wraps the template's in-memory token auth client with uniform request/response interceptors.\n"
        " */\n"
        "import { clearToken, getToken, request, setToken } from '../api/client';\n"
        "\n"
        "export type RequestInterceptor = (path: string, init: RequestInit) => RequestInit;\n"
        "export type ResponseErrorInterceptor = (error: Error) => void;\n"
        "\n"
        "const requestInterceptors: RequestInterceptor[] = [];\n"
        "const errorInterceptors: ResponseErrorInterceptor[] = [];\n"
        "\n"
        "export function addRequestInterceptor(fn: RequestInterceptor): void {\n"
        "  requestInterceptors.push(fn);\n"
        "}\n"
        "\n"
        "export function addErrorInterceptor(fn: ResponseErrorInterceptor): void {\n"
        "  errorInterceptors.push(fn);\n"
        "}\n"
        "\n"
        "export function normalizeApiPath(path: string): string {\n"
        "  return path.startsWith('/api/') ? path.slice(4) : path;\n"
        "}\n"
        "\n"
        "export async function coreRequest<T>(path: string, options: RequestInit = {}): Promise<T> {\n"
        "  let init: RequestInit = { ...options };\n"
        "  for (const interceptor of requestInterceptors) {\n"
        "    init = interceptor(path, init);\n"
        "  }\n"
        "  try {\n"
        "    return await request<T>(normalizeApiPath(path), init);\n"
        "  } catch (err) {\n"
        "    const normalized = err instanceof Error ? err : new Error(String(err));\n"
        "    for (const onError of errorInterceptors) {\n"
        "      onError(normalized);\n"
        "    }\n"
        "    throw normalized;\n"
        "  }\n"
        "}\n"
        "\n"
        "export { clearToken, getToken, setToken };\n"
    )


def _render_core_layout_ts(blueprint: ArchitectureBlueprint) -> str:
    items = [
        {"deptId": m.module_id, "label": m.name}
        for m in blueprint.modules
    ]
    return (
        "/**\n"
        " * READ-ONLY factory frontend layout & navigation descriptor (AgentForge FP07).\n"
        " */\n"
        "export interface FactoryNavSection {\n"
        "  readonly deptId: string;\n"
        "  readonly label: string;\n"
        "}\n"
        "\n"
        f"export const FACTORY_NAV_SECTIONS: readonly FactoryNavSection[] = {json.dumps(items, ensure_ascii=False, indent=2)} as const;\n"
    )


def _render_core_styles_css() -> str:
    return (
        "/* READ-ONLY factory frontend base design tokens (AgentForge FP07) */\n"
        ":root {\n"
        "  --af-bg: #f8fafc;\n"
        "  --af-surface: #ffffff;\n"
        "  --af-text: #0f172a;\n"
        "  --af-muted: #64748b;\n"
        "  --af-primary: #2563eb;\n"
        "  --af-border: #e2e8f0;\n"
        "  --af-radius: 8px;\n"
        "}\n"
        "\n"
        "body {\n"
        "  margin: 0;\n"
        "  background: var(--af-bg);\n"
        "  color: var(--af-text);\n"
        "  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;\n"
        "}\n"
    )


def _render_generated_routes_placeholder_ts() -> str:
    return (
        "/**\n"
        " * READ-ONLY declarative route aggregation entry (populated deterministically in FP08).\n"
        " */\n"
        "import type { RouteRecordRaw } from 'vue-router';\n"
        "\n"
        "export const generatedModuleRoutes: RouteRecordRaw[] = [];\n"
    )


def _copy_frontend_template(
    template_dir: Path,
    dest_root: Path,
    limits: AssemblyLimits,
) -> dict[str, str]:
    source_fe = template_dir / "frontend"
    if not source_fe.is_dir() or source_fe.is_symlink():
        raise AssemblyError(f"已认证模板缺少合法 frontend/ 目录：{template_dir}")
    if not (source_fe / "package.json").is_file():
        raise AssemblyError(f"已认证模板缺少 frontend/package.json：{template_dir}")

    categories: dict[str, str] = {}
    for path in sorted(source_fe.rglob("*")):
        rel_in_fe = path.relative_to(source_fe).as_posix()
        if any(part in _EXCLUDED_COPY for part in PurePosixPath(rel_in_fe).parts):
            continue
        if path.is_symlink():
            raise AssemblyError(f"前端模板包含符号链接，拒绝：{path}")
        if path.is_dir():
            continue

        rel = f"frontend/{rel_in_fe}"
        _validate_rel(rel)
        if rel in categories:
            raise AssemblyError(f"前端底座路径重复写入：{rel}")

        data = path.read_bytes()
        if len(data) > limits.max_file_bytes:
            raise AssemblyError(f"前端底座文件超过大小上限：{rel}")

        target = dest_root / rel
        if not _is_within(dest_root, target):
            raise AssemblyError(f"前端底座路径越界：{rel}")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        categories[rel] = "base_template"
        if len(categories) > limits.max_files:
            raise AssemblyError("前端底座文件数超过上限")

    return categories


def _resolve_template_release(
    materializer: AssetMaterializer,
    blueprint: ArchitectureBlueprint,
    lock: AssetLock | None,
) -> tuple[str, str]:
    if lock is not None:
        verify_lock_hash(lock)
        templates = [a for a in lock.assets if a.kind == "template"]
        if len(templates) != 1:
            raise AssemblyError(f"AssetLock 模板数量必须为 1，实际为 {len(templates)}")
        return templates[0].id, templates[0].version

    profile_id = blueprint.profile_ref.split("@", 1)[0].strip()
    releases = materializer.store.list_releases(kind="template")
    matched = [r for r in releases if profile_id and profile_id in r.profile_tags]
    if matched:
        # 优先选择 demo-0.2 等高版本 TS 模板
        chosen = max(matched, key=lambda r: r.version)
        return chosen.asset_id, chosen.version
    for r in releases:
        if r.asset_id == "base-template" and r.version == "demo-0.2":
            return r.asset_id, r.version
    if releases:
        chosen = max(releases, key=lambda r: (r.asset_id, r.version))
        return chosen.asset_id, chosen.version
    raise AppException(
        ErrorCode.ASSET_NOT_FOUND,
        "数据库中未找到任何已认证的前端基础模板（请先运行 seed_assets）",
        status_code=404,
    )


def _set_files_readonly(root: Path) -> None:
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        here = Path(dirpath)
        for fname in filenames:
            fpath = here / fname
            if not fpath.is_symlink():
                fpath.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def build_frontend_base(
    run_dir: str | Path,
    fe_blueprint: ArchitectureBlueprint,
    openapi_doc: dict[str, Any],
    materializer: AssetMaterializer,
    *,
    lock: AssetLock | None = None,
    openapi_sha256: str | None = None,
    state_store: FactoryStateStore | None = None,
    run_id: int | None = None,
    limits: AssemblyLimits | None = None,
) -> FrontendBaseBuildResult:
    """FP07 主入口：校验后端就绪与权威 OpenAPI，确定性搭建只读 Vue 3 + TS 前端底座与契约类型（0 模型调用）。"""
    if fe_blueprint.phase != FactoryPhase.FRONTEND.value:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"build_frontend_base 仅接受 phase='frontend' 蓝图，收到：{fe_blueprint.phase!r}",
            status_code=422,
        )

    actual_openapi_sha = require_backend_ready_for_frontend(
        openapi_doc,
        expected_openapi_sha256=openapi_sha256,
        state_store=state_store,
        run_id=run_id,
    )

    tpl_id, tpl_ver = _resolve_template_release(materializer, fe_blueprint, lock)
    template_ref = f"{tpl_id}@{tpl_ver}"
    template_dir = materializer.materialize_release("template", tpl_id, tpl_ver)
    effective_limits = limits or AssemblyLimits()

    run_root = Path(run_dir).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    final_dir = run_root / FRONTEND_BASE_DIRNAME
    tmp_dir = run_root / f".{FRONTEND_BASE_DIRNAME}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if tmp_dir.exists():
        remove_readonly_tree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        categories = _copy_frontend_template(template_dir, tmp_dir, effective_limits)

        generated_map: dict[str, tuple[str, str]] = {
            "frontend/src/core/http.ts": (_render_core_http_ts(), "base_common"),
            "frontend/src/core/layout.ts": (_render_core_layout_ts(fe_blueprint), "base_common"),
            "frontend/src/core/styles.css": (_render_core_styles_css(), "base_common"),
            GENERATED_ROUTES_REL: (_render_generated_routes_placeholder_ts(), "base_common"),
            GENERATED_TYPES_REL: (generate_ts_api_types(openapi_doc), "facade_stub"),
            GENERATED_ENDPOINTS_REL: (generate_ts_api_endpoints(openapi_doc), "facade_stub"),
        }
        generated_files: list[str] = []
        for rel, (content, cat) in generated_map.items():
            _validate_rel(rel)
            if rel in categories:
                raise AssemblyError(f"前端生成文件与模板基座冲突：{rel}")
            target = tmp_dir / rel
            if not _is_within(tmp_dir, target):
                raise AssemblyError(f"前端生成文件路径越界：{rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            categories[rel] = cat
            generated_files.append(rel)

        entries: list[BaseManifestEntry] = []
        for rel in sorted(categories):
            data = (tmp_dir / rel).read_bytes()
            entries.append(
                BaseManifestEntry(
                    path=rel,
                    sha256=_sha256_bytes(data),
                    size=len(data),
                    category=categories[rel],
                    protected=True,
                )
            )

        manifest_hash = _compute_manifest_hash(entries)
        protected_prefixes = sorted(
            {p for p in PROTECTED_PREFIXES if p.startswith("frontend/")}
            | set(FRONTEND_PROTECTED_PREFIXES)
        )
        bp_hash = fe_blueprint.blueprint_hash()

        manifest = BaseManifest(
            blueprint_hash=bp_hash,
            blueprint_version=fe_blueprint.blueprint_version,
            lock_hash=lock.lock_hash if lock is not None else actual_openapi_sha,
            template_ref=template_ref,
            base_package=fe_blueprint.base_package,
            protected_prefixes=protected_prefixes,
            facade_stub_paths=[GENERATED_TYPES_REL, GENERATED_ENDPOINTS_REL],
            shared_dto_paths=[],
            manifest_hash=manifest_hash,
            files=entries,
        )
        (tmp_dir / READONLY_MANIFEST_NAME).write_text(
            json.dumps(
                manifest.model_dump(mode="json"),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        _set_files_readonly(tmp_dir)
        if final_dir.exists():
            remove_readonly_tree(final_dir)
        os.replace(tmp_dir, final_dir)

        return FrontendBaseBuildResult(
            base_dir=str(final_dir),
            frontend_project_dir=str(final_dir / "frontend"),
            manifest_path=str(final_dir / READONLY_MANIFEST_NAME),
            blueprint_hash=bp_hash,
            openapi_sha256=actual_openapi_sha,
            template_ref=template_ref,
            manifest_hash=manifest_hash,
            generated_files=sorted(generated_files),
            protected_prefixes=protected_prefixes,
            files=entries,
        )
    except Exception:
        if tmp_dir.exists():
            remove_readonly_tree(tmp_dir)
        raise


__all__ = [
    "FRONTEND_BASE_DIRNAME",
    "FRONTEND_PROTECTED_PREFIXES",
    "GENERATED_ENDPOINTS_REL",
    "GENERATED_ROUTES_REL",
    "GENERATED_TYPES_REL",
    "FrontendBaseBuildResult",
    "build_frontend_base",
    "canonical_openapi_sha256",
    "generate_ts_api_endpoints",
    "generate_ts_api_types",
    "require_backend_ready_for_frontend",
    "validate_frontend_blueprint",
]
