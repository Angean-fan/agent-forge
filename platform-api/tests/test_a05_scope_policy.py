"""A05：工厂模式范围校验策略与第三方沙箱强制约定测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentforge_platform.asset_registry import REPO_ROOT
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.scope_policy import (
    MODE_DEMO_MVP,
    MODE_FACTORY,
    SANDBOX_CONSTRAINT_DECLARATION,
    ScopeLimits,
    default_scope_mode,
    enforce_sandbox_third_party,
    factory_max_modules,
    validate_scope_for_mode,
)
from agentforge_platform.spec import SoftwareSpecification, scope_issues

TASK_SPEC_PATH = REPO_ROOT / "schemas" / "task-management.spec.json"
CAMPUS_SPEC_PATH = REPO_ROOT / "schemas" / "campus-activity.spec.json"


def _make_spec(
    *,
    goal: str,
    entity_names: list[str],
    page_names: list[str] | None = None,
    rules: list[str] | None = None,
    integrations: list | None = None,
    capability_requests: list[dict] | None = None,
) -> SoftwareSpecification:
    base = json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8"))
    base["goal"] = goal
    base["entities"] = [
        {
            "name": name,
            "source": "user_confirmed",
            "fields": [
                {"name": "id", "type": "long", "required": True},
                {"name": "title", "type": "string", "required": True},
                {"name": "status", "type": "string", "required": True},
            ],
        }
        for name in entity_names
    ]
    base["pages"] = [
        {"name": pname, "entity": entity_names[idx % len(entity_names)]}
        for idx, pname in enumerate(page_names or [f"{n}Page" for n in entity_names])
    ]
    if rules is not None:
        base["business_rules"] = [
            {"id": f"BR{i + 1:02d}", "text": text, "source": "user_confirmed"}
            for i, text in enumerate(rules)
        ]
    base["integrations"] = integrations or []
    base["capability_requests"] = capability_requests or []
    return SoftwareSpecification.model_validate(base)


def test_ecommerce_5_modules_passes_factory_mode_and_locks_payment_to_sandbox() -> None:
    # 电商 5 部门需求：登录注册(UserAuth)、首页商品展示(Product)、购物车(CartItem)、创建订单(Order)、购买支付(Payment)
    ecommerce = _make_spec(
        goal="电商平台：支持用户登录注册、首页展示商品、加入购物车、创建订单与购买支付全流程",
        entity_names=["UserAccount", "Product", "CartItem", "PurchaseOrder", "PaymentRecord"],
        page_names=["登录注册页", "商品首页", "购物车页", "订单确认页", "购买支付收银台"],
        rules=[
            "登录用户可将上架商品加入购物车并创建订单",
            "用户发起购买支付后更新订单支付状态",
        ],
    )

    # 1. 在默认 demo_mvp 模式下被拒绝（超过 3 实体且含支付）
    mvp_res = validate_scope_for_mode(ecommerce, mode=MODE_DEMO_MVP)
    assert mvp_res.ok is False
    assert any("业务实体最多 3 个" in issue for issue in mvp_res.issues)
    assert any("不支持购买支付" in issue for issue in mvp_res.issues)

    # 2. 在 factory 模式下校验通过，且支付自动标记为 sandbox_only=True 与 Port+Adapter+Mock
    factory_res = validate_scope_for_mode(ecommerce, mode=MODE_FACTORY)
    assert factory_res.ok is True
    assert factory_res.issues == []
    assert factory_res.module_count == 5
    assert factory_res.sandbox_only is True
    assert len(factory_res.sandbox_integrations) >= 1

    payment_entries = [
        item
        for item in ecommerce.integrations
        if isinstance(item, dict) and item.get("kind") == "payment"
    ]
    assert len(payment_entries) == 1
    assert payment_entries[0]["sandbox_only"] is True
    assert payment_entries[0]["mode"] == "sandbox_mock"
    assert payment_entries[0]["pattern"] == "Port+Adapter+Mock"
    assert payment_entries[0]["real_payment_allowed"] is False
    assert SANDBOX_CONSTRAINT_DECLARATION in ecommerce.constraints
    assert any(req.get("sandbox_only") is True for req in ecommerce.external_requirements)


def test_10_modules_oversized_requirement_is_rejected_with_reduction_suggestions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENTFORGE_FACTORY_MAX_MODULES", raising=False)
    assert factory_max_modules() == 8

    ten_modules = [
        "Auth",
        "Product",
        "Cart",
        "Order",
        "Payment",
        "Coupon",
        "Logistics",
        "AfterSale",
        "Comment",
        "Analytics",
    ]
    oversized = _make_spec(
        goal="超大型 10 模块全渠道电商中台",
        entity_names=ten_modules,
    )

    res = validate_scope_for_mode(oversized, mode=MODE_FACTORY)
    assert res.ok is False
    assert res.module_count == 10
    assert any("最多允许 8 个模块" in issue for issue in res.issues)
    assert any("缩减建议" in issue for issue in res.issues)
    assert len(res.suggestions) >= 1
    assert "合并或裁剪至少 2 个" in res.suggestions[0]

    # 验证环境变量 AGENTFORGE_FACTORY_MAX_MODULES 可配置软上限
    monkeypatch.setenv("AGENTFORGE_FACTORY_MAX_MODULES", "10")
    assert ScopeLimits.for_mode(MODE_FACTORY).max_modules == 10
    res_expanded = validate_scope_for_mode(oversized, mode=MODE_FACTORY)
    assert res_expanded.ok is True


def test_factory_mode_blocks_real_payment_secrets_and_high_risk_requests() -> None:
    # 1. 企图传入生产商户私钥或要求真实资金扣款 -> 即使在 factory 模式也必须硬拦截
    for bad_rule in (
        "接入微信支付真实资金扣款，使用生产商户私钥签名",
        "配置 Stripe 生产密钥 sk_live_1234567890abcdef 完成扣费",
        "内置 -----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...",
    ):
        spec_bad_pay = _make_spec(
            goal="电商支付",
            entity_names=["Product", "PurchaseOrder"],
            rules=[bad_rule],
        )
        res = validate_scope_for_mode(spec_bad_pay, mode=MODE_FACTORY)
        assert res.ok is False
        assert any("安全红线拦截" in issue for issue in res.issues)

        with pytest.raises(AppException) as exc_info:
            enforce_sandbox_third_party(spec_bad_pay)
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    # 2. 企图显式将集成声明为 sandbox_only=False 或 mode="live" -> 硬拦截
    spec_live_flag = _make_spec(
        goal="电商支付",
        entity_names=["Product", "PurchaseOrder"],
        integrations=[{"id": "alipay", "kind": "payment", "sandbox_only": False, "mode": "live"}],
    )
    res_live = validate_scope_for_mode(spec_live_flag, mode=MODE_FACTORY)
    assert res_live.ok is False
    assert any("禁止启用生产/真实扣款模式" in issue for issue in res_live.issues)

    # 3. 高危超边界请求（任意公网爬虫、分布式微服务集群编排、宿主机命令执行）-> 硬拦截
    for dangerous_goal in (
        "提供全网爬取与公网爬虫绕过反爬抓取竞品价格",
        "基于 Kubernetes 集群与分布式微服务集群编排部署 20 个服务",
        "后台管理支持宿主机命令执行与任意 Shell 终端调试",
    ):
        spec_danger = _make_spec(
            goal=dangerous_goal,
            entity_names=["JobTask"],
        )
        res_danger = validate_scope_for_mode(spec_danger, mode=MODE_FACTORY)
        assert res_danger.ok is False
        assert any("安全与架构边界拦截" in issue for issue in res_danger.issues)


def test_demo_mvp_default_behavior_is_100_percent_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENTFORGE_SCOPE_MODE", raising=False)
    assert default_scope_mode() == MODE_DEMO_MVP

    for spec_path in (
        TASK_SPEC_PATH,
        CAMPUS_SPEC_PATH,
        REPO_ROOT / "schemas" / "book-lending.spec.json",
    ):
        spec = SoftwareSpecification.model_validate_json(
            Path(spec_path).read_text(encoding="utf-8")
        )
        assert scope_issues(spec) == []
        res = validate_scope_for_mode(spec)
        assert res.ok is True
        assert res.mode == MODE_DEMO_MVP
        assert res.issues == []
