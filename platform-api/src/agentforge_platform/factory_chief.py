"""FP02：后端项目总管（``ChiefArchitect``）——模块拆分、SQL 号段与跨模块契约冻结。

核心职责：
1. 接收冻结 ``SoftwareSpecification``，先以 A05 ``validate_scope_for_mode(..., "factory")``
   拦截超 8 模块上限、高危请求与真实生产支付扣款；
2. 通过 A04 自定义路由（``chief_architect`` 角色）与 A07 ``PgBudgetLedger``（标明
   ``phase="backend", dept_id="chief", round=r``）调用强推理模型，产出严格符合
   :class:`ArchitectureBlueprint` Schema 的结构化 JSON；总管**零文件写入权限**；
3. 对模型输出执行 A06 ``validate_blueprint()`` 5 项硬校验 + 第三方沙箱安全红线校验；
   未通过时将确定性错误列表回喂给总管纠正，最多重试 ``max_retries`` 轮（默认 2 轮），
   超限终止并报 ``BLUEPRINT_INVALID``；
4. 校验通过后在 ``factory_blueprint`` 表中冻结持久化为 ``APPROVED_FOR_BUILD``；
5. 提供 ``triage_integration_failures(...)`` 占位接口供 FP06 补全。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform.blueprint_contracts import (
    MAX_MODULE_MIGRATION_VERSION,
    MIN_MODULE_MIGRATION_VERSION,
    ArchitectureBlueprint,
    blueprint_prompt_contract,
    validate_blueprint,
)
from agentforge_platform.budget_pg import PgBudgetLedger
from agentforge_platform.clients import ModelClient, ProviderPreflightError
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.external_requirements import scan_for_secrets
from agentforge_platform.factory_state import (
    BlueprintStatus,
    FactoryPhase,
    FactoryStateStore,
)
from agentforge_platform.factory_tickets import (
    MAX_DEFECT_LOG_BYTES,
    DefectTicket,
    sanitize_defect_log,
)
from agentforge_platform.model_gateway import Route, lookup_price, resolve_route
from agentforge_platform.models import Run
from agentforge_platform.production_routing import build_client_for_role
from agentforge_platform.scope_policy import (
    MODE_FACTORY,
    SANDBOX_CONSTRAINT_DECLARATION,
    detect_third_party_capabilities,
    factory_max_modules,
    validate_scope_for_mode,
)
from agentforge_platform.spec import SoftwareSpecification, TechnicalProfile
from agentforge_platform.spec_service import content_hash

CHIEF_ROLE = "chief_architect"
CHIEF_DEPT_ID = "chief"
DEFAULT_BASE_PACKAGE = "com.agentforge.template"
DEFAULT_MAX_CORRECTION_RETRIES = 2

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL | re.IGNORECASE)
_PROD_PAYMENT_BLUEPRINT_RE = re.compile(
    r"(真实扣款|真实资金扣款|生产商户私钥|真实微信商户密钥|真实支付宝私钥|"
    r"sk_live_[A-Za-z0-9]{8,}|rk_live_[A-Za-z0-9]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)


def _normalize_profile_ref(
    profile: TechnicalProfile | dict[str, Any] | str | None,
    spec: SoftwareSpecification,
) -> str:
    if isinstance(profile, str) and profile.strip():
        return profile.strip()
    if isinstance(profile, TechnicalProfile):
        return f"{profile.profile_id}@{profile.profile_version}"
    if isinstance(profile, dict):
        pid = str(profile.get("profile_id") or profile.get("id") or "").strip()
        pver = str(profile.get("profile_version") or profile.get("version") or "").strip()
        if pid and pver:
            return f"{pid}@{pver}"
        if pid:
            return pid
    tp = spec.technical_profile
    return f"{tp.profile_id}@{tp.profile_version}"


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if not text:
        raise ValueError("模型返回为空，无法解析 ArchitectureBlueprint JSON")

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
        raise TypeError("ArchitectureBlueprint 顶层必须为 JSON 对象")
    return parsed


def _blueprint_safety_issues(
    blueprint: ArchitectureBlueprint,
    *,
    raw_content: str,
    detected_third_party: list[tuple[str, str]],
) -> list[str]:
    """校验蓝图不含真实生产扣款/密钥，且对所有第三方能力强制声明沙箱/Mock 接入。"""
    issues: list[str] = []

    serialized = json.dumps(blueprint.model_dump(mode="json"), ensure_ascii=False)
    combined = f"{raw_content}\n{serialized}"

    prod_match = _PROD_PAYMENT_BLUEPRINT_RE.search(combined)
    if prod_match:
        issues.append(
            f"安全红线拒绝：蓝图禁止包含真实生产支付扣款或私钥（命中：{prod_match.group(0)!r}）"
        )

    secret_hits = scan_for_secrets(combined)
    if secret_hits:
        issues.append("安全红线拒绝：蓝图包含疑似真实密钥（" + "；".join(secret_hits) + "）")

    for idx, entry in enumerate(blueprint.sandbox_integrations):
        if not isinstance(entry, dict):
            issues.append(f"sandbox_integrations[{idx}] 必须为对象")
            continue
        entry_id = str(entry.get("id") or entry.get("kind") or f"entry-{idx}")
        if entry.get("sandbox_only") is not True:
            issues.append(
                f"安全红线拒绝：第三方集成 {entry_id} 必须声明 sandbox_only=true（禁止生产真实接入）"
            )
        if entry.get("real_payment_allowed") is True:
            issues.append(
                f"安全红线拒绝：第三方集成 {entry_id} 禁止设置 real_payment_allowed=true"
            )
        mode = str(entry.get("mode") or "").strip().lower()
        if mode in {"live", "production", "real_payment"}:
            issues.append(
                f"安全红线拒绝：第三方集成 {entry_id} 禁止使用生产模式 mode={mode!r}"
            )

    if detected_third_party:
        declared_keys: set[str] = set()
        for entry in blueprint.sandbox_integrations:
            if isinstance(entry, dict) and entry.get("sandbox_only") is True:
                for k in ("id", "kind", "pack_id"):
                    val = str(entry.get(k) or "").strip().lower()
                    if val:
                        declared_keys.add(val)
        for pack_id, kind in detected_third_party:
            if pack_id.lower() not in declared_keys and kind.lower() not in declared_keys:
                issues.append(
                    f"蓝图缺少第三方能力「{kind}（{pack_id}）」的沙箱声明"
                    "（必须在 sandbox_integrations 中声明 sandbox_only=true 与 Port+Adapter+Mock）"
                )

    return issues


def _build_backend_blueprint_prompt(
    *,
    spec: SoftwareSpecification,
    spec_hash: str,
    project_id: int,
    profile_ref: str,
    base_package: str,
    blueprint_version: int,
    max_modules: int,
    sandbox_integrations: list[dict[str, Any]],
    previous_errors: list[str] | None = None,
) -> str:
    spec_payload = json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, indent=2)
    sandbox_payload = json.dumps(sandbox_integrations, ensure_ascii=False, indent=2)

    lines = [
        "你是 AgentForge 异步工厂流水线的【后端项目总管 ChiefArchitect】。",
        "你的唯一职责是产出冻结架构蓝图 JSON（ArchitectureBlueprint），你不编写任何业务源码，也不拥有文件写入权限。",
        "",
        "【硬性架构规则（全部由 Python 确定性校验）】",
        f"1. 业务部门数量必须 <= {max_modules}，每个部门拥有互不重叠的 owned_paths、非空 test_paths 与 owned_package。",
        "2. 禁止任何部门将只读底座或跨模块 api/*Facade.java 目录放入可写 owned_paths（业务实现放在 modules/<dept_id>/internal/ 或 controller/service/mapper/entity/dto）。",
        f"3. 每个后端部门必须分配互不重叠的 Flyway SQL 号段 migration_range=[lo, hi]，范围严格在 [{MIN_MODULE_MIGRATION_VERSION}, {MAX_MODULE_MIGRATION_VERSION}]（如 [100,199]、[200,299]…，避开底座 V001..V009 与总装 V900+）；如需 SQL 迁移文件，请把具体文件路径（如 backend/src/main/resources/db/migration/V100__init_<dept>.sql）加入该部门 owned_paths，严禁把整个 migration 目录分给多个部门。",
        "4. 所有跨模块 JAVA_METHOD（api/*Facade）与对外契约必须在 frozen_contracts 中冻结为 status='APPROVED_FOR_BUILD'，签名必须精确（'<ReturnType> <methodName>(<Args>)'），严禁出现 TODO / * / ? / unknown；凡消费他部门契约必须在 readable_contract_refs 与 depends_on_modules 中同时显式声明，且依赖图必须无环。",
        "5. 蓝图必须通过 covered_entities 与 covered_acceptance_ids 覆盖冻结规格中的全部业务实体与验收场景。",
        f"6. {SANDBOX_CONSTRAINT_DECLARATION}",
        "",
        "【固定元信息】",
        f"- project_id: \"{project_id}\"",
        "- phase: \"backend\"",
        f"- blueprint_version: {blueprint_version}",
        f"- based_on_spec_hash: \"{spec_hash}\"",
        f"- profile_ref: \"{profile_ref}\"",
        f"- base_package: \"{base_package}\"",
        "",
        "【强制沙箱第三方能力清单】",
        sandbox_payload,
        "",
        "【冻结业务规格 SoftwareSpecification】",
        spec_payload,
    ]

    if previous_errors:
        lines.extend(
            [
                "",
                "【上一轮确定性校验失败错误列表——请逐条修复并重新输出完整合法 JSON】",
                *[f"- {err}" for err in previous_errors],
            ]
        )

    lines.extend(
        [
            "",
            "【ArchitectureBlueprint 字段契约——由代码生成，键名必须逐字匹配】",
            blueprint_prompt_contract(),
            "",
            "请仅输出一个严格符合上述字段契约的合法 JSON 对象（不要输出多余解释文本）。",
        ]
    )
    return "\n".join(lines)


def _sanitize_failure_report(report: dict[str, Any]) -> dict[str, Any]:
    """对总验收失败报告做脱敏与长度控制，防止密钥或超长日志直接进入总管上下文。"""
    sanitized: dict[str, Any] = {}
    for key, val in report.items():
        if isinstance(val, str):
            sanitized[key] = sanitize_defect_log(val, max_bytes=MAX_DEFECT_LOG_BYTES)
        elif isinstance(val, list):
            cleaned_list: list[Any] = []
            for item in val[:20]:
                if isinstance(item, dict):
                    cleaned_list.append(_sanitize_failure_report(item))
                elif isinstance(item, str):
                    cleaned_list.append(sanitize_defect_log(item, max_bytes=1024))
                else:
                    cleaned_list.append(item)
            sanitized[key] = cleaned_list
        elif isinstance(val, dict):
            sanitized[key] = _sanitize_failure_report(val)
        else:
            sanitized[key] = val
    return sanitized


class ChiefArchitect:
    """FP02：后端项目总管——只输出经硬校验并持久化冻结的 ``ArchitectureBlueprint``，零文件写权限。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        client: ModelClient | None = None,
        ledger: PgBudgetLedger | None = None,
        state_store: FactoryStateStore | None = None,
        role: str = CHIEF_ROLE,
        max_retries: int = DEFAULT_MAX_CORRECTION_RETRIES,
        base_package: str = DEFAULT_BASE_PACKAGE,
        reserve_micro: int | None = None,
    ) -> None:
        if max_retries < 0:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"max_retries 不能为负数：{max_retries}",
                status_code=422,
            )
        self._session_factory = session_factory
        self._client = client
        self._ledger = ledger or PgBudgetLedger(session_factory)
        self._state_store = state_store or FactoryStateStore(session_factory)
        self._role = role
        self._max_retries = max_retries
        self._base_package = base_package
        self._reserve_micro = reserve_micro

    def _resolve_run_project_id(self, run_id: int) -> int:
        with session_scope(self._session_factory) as db:
            run = db.get(Run, run_id)
            if run is None:
                raise AppException(
                    ErrorCode.RUN_NOT_FOUND,
                    f"run {run_id} 不存在",
                    status_code=404,
                )
            return int(run.project_id)

    def _resolve_route_and_client(self) -> tuple[Route, ModelClient]:
        route = resolve_route(self._role)
        client = self._client if self._client is not None else build_client_for_role(self._role)
        return route, client

    def _invoke_billed_model(
        self,
        *,
        project_id: int,
        run_id: int,
        route: Route,
        client: ModelClient,
        prompt: str,
        phase: FactoryPhase,
        round_num: int,
    ) -> str:
        call_id = f"factory:{run_id}:{phase.value}:{CHIEF_DEPT_ID}:r{round_num}:{uuid.uuid4().hex[:8]}"
        price = route.price or lookup_price(route.model)
        max_out = route.max_output_tokens or 8192
        max_in = max(1, len(prompt) // 2)

        self._ledger.reserve_atomic(
            call_id=call_id,
            project_id=project_id,
            run_id=run_id,
            model_role=self._role,
            provider=route.provider,
            model=route.model,
            reserve_micro=self._reserve_micro,
            max_input_tokens=max_in,
            max_output_tokens=max_out,
            dept_id=CHIEF_DEPT_ID,
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
                # 蓝图是一份体积固定的结构化 JSON：思考模式的 reasoning_content 同样计入
                # max_tokens，会把 JSON 截断（实测 deepseek-flash 8K/16K 均被截断，
                # 报 "Unterminated string"）。蓝图调用关闭思考，把额度全留给 JSON。
                thinking_mode="disabled",
            )
        except ProviderPreflightError as exc:
            self._ledger.release_atomic(call_id, reason="ProviderPreflightError")
            raise AppException(
                ErrorCode.CONFIG_REQUIRED,
                f"模型角色 {self._role} 前置配置不足：{exc}",
                status_code=422,
            ) from exc
        except Exception as exc:
            self._ledger.settle_idempotent(
                call_id,
                usage=None,
                error_class=type(exc).__name__,
            )
            raise AppException(
                ErrorCode.MODEL_UNAVAILABLE,
                f"项目总管调用模型失败（{type(exc).__name__}）",
                status_code=503,
                retryable=True,
            ) from exc

        self._ledger.settle_idempotent(
            call_id,
            usage=completion.usage,
            request_id=completion.request_id,
        )
        return completion.content

    def plan_backend_blueprint(
        self,
        spec: SoftwareSpecification,
        profile: TechnicalProfile | dict[str, Any] | str | None,
        run_id: int,
        *,
        blueprint_version: int = 1,
    ) -> ArchitectureBlueprint:
        """规划并冻结后端架构蓝图（通过 5 项硬校验后写入 ``factory_blueprint`` 为 ``APPROVED_FOR_BUILD``）。"""
        project_id = self._resolve_run_project_id(run_id)
        self._state_store.raise_if_cancelled(run_id, boundary="chief_backend_preflight")

        # 1. 前置硬拒绝：在独立副本上运行 A05 工厂范围与安全策略校验（绝不篡改冻结 spec 原件及其哈希）
        scope_result = validate_scope_for_mode(spec.model_copy(deep=True), mode=MODE_FACTORY)
        if not scope_result.ok:
            joined = "；".join(scope_result.issues)
            is_security_block = any(
                "安全红线拦截" in msg or "安全与架构边界拦截" in msg
                for msg in scope_result.issues
            )
            code = ErrorCode.VALIDATION_ERROR if is_security_block else ErrorCode.UNSUPPORTED_SCOPE
            raise AppException(code, joined, status_code=422)

        route, client = self._resolve_route_and_client()
        spec_hash = content_hash(spec)
        profile_ref = _normalize_profile_ref(profile, spec)
        max_modules = factory_max_modules()
        detected_tp = detect_third_party_capabilities(spec)

        last_issues: list[str] = []
        total_attempts = 1 + self._max_retries

        for round_num in range(1, total_attempts + 1):
            self._state_store.raise_if_cancelled(
                run_id,
                boundary=f"chief_backend_round_{round_num}",
            )
            prompt = _build_backend_blueprint_prompt(
                spec=spec,
                spec_hash=spec_hash,
                project_id=project_id,
                profile_ref=profile_ref,
                base_package=self._base_package,
                blueprint_version=blueprint_version,
                max_modules=max_modules,
                sandbox_integrations=scope_result.sandbox_integrations,
                previous_errors=last_issues or None,
            )
            raw_content = self._invoke_billed_model(
                project_id=project_id,
                run_id=run_id,
                route=route,
                client=client,
                prompt=prompt,
                phase=FactoryPhase.BACKEND,
                round_num=round_num,
            )

            issues: list[str] = []
            blueprint: ArchitectureBlueprint | None = None
            try:
                payload = _extract_json_object(raw_content)
                payload.setdefault("phase", FactoryPhase.BACKEND.value)
                payload.setdefault("project_id", str(project_id))
                payload.setdefault("based_on_spec_hash", spec_hash)
                payload.setdefault("profile_ref", profile_ref)
                payload.setdefault("base_package", self._base_package)
                payload.setdefault("blueprint_version", blueprint_version)
                blueprint = ArchitectureBlueprint.model_validate(payload)
            except (ValueError, TypeError, ValidationError) as exc:
                issues.append(f"蓝图 JSON/Schema 解析失败：{exc}")

            if blueprint is not None:
                if blueprint.phase != FactoryPhase.BACKEND.value:
                    issues.append(
                        f"后端总管蓝图 phase 必须为 'backend'，实际为 {blueprint.phase!r}"
                    )
                issues.extend(validate_blueprint(blueprint, spec))
                issues.extend(
                    _blueprint_safety_issues(
                        blueprint,
                        raw_content=raw_content,
                        detected_third_party=detected_tp,
                    )
                )

            if blueprint is not None and not issues:
                canonical_json = json.dumps(
                    blueprint.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                self._state_store.save_blueprint(
                    run_id,
                    FactoryPhase.BACKEND,
                    version=blueprint_version,
                    blueprint_json=canonical_json,
                    blueprint_hash=blueprint.blueprint_hash(),
                    status=BlueprintStatus.APPROVED_FOR_BUILD,
                )
                return blueprint

            last_issues = issues
            self._state_store.record_event(
                run_id,
                "factory_blueprint_retry",
                {
                    "phase": FactoryPhase.BACKEND.value,
                    "round": round_num,
                    "max_attempts": total_attempts,
                    "issue_count": len(issues),
                    "issues": [item[:240] for item in issues[:10]],
                },
            )

        self._state_store.record_event(
            run_id,
            "factory_blueprint_invalid",
            {
                "phase": FactoryPhase.BACKEND.value,
                "attempts": total_attempts,
                "issue_count": len(last_issues),
                "issues": [item[:240] for item in last_issues[:10]],
            },
        )
        raise AppException(
            ErrorCode.BLUEPRINT_INVALID,
            f"后端架构蓝图经 {total_attempts} 轮规划仍未通过硬校验：" + "；".join(last_issues),
            status_code=422,
        )

    def plan_frontend_blueprint(
        self,
        spec: SoftwareSpecification,
        openapi_doc: dict[str, Any],
        profile: TechnicalProfile | dict[str, Any] | str | None,
        run_id: int,
        *,
        openapi_sha256: str | None = None,
        blueprint_version: int = 1,
    ) -> ArchitectureBlueprint:
        """FP07：前端项目总管——以权威冻结 ``openapi.json`` 与冻结规格为唯一输入，规划并冻结前端多部门蓝图。"""
        from agentforge_platform.factory_frontend_base import (
            FRONTEND_PROTECTED_PREFIXES,
            require_backend_ready_for_frontend,
            validate_frontend_blueprint,
        )

        project_id = self._resolve_run_project_id(run_id)
        self._state_store.raise_if_cancelled(run_id, boundary="chief_frontend_preflight")

        # 1. 硬门禁：必须校验后端阶段 PASSED 且权威 openapi.json 已冻结且哈希有效
        verified_oa_sha = require_backend_ready_for_frontend(
            openapi_doc,
            expected_openapi_sha256=openapi_sha256,
            state_store=self._state_store,
            run_id=run_id,
        )

        # 2. 前置工厂范围与安全红线检查（在副本上执行，不修改冻结 spec）
        scope_result = validate_scope_for_mode(spec.model_copy(deep=True), mode=MODE_FACTORY)
        if not scope_result.ok:
            joined = "；".join(scope_result.issues)
            is_security_block = any(
                "安全红线拦截" in msg or "安全与架构边界拦截" in msg
                for msg in scope_result.issues
            )
            code = ErrorCode.VALIDATION_ERROR if is_security_block else ErrorCode.UNSUPPORTED_SCOPE
            raise AppException(code, joined, status_code=422)

        route, client = self._resolve_route_and_client()
        spec_hash = content_hash(spec)
        profile_ref = _normalize_profile_ref(profile, spec)
        max_modules = factory_max_modules()
        detected_tp = detect_third_party_capabilities(spec)

        last_issues: list[str] = []
        total_attempts = 1 + self._max_retries

        for round_num in range(1, total_attempts + 1):
            self._state_store.raise_if_cancelled(
                run_id,
                boundary=f"chief_frontend_round_{round_num}",
            )
            prompt_lines = [
                "你是 AgentForge 异步工厂流水线的【前端项目总管 ChiefArchitect】。",
                "你的唯一职责是基于后端已冻结权威 openapi.json 与冻结需求，产出前端架构蓝图 JSON（ArchitectureBlueprint, phase='frontend'）；你零文件写入权限，不写前端源码。",
                "",
                "【前端架构硬性规则】",
                f"1. 前端业务部门数量必须 <= {max_modules}，module_id 仅允许小写字母/数字/连字符/下划线（如 auth-ui、home-ui、cart-ui、order-ui、payment-ui）。",
                "2. 每个前端部门独占 frontend/src/modules/<dept_id>/ 与非空 frontend/test/<dept_id>/，彼此零交集。",
                f"3. 严禁将只读受保护路径纳入 owned_paths：{json.dumps(list(FRONTEND_PROTECTED_PREFIXES), ensure_ascii=False)}。",
                "4. 各前端部门页面路由前缀不可冲突且禁止侵占只读底座保留路由 '/' 与 '/login'；若在 frozen_contracts 中声明后端 /api/* 端点，(http_method, http_path) 必须严格存在于权威 openapi.json。",
                "5. 必须通过 covered_pages 覆盖冻结规格中的全部业务页面（使用 spec.pages 中的精确 page.name）。",
                f"- project_id: \"{project_id}\"",
                "- phase: \"frontend\"",
                f"- blueprint_version: {blueprint_version}",
                f"- based_on_spec_hash: \"{spec_hash}\"",
                f"- profile_ref: \"{profile_ref}\"",
                f"- authoritative_openapi_sha256: \"{verified_oa_sha}\"",
                "",
                "【权威后端 openapi.json】",
                json.dumps(openapi_doc, ensure_ascii=False, indent=2),
                "",
                "【冻结业务规格 SoftwareSpecification】",
                json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, indent=2),
            ]
            if last_issues:
                prompt_lines.extend(
                    [
                        "",
                        "【上一轮确定性校验失败错误列表——请逐条修复】",
                        *[f"- {err}" for err in last_issues],
                    ]
                )
            prompt_lines.extend(
                [
                    "",
                    "【ArchitectureBlueprint 字段契约——由代码生成，键名必须逐字匹配】",
                    blueprint_prompt_contract(),
                    "",
                    "请仅输出一个严格符合上述字段契约的合法 JSON 对象。",
                ]
            )

            raw_content = self._invoke_billed_model(
                project_id=project_id,
                run_id=run_id,
                route=route,
                client=client,
                prompt="\n".join(prompt_lines),
                phase=FactoryPhase.FRONTEND,
                round_num=round_num,
            )

            issues: list[str] = []
            blueprint: ArchitectureBlueprint | None = None
            try:
                payload = _extract_json_object(raw_content)
                payload.setdefault("phase", FactoryPhase.FRONTEND.value)
                payload.setdefault("project_id", str(project_id))
                payload.setdefault("based_on_spec_hash", spec_hash)
                payload.setdefault("profile_ref", profile_ref)
                payload.setdefault("base_package", self._base_package)
                payload.setdefault("blueprint_version", blueprint_version)
                blueprint = ArchitectureBlueprint.model_validate(payload)
            except (ValueError, TypeError, ValidationError) as exc:
                issues.append(f"前端蓝图 JSON/Schema 解析失败：{exc}")

            if blueprint is not None:
                issues.extend(validate_frontend_blueprint(blueprint, spec, openapi_doc))
                issues.extend(
                    _blueprint_safety_issues(
                        blueprint,
                        raw_content=raw_content,
                        detected_third_party=detected_tp,
                    )
                )

            if blueprint is not None and not issues:
                canonical_json = json.dumps(
                    blueprint.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                self._state_store.save_blueprint(
                    run_id,
                    FactoryPhase.FRONTEND,
                    version=blueprint_version,
                    blueprint_json=canonical_json,
                    blueprint_hash=blueprint.blueprint_hash(),
                    status=BlueprintStatus.APPROVED_FOR_BUILD,
                )
                return blueprint

            last_issues = issues
            self._state_store.record_event(
                run_id,
                "factory_blueprint_retry",
                {
                    "phase": FactoryPhase.FRONTEND.value,
                    "round": round_num,
                    "max_attempts": total_attempts,
                    "issue_count": len(issues),
                    "issues": [item[:240] for item in issues[:10]],
                },
            )

        self._state_store.record_event(
            run_id,
            "factory_blueprint_invalid",
            {
                "phase": FactoryPhase.FRONTEND.value,
                "attempts": total_attempts,
                "issue_count": len(last_issues),
                "issues": [item[:240] for item in last_issues[:10]],
            },
        )
        raise AppException(
            ErrorCode.BLUEPRINT_INVALID,
            f"前端架构蓝图经 {total_attempts} 轮规划仍未通过硬校验：" + "；".join(last_issues),
            status_code=422,
        )

    def triage_integration_failures(
        self,
        *,
        run_id: int,
        phase: FactoryPhase | str,
        blueprint: ArchitectureBlueprint,
        failure_report: dict[str, Any],
        integration_round: int = 1,
    ) -> list[DefectTicket]:
        """FP06：总验收失败后的根因归因——将编译/迁移/HTTP 失败精确映射到责任部门并落库缺陷单。"""
        resolved_phase = FactoryPhase(phase)
        project_id = self._resolve_run_project_id(run_id)
        self._state_store.raise_if_cancelled(
            run_id,
            boundary=f"chief_triage_{resolved_phase.value}_r{integration_round}",
        )

        route, client = self._resolve_route_and_client()
        valid_dept_ids = {m.module_id for m in blueprint.modules}

        sanitized_report = _sanitize_failure_report(failure_report)
        dept_catalog = [
            {
                "dept_id": m.module_id,
                "name": m.name,
                "owned_paths": m.owned_paths,
                "migration_range": m.migration_range,
                "depends_on_modules": m.depends_on_modules,
                "readable_contract_refs": m.readable_contract_refs,
            }
            for m in blueprint.modules
        ]
        prompt = "\n".join(
            [
                "你是 AgentForge 异步工厂流水线的【项目总管 ChiefArchitect（总验收根因归因角色）】。",
                "你不直接编写业务源码，只负责将总装/Docker Runner 总验收失败堆栈精确归因到一个或多个责任部门 dept_id。",
                "严禁把未出错的部门拉入返工；必须输出严格 JSON：{\"defects\": [{\"dept_id\": \"...\", \"error_class\": \"...\", \"summary\": \"...\", \"affected_paths\": [...], \"failing_tests\": [...], \"log_excerpt\": \"...\"}]}。",
                f"- phase: {resolved_phase.value}",
                f"- integration_round: {integration_round}",
                f"- valid_dept_ids: {json.dumps(sorted(valid_dept_ids), ensure_ascii=False)}",
                f"- departments: {json.dumps(dept_catalog, ensure_ascii=False)}",
                f"- failure_report: {json.dumps(sanitized_report, ensure_ascii=False)}",
            ]
        )

        raw_content = self._invoke_billed_model(
            project_id=project_id,
            run_id=run_id,
            route=route,
            client=client,
            prompt=prompt,
            phase=resolved_phase,
            round_num=integration_round,
        )

        try:
            payload = _extract_json_object(raw_content)
        except (ValueError, TypeError) as exc:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"总管归因 JSON 解析失败：{exc}",
                status_code=422,
            ) from exc

        raw_defects = payload.get("defects")
        if not isinstance(raw_defects, list) or not raw_defects:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                "总管归因结果缺少非空的 defects 列表",
                status_code=422,
            )

        tickets: list[DefectTicket] = []
        for idx, item in enumerate(raw_defects):
            if not isinstance(item, dict):
                raise AppException(
                    ErrorCode.VALIDATION_ERROR,
                    f"总管归因 defects[{idx}] 必须为对象",
                    status_code=422,
                )
            dept_id = str(item.get("dept_id") or "").strip()
            if dept_id not in valid_dept_ids:
                raise AppException(
                    ErrorCode.VALIDATION_ERROR,
                    f"总管归因引用了未知部门 dept_id：{dept_id!r}（合法：{sorted(valid_dept_ids)}）",
                    status_code=422,
                )
            error_class = str(item.get("error_class") or "INTEGRATION_FAILURE").strip()[:64]
            summary = str(item.get("summary") or "总验收集成失败").strip()
            affected_paths = [
                str(p).strip() for p in (item.get("affected_paths") or []) if str(p).strip()
            ]
            failing_tests = [
                str(t).strip() for t in (item.get("failing_tests") or []) if str(t).strip()
            ]
            log_excerpt = sanitize_defect_log(
                str(item.get("log_excerpt") or summary),
                max_bytes=MAX_DEFECT_LOG_BYTES,
            )

            ticket = DefectTicket(
                ticket_id=f"integ-{run_id}-{resolved_phase.value}-{dept_id}-r{integration_round}-{idx + 1}",
                run_id=run_id,
                phase=resolved_phase.value,
                dept_id=dept_id,
                scope="integration",
                round=integration_round,
                error_class=error_class,
                summary=summary,
                failing_tests=failing_tests,
                affected_paths=affected_paths,
                log_excerpt=log_excerpt,
                verifier_kind="docker_runner",
            )
            row = self._state_store.open_defect_ticket(
                run_id,
                resolved_phase,
                dept_id,
                scope="integration",
                round_num=integration_round,
                error_class=ticket.error_class,
                payload=ticket.model_dump(mode="json"),
            )
            tickets.append(ticket.model_copy(update={"ticket_id": str(row.id)}))

        self._state_store.record_event(
            run_id,
            "factory_triage",
            {
                "phase": resolved_phase.value,
                "integration_round": integration_round,
                "blamed_depts": sorted({t.dept_id for t in tickets}),
                "ticket_count": len(tickets),
            },
        )
        return tickets


__all__ = [
    "CHIEF_DEPT_ID",
    "CHIEF_ROLE",
    "DEFAULT_BASE_PACKAGE",
    "DEFAULT_MAX_CORRECTION_RETRIES",
    "ChiefArchitect",
]
