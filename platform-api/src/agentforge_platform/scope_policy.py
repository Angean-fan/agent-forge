"""A05：工厂模式范围校验策略与第三方沙箱强制约定（``scope_policy.py``）。

按模式隔离范围校验：
1. ``demo_mvp``（默认模式）：100% 保持既有 R01—R27 严格范围（≤3 实体、≤2 角色、≤8 页面、禁支付），确保既有测试零改动通过；
2. ``factory``（工厂模式）：
   - 模块软上限默认 **≤ 8**（可经环境变量 ``AGENTFORGE_FACTORY_MAX_MODULES`` 配置），超出时拒绝并给出明确缩减建议；
   - 允许电商等需求携带“支付/短信/存储/回调”等第三方能力，但由 :func:`enforce_sandbox_third_party`
     **强制锁定为本地沙箱 / Mock 模拟模式**（``sandbox_only=True`` + ``Port/Adapter/Mock`` 契约约束）；
   - 硬禁止任何真实生产支付密钥/私钥或真实扣款要求进入 ``spec``，并拦截任意公网爬虫、分布式微服务编排、
     宿主机命令执行等明显超边界高危请求。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.external_requirements import scan_for_secrets
from agentforge_platform.spec import (
    MAX_BUSINESS_ENTITIES,
    MAX_BUSINESS_PAGES,
    MAX_BUSINESS_ROLES,
    SoftwareSpecification,
    business_roles,
    scope_issues,
)

ScopeMode = Literal["demo_mvp", "factory"]
MODE_DEMO_MVP: ScopeMode = "demo_mvp"
MODE_FACTORY: ScopeMode = "factory"

DEFAULT_FACTORY_MAX_MODULES = 8
DEFAULT_FACTORY_MAX_ENTITIES = 12
DEFAULT_FACTORY_MAX_PAGES = 24
DEFAULT_FACTORY_MAX_ROLES = 6

SANDBOX_CONSTRAINT_DECLARATION = (
    "THIRD_PARTY_SANDBOX_ONLY: 所有支付/短信/外部回调等第三方服务强制采用 "
    "Port + Adapter + 本地沙箱/Mock 模拟（sandbox_only=true），严禁接入真实生产扣款或商户私钥。"
)

_THIRD_PARTY_KEYWORDS: tuple[tuple[str, str, str], ...] = (
    ("payment-sandbox", "payment", r"(支付|付款|购买支付|收银台|微信支付|支付宝|退款|结账|payment|checkout|alipay|wechat[_ -]?pay|stripe)"),
    ("sms-sandbox", "sms", r"(短信|验证码发送|sms)"),
    ("oss-sandbox", "storage", r"(对象存储|云存储|oss|s3[_ -]?bucket)"),
    ("webhook-sandbox", "webhook", r"(外部回调|第三方回调|webhook)"),
)

_HIGH_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "任意公网爬虫/反爬绕过",
        re.compile(r"(公网爬虫|全网爬取|绕过反爬|任意网站抓取|批量爬虫|分布式爬虫)", re.IGNORECASE),
    ),
    (
        "分布式微服务/集群编排",
        re.compile(
            r"(分布式微服务|微服务集群编排|kubernetes\s*集群|k8s\s*编排|service\s*mesh|istio|spring\s*cloud\s*alibaba)",
            re.IGNORECASE,
        ),
    ),
    (
        "宿主机命令执行/远程Shell",
        re.compile(
            r"(宿主机命令执行|执行宿主机|任意\s*shell|远程代码执行|反弹\s*shell|root\s*终端|宿主机终端)",
            re.IGNORECASE,
        ),
    ),
)

_PROD_PAYMENT_PHRASE_RE = re.compile(
    r"(真实扣款|真实资金扣款|生产商户私钥|真实微信商户密钥|真实支付宝私钥)",
    re.IGNORECASE,
)

_PROD_PAYMENT_SECRET_RE = re.compile(
    r"(sk_live_[A-Za-z0-9]{8,}|rk_live_[A-Za-z0-9]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)

_PROD_PAYMENT_FORBIDDEN_RE = re.compile(
    r"(真实扣款|真实资金扣款|生产商户私钥|真实微信商户密钥|真实支付宝私钥|"
    r"sk_live_[A-Za-z0-9]{8,}|rk_live_[A-Za-z0-9]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)

_NEGATION_PREFIX_RE = re.compile(
    r"(?:严禁|禁止|不得|不允许|不支持|不做|不要|不接入|无需|避免|切勿|杜绝|不进行|非|无)[^，。；;\n!！]{0,16}$"
)


def _find_unnegated_prod_payment_match(corpus: str) -> re.Match[str] | None:
    """检测真实生产支付要求或私钥片段；对合规否定声明（如“严禁真实扣款”）不误拦截。"""
    secret_match = _PROD_PAYMENT_SECRET_RE.search(corpus)
    if secret_match is not None:
        return secret_match
    for match in _PROD_PAYMENT_PHRASE_RE.finditer(corpus):
        start = max(0, match.start() - 24)
        prefix = corpus[start : match.start()]
        if _NEGATION_PREFIX_RE.search(prefix):
            continue
        return match
    return None


def factory_max_modules() -> int:
    """读取工厂模式模块软上限（默认 8，可通过 ``AGENTFORGE_FACTORY_MAX_MODULES`` 配置）。"""
    raw = os.environ.get("AGENTFORGE_FACTORY_MAX_MODULES", "").strip()
    if not raw:
        return DEFAULT_FACTORY_MAX_MODULES
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_FACTORY_MAX_MODULES
    return max(1, val)


def default_scope_mode() -> ScopeMode:
    """默认范围模式固定为 ``demo_mvp``，保证既有 R01—R27 测试零改动通过。"""
    raw = os.environ.get("AGENTFORGE_SCOPE_MODE", "").strip().lower()
    if raw == MODE_FACTORY:
        return MODE_FACTORY
    return MODE_DEMO_MVP


@dataclass(frozen=True)
class ScopeLimits:
    """范围校验上限配置。"""

    max_modules: int = DEFAULT_FACTORY_MAX_MODULES
    max_entities: int = DEFAULT_FACTORY_MAX_ENTITIES
    max_pages: int = DEFAULT_FACTORY_MAX_PAGES
    max_roles: int = DEFAULT_FACTORY_MAX_ROLES
    allow_sandbox_integrations: bool = True

    @classmethod
    def for_mode(cls, mode: ScopeMode) -> ScopeLimits:
        if mode == MODE_DEMO_MVP:
            return cls(
                max_modules=MAX_BUSINESS_ENTITIES,
                max_entities=MAX_BUSINESS_ENTITIES,
                max_pages=MAX_BUSINESS_PAGES,
                max_roles=MAX_BUSINESS_ROLES,
                allow_sandbox_integrations=False,
            )
        max_mods = factory_max_modules()
        return cls(
            max_modules=max_mods,
            max_entities=max(DEFAULT_FACTORY_MAX_ENTITIES, max_mods),
            max_pages=max(DEFAULT_FACTORY_MAX_PAGES, max_mods * 3),
            max_roles=DEFAULT_FACTORY_MAX_ROLES,
            allow_sandbox_integrations=True,
        )


@dataclass
class ScopeValidationResult:
    """``validate_scope_for_mode`` 的结构化校验结果。"""

    ok: bool
    mode: ScopeMode
    limits: ScopeLimits
    modules: list[str]
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    sandbox_only: bool = False
    sandbox_integrations: list[dict[str, Any]] = field(default_factory=list)
    spec: SoftwareSpecification | None = None

    @property
    def module_count(self) -> int:
        return len(self.modules)

    def __bool__(self) -> bool:
        return self.ok


def _spec_corpus(spec: SoftwareSpecification) -> str:
    parts: list[str] = [spec.goal]
    parts.extend(e.name for e in spec.entities)
    parts.extend(p.name for p in spec.pages)
    parts.extend(r.text for r in spec.business_rules)
    parts.extend(f"{c.scenario} {c.expected}" for c in spec.acceptance_cases)
    parts.extend(spec.constraints)
    parts.extend(json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item) for item in spec.integrations)
    parts.extend(json.dumps(item, ensure_ascii=False) for item in spec.capability_requests)
    parts.extend(json.dumps(item, ensure_ascii=False) for item in spec.external_requirements)
    return "\n".join(p for p in parts if p)


def derive_candidate_modules(spec: SoftwareSpecification) -> list[str]:
    """从规格的显式模块声明、业务实体或独立功能域推导候选业务模块列表。"""
    explicit_modules: list[str] = []
    for req in spec.capability_requests:
        if isinstance(req, dict):
            mod_name = str(
                req.get("module") or req.get("dept_id") or req.get("id") or req.get("name") or ""
            ).strip()
            if mod_name and mod_name not in explicit_modules:
                explicit_modules.append(mod_name)

    entity_modules: list[str] = []
    for ent in spec.entities:
        name = ent.name.strip()
        if name and name not in entity_modules:
            entity_modules.append(name)

    page_modules: list[str] = []
    for page in spec.pages:
        key = (page.entity or page.name).strip()
        if key and key not in page_modules:
            page_modules.append(key)

    # 取最能反映真实模块划分的集合：若显式声明了 capability_requests 且数量更多则以其为准，否则以实体（或无实体时的页面域）为准
    candidates = explicit_modules if len(explicit_modules) >= len(entity_modules) and explicit_modules else entity_modules
    if not candidates and page_modules:
        candidates = page_modules
    return candidates


def detect_third_party_capabilities(spec: SoftwareSpecification) -> list[tuple[str, str]]:
    """检测规格中涉及的第三方能力 ``[(pack_id, capability_kind)]``（如支付、短信、存储、回调）。"""
    corpus = _spec_corpus(spec)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for pack_id, kind, pattern in _THIRD_PARTY_KEYWORDS:
        if re.search(pattern, corpus, re.IGNORECASE) and pack_id not in seen:
            seen.add(pack_id)
            found.append((pack_id, kind))
    for item in spec.integrations:
        if isinstance(item, dict):
            item_id = str(item.get("id") or item.get("provider") or "custom-integration").strip()
            item_kind = str(item.get("kind") or "third_party").strip()
        else:
            item_id = str(item).split("@", 1)[0].strip()
            item_kind = "third_party"
        if item_id and item_id not in seen:
            seen.add(item_id)
            found.append((item_id, item_kind))
    return found


def _security_and_boundary_issues(spec: SoftwareSpecification) -> list[str]:
    """检查高危超边界请求与真实生产支付私钥/凭证注入。"""
    corpus = _spec_corpus(spec)
    issues: list[str] = []

    for label, pattern in _HIGH_RISK_PATTERNS:
        match = pattern.search(corpus)
        if match:
            issues.append(f"安全与架构边界拦截：不支持高危超边界需求「{label}」（命中：{match.group(0)!r}）")

    prod_match = _find_unnegated_prod_payment_match(corpus)
    if prod_match:
        issues.append(
            f"安全红线拦截：严禁在规格中要求真实生产支付扣款或传入生产商户私钥（命中：{prod_match.group(0)!r}），"
            "第三方支付仅允许本地沙箱/Mock 模拟"
        )

    secret_hits = scan_for_secrets(corpus)
    if secret_hits:
        issues.append("安全红线拦截：规格中包含疑似真实密钥或私钥片段（" + "；".join(secret_hits) + "）")

    for item in (*spec.integrations, *spec.external_requirements):
        if isinstance(item, dict) and (
            item.get("sandbox_only") is False
            or str(item.get("mode") or "").lower() in {"live", "production", "real_payment"}
        ):
            issues.append(
                f"安全红线拦截：第三方集成 {item.get('id') or item.get('name') or 'unknown'} "
                "禁止启用生产/真实扣款模式（必须为 sandbox_only=True）"
            )
    return issues


def enforce_sandbox_third_party(spec: SoftwareSpecification) -> SoftwareSpecification:
    """强制将规格中的支付/短信/外部服务等第三方能力锁定为本地沙箱 / Mock 模拟模式。

    若检测到真实生产支付密钥或高危请求则直接抛 ``AppException(VALIDATION_ERROR)`` 拒绝。
    """
    sec_issues = _security_and_boundary_issues(spec)
    if sec_issues:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "；".join(sec_issues),
            status_code=422,
        )

    detected = detect_third_party_capabilities(spec)
    if not detected:
        return spec

    normalized_integrations: list[dict[str, Any] | str] = []
    seen_ids: set[str] = set()
    for item in spec.integrations:
        if isinstance(item, dict):
            entry = dict(item)
            entry_id = str(entry.get("id") or entry.get("provider") or "third-party-sandbox")
            entry["id"] = entry_id
            entry["sandbox_only"] = True
            entry["mode"] = "sandbox_mock"
            entry["pattern"] = "Port+Adapter+Mock"
            entry["real_payment_allowed"] = False
            normalized_integrations.append(entry)
            seen_ids.add(entry_id)
        else:
            entry_id = str(item).split("@", 1)[0].strip()
            normalized_integrations.append(
                {
                    "id": entry_id,
                    "ref": str(item),
                    "sandbox_only": True,
                    "mode": "sandbox_mock",
                    "pattern": "Port+Adapter+Mock",
                    "real_payment_allowed": False,
                }
            )
            seen_ids.add(entry_id)

    for pack_id, kind in detected:
        if pack_id not in seen_ids:
            normalized_integrations.append(
                {
                    "id": pack_id,
                    "kind": kind,
                    "sandbox_only": True,
                    "mode": "sandbox_mock",
                    "pattern": "Port+Adapter+Mock",
                    "real_payment_allowed": False,
                }
            )
            seen_ids.add(pack_id)

    spec.integrations = normalized_integrations

    normalized_reqs: list[dict[str, Any]] = []
    seen_req_ids: set[str] = set()
    for req in spec.external_requirements:
        req_copy = dict(req)
        req_id = str(req_copy.get("id") or req_copy.get("name") or "third-party")
        req_copy["sandbox_only"] = True
        req_copy["mode"] = "sandbox_mock"
        req_copy["pattern"] = "Port+Adapter+Mock"
        normalized_reqs.append(req_copy)
        seen_req_ids.add(req_id)

    for pack_id, kind in detected:
        if pack_id not in seen_req_ids:
            normalized_reqs.append(
                {
                    "id": pack_id,
                    "kind": kind,
                    "sandbox_only": True,
                    "mode": "sandbox_mock",
                    "pattern": "Port+Adapter+Mock",
                    "description": f"{kind} 第三方能力已强制锁定为本地沙箱/Mock 模拟（Port+Adapter+Mock），禁止真实扣款。",
                }
            )
            seen_req_ids.add(pack_id)

    spec.external_requirements = normalized_reqs

    if SANDBOX_CONSTRAINT_DECLARATION not in spec.constraints:
        spec.constraints.append(SANDBOX_CONSTRAINT_DECLARATION)

    return spec


def validate_scope_for_mode(
    spec: SoftwareSpecification,
    mode: ScopeMode | str | None = None,
    *,
    limits: ScopeLimits | None = None,
) -> ScopeValidationResult:
    """按指定模式（``demo_mvp`` 或 ``factory``）校验规格范围与第三方沙箱策略。"""
    resolved_mode_str = (mode or default_scope_mode()).strip().lower()
    if resolved_mode_str not in (MODE_DEMO_MVP, MODE_FACTORY):
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"未知的范围校验模式：{mode!r}（仅支持 'demo_mvp' 或 'factory'）",
            status_code=422,
        )
    resolved_mode: ScopeMode = (
        MODE_FACTORY if resolved_mode_str == MODE_FACTORY else MODE_DEMO_MVP
    )
    effective_limits = limits or ScopeLimits.for_mode(resolved_mode)
    modules = derive_candidate_modules(spec)

    # 1. demo_mvp 模式：100% 沿用既有 scope_issues，并拒绝支付类需求进入 MVP
    if resolved_mode == MODE_DEMO_MVP:
        issues = list(scope_issues(spec))
        issues.extend(_security_and_boundary_issues(spec))
        detected_tp = detect_third_party_capabilities(spec)
        if any(kind == "payment" for _, kind in detected_tp):
            issues.append(
                "Demo MVP 模式不支持购买支付/真实交易能力，请缩减支付需求或切换至 factory 模式使用本地沙箱模拟"
            )
        suggestions = (
            [
                (
                    f"当前为 demo_mvp 模式（上限：{effective_limits.max_entities} 实体 / "
                    f"{effective_limits.max_roles} 角色 / {effective_limits.max_pages} 页面），"
                    "请缩减范围或启用 factory 模式。"
                )
            ]
            if issues
            else []
        )
        return ScopeValidationResult(
            ok=not issues,
            mode=resolved_mode,
            limits=effective_limits,
            modules=modules,
            issues=issues,
            suggestions=suggestions,
            sandbox_only=False,
            sandbox_integrations=[],
            spec=spec,
        )

    # 2. factory 模式：校验安全边界、8 模块软上限，并强制第三方能力走 Port+Adapter+Mock 沙箱
    issues = _security_and_boundary_issues(spec)
    suggestions: list[str] = []

    if len(modules) > effective_limits.max_modules:
        excess = len(modules) - effective_limits.max_modules
        preview_keep = "、".join(modules[: effective_limits.max_modules])
        preview_trim = "、".join(modules[effective_limits.max_modules :])
        suggestion = (
            f"缩减建议：请将当前 {len(modules)} 个业务模块合并或裁剪至少 {excess} 个至 "
            f"≤ {effective_limits.max_modules} 个（例如优先保留核心链路 [{preview_keep}]，"
            f"延后或合并 [{preview_trim}]）。"
        )
        issues.append(
            f"工厂模式业务模块数超出上限：最多允许 {effective_limits.max_modules} 个模块，"
            f"当前识别到 {len(modules)} 个模块（{', '.join(modules)}）。{suggestion}"
        )
        suggestions.append(suggestion)

    if len(spec.entities) > effective_limits.max_entities:
        suggestion = (
            f"缩减建议：业务实体最多 {effective_limits.max_entities} 个（当前 {len(spec.entities)} 个），"
            "请合并附属子表或冗余实体。"
        )
        issues.append(
            f"工厂模式业务实体最多 {effective_limits.max_entities} 个，收到 {len(spec.entities)} 个。{suggestion}"
        )
        suggestions.append(suggestion)

    if len(spec.pages) > effective_limits.max_pages:
        suggestion = (
            f"缩减建议：业务页面最多 {effective_limits.max_pages} 个（当前 {len(spec.pages)} 个），"
            "请合并弹窗/详情页以控制在上限内。"
        )
        issues.append(
            f"工厂模式业务页面最多 {effective_limits.max_pages} 个，收到 {len(spec.pages)} 个。{suggestion}"
        )
        suggestions.append(suggestion)

    roles = business_roles(spec)
    if len(roles) > effective_limits.max_roles:
        suggestion = (
            f"缩减建议：业务角色最多 {effective_limits.max_roles} 个（当前 {len(roles)} 个），请合并近似权限角色。"
        )
        issues.append(
            f"工厂模式业务角色最多 {effective_limits.max_roles} 个，收到 {len(roles)} 个。{suggestion}"
        )
        suggestions.append(suggestion)

    sandbox_integrations: list[dict[str, Any]] = []
    if not issues:
        enforce_sandbox_third_party(spec)
        sandbox_integrations = [
            item for item in spec.integrations if isinstance(item, dict) and item.get("sandbox_only") is True
        ]

    return ScopeValidationResult(
        ok=not issues,
        mode=resolved_mode,
        limits=effective_limits,
        modules=modules,
        issues=issues,
        suggestions=suggestions,
        sandbox_only=bool(sandbox_integrations),
        sandbox_integrations=sandbox_integrations,
        spec=spec,
    )


__all__ = [
    "DEFAULT_FACTORY_MAX_ENTITIES",
    "DEFAULT_FACTORY_MAX_MODULES",
    "DEFAULT_FACTORY_MAX_PAGES",
    "DEFAULT_FACTORY_MAX_ROLES",
    "MODE_DEMO_MVP",
    "MODE_FACTORY",
    "SANDBOX_CONSTRAINT_DECLARATION",
    "ScopeLimits",
    "ScopeMode",
    "ScopeValidationResult",
    "default_scope_mode",
    "derive_candidate_modules",
    "detect_third_party_capabilities",
    "enforce_sandbox_third_party",
    "factory_max_modules",
    "validate_scope_for_mode",
]
