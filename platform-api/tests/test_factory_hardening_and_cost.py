"""针对工厂实测暴露的 7 个卡点与 Token 降费优化的回归测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agentforge_platform.asset_registry import REPO_ROOT
from agentforge_platform.blueprint_contracts import ArchitectureBlueprint
from agentforge_platform.budget_pg import PgBudgetLedger
from agentforge_platform.db import make_engine, make_session_factory, session_scope
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.factory_department import (
    DEPT_CODER_ROLE,
    DEPT_LEAD_ROLE,
    DEPT_TESTER_ROLE,
    DepartmentRunner,
    _build_coder_prompt,
    _build_lead_fix_prompt,
    _build_lead_plan_prompt,
    _build_tester_prompt,
    _derive_test_selector,
    _extract_failing_tests,
    _is_test_only_directive,
    _is_unit_test_failure,
)
from agentforge_platform.factory_scheduler import AsyncDepartmentPool
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
    FixDirective,
    sanitize_defect_log,
)
from agentforge_platform.models import Project, Run, RunEvent
from agentforge_platform.module_registry import Module
from agentforge_platform.module_verifier import (
    ModuleVerificationResult,
    extract_surefire_failure_summary,
    redact_and_truncate_log,
    validate_test_selector,
)
from agentforge_platform.patch_engine import (
    LedgerBinding,
    LedgerChatModel,
    compact_messages_for_llm,
)
from agentforge_platform.runs import create_run
from agentforge_platform.scope_policy import MODE_FACTORY, validate_scope_for_mode
from agentforge_platform.spec import SoftwareSpecification

CAMPUS_SPEC_PATH = REPO_ROOT / "schemas" / "campus-activity.spec.json"
ALEMBIC_INI = REPO_ROOT / "platform-api" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "platform-api" / "migrations"


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _make_db(tmp_path: Path, name: str = "hardening.db"):
    url = f"sqlite+pysqlite:///{tmp_path / name}"
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        project = Project(owner_id=1, name="homestay", requirement_text="民宿预订系统")
        db.add(project)
        db.flush()
        run = create_run(db, project, spec_id=1, spec_hash="a" * 64)
        project_id, run_id = project.id, run.id
    return factory, project_id, run_id


def test_scope_policy_allows_negated_real_payment_constraints_but_still_blocks_positive() -> None:
    base = json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8"))
    base["goal"] = "民宿预订系统，支付采用本地沙箱模拟，严禁真实扣款"
    base["business_rules"] = [
        {"id": "BR01", "text": "支付走本地沙箱模拟，严禁真实扣款与生产商户私钥。", "source": "user_confirmed"}
    ]
    base["constraints"] = ["THIRD_PARTY_SANDBOX_ONLY: 不接入真实资金扣款，仅本地 Mock"]
    spec_ok = SoftwareSpecification.model_validate(base)
    res_ok = validate_scope_for_mode(spec_ok, mode=MODE_FACTORY)
    assert res_ok.ok is True
    assert res_ok.sandbox_only is True

    # 正向要求真实扣款或泄露私钥必须仍被硬拦截
    base_bad = json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8"))
    base_bad["business_rules"] = [
        {"id": "BR01", "text": "接入微信支付真实扣款完成结账", "source": "user_confirmed"}
    ]
    res_bad = validate_scope_for_mode(SoftwareSpecification.model_validate(base_bad), mode=MODE_FACTORY)
    assert res_bad.ok is False
    assert any("安全红线拦截" in i for i in res_bad.issues)

    # 即使写了否定词，真实私钥前缀依然必须拦截
    base_secret = json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8"))
    base_secret["constraints"] = ["严禁使用 sk_live_1234567890abcdef"]
    res_secret = validate_scope_for_mode(SoftwareSpecification.model_validate(base_secret), mode=MODE_FACTORY)
    assert res_secret.ok is False


def test_derive_test_selector_stays_within_200_chars_for_many_test_classes(tmp_path: Path) -> None:
    dept_dir = tmp_path / "booking"
    base_pkg = "com.agentforge.template"
    rel_pkg = base_pkg.replace(".", "/")
    test_names = [
        "controller/BookingControllerTest.java",
        "domain/BookingStatusTest.java",
        "facade/BookingQueryFacadeImplTest.java",
        "facade/BookingStatusFacadeImplTest.java",
        "service/BookingApplicationServiceTest.java",
        "service/BookingPaymentServiceTest.java",
        "service/BookingReviewServiceTest.java",
        "service/BookingStatusServiceTest.java",
        "service/CurrentUserResolverTest.java",
    ]
    for rel in test_names:
        f = dept_dir / f"backend/src/test/java/{rel_pkg}/modules/booking/{rel}"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("class X {}", encoding="utf-8")

    mod = Module(
        project_id="1",
        module_id="booking",
        name="预订部",
        based_on_spec_hash="a" * 64,
        profile_ref="demo-ts-mp@0.4",
        owned_paths=[f"backend/src/main/java/{rel_pkg}/modules/booking/"],
        test_paths=[f"backend/src/test/java/{rel_pkg}/modules/booking/"],
        owned_package=f"{base_pkg}.modules.booking",
        migration_range=(200, 299),
    )
    bp = ArchitectureBlueprint(
        project_id="1",
        phase="backend",
        blueprint_version=1,
        based_on_spec_hash="a" * 64,
        profile_ref="demo-ts-mp@0.4",
        base_package=base_pkg,
        modules=[mod],
    )

    selector, files = _derive_test_selector(
        dept_staging=dept_dir,
        module=mod,
        blueprint=bp,
        stack="java",
        explicit_selector=None,
    )
    assert len(files) == 9
    assert len(selector) <= 200
    assert validate_test_selector("java", selector) == selector


def test_error_focused_log_truncation_strips_jansi_and_preserves_surefire_failure() -> None:
    filler = "\n".join(f"[INFO] Processing verbose maven step {i} " + ("x" * 60) for i in range(120))
    raw_log = (
        "[INFO] Scanning for projects...\n"
        f"{filler}\n"
        "[ERROR] Tests run: 7, Failures: 0, Errors: 1, Skipped: 0, Time elapsed: 0.1 s <<< FAILURE! -- in SandboxRefundFacadeImplTest\n"
        "[ERROR] com.agentforge.template.modules.payment.service.impl.SandboxRefundFacadeImplTest.refundByBooking_toleratesBookingStatusFailure -- Time elapsed: 0.003 s <<< ERROR!\n"
        "java.lang.NullPointerException: bookingStatusFacade was null\n"
        "  at SandboxRefundFacadeImpl.refund(SandboxRefundFacadeImpl.java:42)\n"
        f"{filler}\n"
        "java.lang.UnsatisfiedLinkError: /tmp/jansi-2.4.3-libjansi.so: failed to map segment from shared object\n"
        "OpenJDK 64-Bit Server VM warning: Sharing is only supported for boot loader classes because bootstrap classpath has been appended\n"
    )
    excerpt = redact_and_truncate_log(raw_log, limit=2000)
    assert "SandboxRefundFacadeImplTest" in excerpt
    assert "NullPointerException" in excerpt
    assert "libjansi.so" not in excerpt
    assert "Sharing is only supported" not in excerpt

    sanitized = sanitize_defect_log(raw_log, max_bytes=2000)
    assert len(sanitized.encode("utf-8")) <= 2000
    assert "SandboxRefundFacadeImplTest" in sanitized

    assert _is_unit_test_failure(excerpt) is True
    failing = _extract_failing_tests(excerpt)
    assert any("SandboxRefundFacadeImplTest" in item for item in failing)


def test_compact_messages_for_llm_shrinks_old_write_file_and_tool_outputs() -> None:
    huge_java = "public class A {\n" + ("    private String field = 'x';\n" * 150) + "}\n"
    huge_read = "line\n" * 400
    messages = [
        SystemMessage(content="sys prompt"),
        HumanMessage(content="initial task"),
        AIMessage(
            content="",
            tool_calls=[{"id": "c1", "name": "write_file", "args": {"file_path": "/a.java", "content": huge_java}}],
        ),
        ToolMessage(content=huge_read, tool_call_id="c1"),
        # 最近 6 条应完整保留
        AIMessage(
            content="",
            tool_calls=[{"id": "c2", "name": "write_file", "args": {"file_path": "/b.java", "content": huge_java}}],
        ),
        ToolMessage(content="ok", tool_call_id="c2"),
        AIMessage(content="step 3"),
        HumanMessage(content="step 4"),
        AIMessage(content="step 5"),
        ToolMessage(content="recent output", tool_call_id="c3"),
    ]
    compacted = compact_messages_for_llm(messages, keep_recent=6)
    assert len(compacted) == len(messages)
    # 早期 write_file content 被大幅压缩
    old_write_content = compacted[2].tool_calls[0]["args"]["content"]
    assert "compacted: already written to /a.java" in old_write_content
    assert len(old_write_content) < 200
    # 早期长 ToolMessage 被压缩
    assert "compacted earlier tool output" in compacted[3].content
    assert len(compacted[3].content) < 600
    # 最近 6 条原封不动
    assert compacted[4].tool_calls[0]["args"]["content"] == huge_java


def test_pg_budget_ledger_enforces_max_model_calls_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, project_id, run_id = _make_db(tmp_path)
    ledger = PgBudgetLedger(factory)
    monkeypatch.setenv("AGENTFORGE_MAX_MODEL_CALLS_PER_RUN", "2")
    monkeypatch.delenv("AGENTFORGE_FACTORY_MAX_MODEL_CALLS_PER_RUN", raising=False)

    for idx in range(2):
        ledger.reserve_atomic(
            call_id=f"call-{idx}",
            project_id=project_id,
            run_id=run_id,
            model_role="dept_coder",
            provider="deepseek_official",
            model="deepseek-chat",
            reserve_micro=100,
            dept_id="homestay",
            phase="backend",
            round_num=1,
        )

    with pytest.raises(AppException) as exc_info:
        ledger.reserve_atomic(
            call_id="call-2",
            project_id=project_id,
            run_id=run_id,
            model_role="dept_coder",
            provider="deepseek_official",
            model="deepseek-chat",
            reserve_micro=100,
            dept_id="homestay",
            phase="backend",
            round_num=1,
        )
    assert exc_info.value.code == ErrorCode.CALL_LIMIT_EXCEEDED
    assert exc_info.value.status_code == 429


def test_ledger_chat_model_aborts_immediately_on_cancel_requested_before_reserve(
    tmp_path: Path,
) -> None:
    factory, project_id, run_id = _make_db(tmp_path)
    with session_scope(factory) as db:
        run = db.get(Run, run_id)
        assert run is not None
        run.cancel_requested = True

    binding = LedgerBinding(
        session_factory=factory,
        project_id=project_id,
        run_id=run_id,
        task_id="backend:payment:tester:r1",
        model_role="dept_tester",
        dept_id="payment",
        phase="backend",
        round_num=1,
    )
    model = LedgerChatModel(
        ledger_binding=binding,
        model="deepseek-flash",
        api_key="dummy-never-called",
        base_url="http://127.0.0.1:1/v1",
    )
    with pytest.raises(FactoryCancelledError):
        model._generate([HumanMessage(content="hi")])


def test_async_pool_fail_fast_stops_queued_departments_when_one_fails(tmp_path: Path) -> None:
    factory, project_id, run_id = _make_db(tmp_path)
    store = FactoryStateStore(factory)

    def _mod(mid: str) -> Module:
        return Module(
            project_id=str(project_id),
            module_id=mid,
            name=mid,
            based_on_spec_hash="a" * 64,
            profile_ref="demo-ts-mp@0.4",
            owned_paths=[f"backend/src/main/java/com/agentforge/template/modules/{mid}/"],
            test_paths=[f"backend/src/test/java/com/agentforge/template/modules/{mid}/"],
        )

    bp = ArchitectureBlueprint(
        project_id=str(project_id),
        phase="backend",
        blueprint_version=1,
        based_on_spec_hash="a" * 64,
        profile_ref="demo-ts-mp@0.4",
        modules=[_mod("a"), _mod("b"), _mod("c")],
    )
    spec = SoftwareSpecification.model_validate(json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8")))

    started: list[str] = []

    class _DummyVerifier:
        verifier_kind = "docker_runner"

        def verify_module(self, **_kwargs: Any) -> ModuleVerificationResult:
            raise AssertionError("not called")

    async def _executor(rid, mod, _bp, _ver, _ticket):
        started.append(mod.module_id)
        if mod.module_id == "a":
            store.transition_department(rid, FactoryPhase.BACKEND, "a", DeptStatus.PLANNING)
            store.transition_department(rid, FactoryPhase.BACKEND, "a", DeptStatus.FAILED, error_code="BUILD_FAILED")
            return DepartmentDeliverable(
                run_id=rid,
                phase="backend",
                dept_id="a",
                status="FAILED",
                rounds_used=1,
                staging_dir=str(tmp_path / "a"),
                error_code="BUILD_FAILED",
            )
        await asyncio.sleep(0.05)
        store.transition_department(rid, FactoryPhase.BACKEND, mod.module_id, DeptStatus.PLANNING)
        store.transition_department(rid, FactoryPhase.BACKEND, mod.module_id, DeptStatus.PASSED)
        return DepartmentDeliverable(
            run_id=rid,
            phase="backend",
            dept_id=mod.module_id,
            status="PASSED",
            rounds_used=1,
            staging_dir=str(tmp_path / mod.module_id),
        )

    pool = AsyncDepartmentPool(
        session_factory=factory,
        verifier=_DummyVerifier(),  # type: ignore[arg-type]
        dept_executor=_executor,
        state_store=store,
        max_concurrent_depts_limit=1,
        fail_fast=True,
    )
    res = asyncio.run(
        pool.run_phase(
            run_id=run_id,
            blueprint=bp,
            spec=spec,
            run_dir=tmp_path,
            phase=FactoryPhase.BACKEND,
        )
    )
    assert res.ok is False
    assert res.status == "FAILED"
    assert res.failed_depts == ["a"]
    # b 和 c 排队等待信号量，因 fail_fast 立即跳过未执行
    assert started == ["a"]


def test_prompts_explicitly_constrain_sibling_boundaries_and_concise_tests() -> None:
    spec = SoftwareSpecification.model_validate(json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8")))
    m1 = Module(
        project_id="1",
        module_id="homestay",
        name="房源部",
        based_on_spec_hash="a" * 64,
        profile_ref="demo-ts-mp@0.4",
        owned_paths=["backend/src/main/java/com/agentforge/template/modules/homestay/"],
        test_paths=["backend/src/test/java/com/agentforge/template/modules/homestay/"],
    )
    m2 = Module(
        project_id="1",
        module_id="booking",
        name="预订部",
        based_on_spec_hash="a" * 64,
        profile_ref="demo-ts-mp@0.4",
        owned_paths=["backend/src/main/java/com/agentforge/template/modules/booking/"],
        test_paths=["backend/src/test/java/com/agentforge/template/modules/booking/"],
    )
    bp = ArchitectureBlueprint(
        project_id="1",
        phase="backend",
        blueprint_version=1,
        based_on_spec_hash="a" * 64,
        profile_ref="demo-ts-mp@0.4",
        modules=[m1, m2],
    )
    lead_prompt = _build_lead_plan_prompt(
        module=m1, blueprint=bp, spec=spec, phase=FactoryPhase.BACKEND, round_num=1
    )
    assert "sibling_modules" in lead_prompt
    assert "booking" in lead_prompt

    coder_prompt = _build_coder_prompt(
        module=m1, blueprint=bp, plan=None, directive=None, round_num=1
    )
    assert "兄弟部门" in coder_prompt and "booking" in coder_prompt

    from agentforge_platform.agent_files import FileChangeSet

    tester_prompt = _build_tester_prompt(
        module=m1, plan=None, directive=None, coder_changes=FileChangeSet(changes=()), round_num=1
    )
    assert "2~3" in tester_prompt

    from agentforge_platform.factory_pipeline import factory_backend_only_enabled
    from agentforge_platform.limits import factory_agent_recursion_limit

    assert factory_backend_only_enabled() is False
    assert factory_agent_recursion_limit("tester") < factory_agent_recursion_limit("coder")


def test_surefire_report_extraction_preserves_assertions_within_4kb_utf8_budget(tmp_path: Path) -> None:
    reports_dir = tmp_path / "backend" / "target" / "surefire-reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "com.agentforge.template.modules.payment.PaymentServiceSandboxPayTest.txt").write_text(
        (
            "-------------------------------------------------------------------------------\n"
            "Test set: com.agentforge.template.modules.payment.PaymentServiceSandboxPayTest\n"
            "-------------------------------------------------------------------------------\n"
            "Tests run: 5, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.42 s <<< FAILURE! -- in com.agentforge.template.modules.payment.PaymentServiceSandboxPayTest\n"
            "com.agentforge.template.modules.payment.PaymentServiceSandboxPayTest.sandboxPay_recordsPendingThenPaid -- Time elapsed: 0.08 s <<< FAILURE!\n"
            "org.opentest4j.AssertionFailedError: \n"
            'expected: "PENDING"\n'
            ' but was: "PAID"\n'
            "\tat org.junit.jupiter.api.AssertionFailureBuilder.build(AssertionFailureBuilder.java:151)\n"
            "\tat org.junit.jupiter.api.AssertEquals.assertEquals(AssertEquals.java:182)\n"
            "\tat com.agentforge.template.modules.payment.PaymentServiceSandboxPayTest.sandboxPay_recordsPendingThenPaid(PaymentServiceSandboxPayTest.java:88)\n"
            "\tat java.base/jdk.internal.reflect.NativeMethodAccessorImpl.invoke0(Native Method)\n"
        ),
        encoding="utf-8",
    )
    (reports_dir / "com.agentforge.template.modules.payment.PaymentControllerApiTest.txt").write_text(
        (
            "-------------------------------------------------------------------------------\n"
            "Test set: com.agentforge.template.modules.payment.PaymentControllerApiTest\n"
            "-------------------------------------------------------------------------------\n"
            "Tests run: 4, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.31 s <<< FAILURE! -- in com.agentforge.template.modules.payment.PaymentControllerApiTest\n"
            "com.agentforge.template.modules.payment.PaymentControllerApiTest.payOrder_rejectsNonOwner -- Time elapsed: 0.05 s <<< FAILURE!\n"
            "java.lang.AssertionError: Expecting code to raise a throwable.\n"
            "\tat org.assertj.core.api.ThrowableAssert.shouldHaveThrown(ThrowableAssert.java:61)\n"
            "\tat com.agentforge.template.modules.payment.PaymentControllerApiTest.payOrder_rejectsNonOwner(PaymentControllerApiTest.java:144)\n"
        ),
        encoding="utf-8",
    )
    # 通过的测试报告不应被混入失败摘要
    (reports_dir / "com.agentforge.template.modules.payment.PaymentStateMachineTest.txt").write_text(
        "Test set: PaymentStateMachineTest\nTests run: 6, Failures: 0, Errors: 0, Skipped: 0\n",
        encoding="utf-8",
    )

    summary = extract_surefire_failure_summary(tmp_path)
    assert summary.startswith("[surefire-failures]")
    assert 'expected: "PENDING"' in summary
    assert 'but was: "PAID"' in summary
    assert "PaymentServiceSandboxPayTest.java:88" in summary
    assert "Expecting code to raise a throwable." in summary
    assert "PaymentControllerApiTest.java:144" in summary
    assert "PaymentStateMachineTest" not in summary
    assert "NativeMethodAccessorImpl" not in summary

    # 即使拼接大量中文与 Maven Help 样板日志，sanitize_defect_log 也必须保住头部 Surefire 断言且 <= 4096 字节
    noisy_tail = "\n".join(
        [
            "[ERROR] To see the full debug logging of the errors, re-run Maven with the -e switch.",
            "[ERROR] Re-run Maven using the -X switch to enable full debug logging.",
            "[ERROR] For more information about the errors and possible solutions, please read the following articles:",
            "[ERROR] [Help 1] http://cwiki.apache.org/confluence/display/MAVEN/MojoFailureException",
            *[f"[INFO] 冗长中文构建日志输出第 {i} 行，用于测试多字节 UTF-8 截断保护" for i in range(120)],
        ]
    )
    sanitized = sanitize_defect_log(f"{summary}\n\n[maven-summary]\n{noisy_tail}", max_bytes=MAX_DEFECT_LOG_BYTES)
    assert len(sanitized.encode("utf-8")) <= MAX_DEFECT_LOG_BYTES
    assert 'expected: "PENDING"' in sanitized
    assert 'but was: "PAID"' in sanitized
    assert "PaymentControllerApiTest.java:144" in sanitized
    assert "[Help 1]" not in sanitized
    assert "Re-run Maven using the -X" not in sanitized


def test_rework_tester_prompt_inlines_failing_tests_ticket_and_forbids_duplicates(tmp_path: Path) -> None:
    from agentforge_platform.agent_files import FileChangeSet

    pkg = "com/agentforge/template/modules/payment"
    src_file = tmp_path / f"backend/src/main/java/{pkg}/service/PaymentService.java"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("public class PaymentService { void pay() {} }\n", encoding="utf-8")

    failing_test = tmp_path / f"backend/src/test/java/{pkg}/PaymentServiceSandboxPayTest.java"
    failing_test.parent.mkdir(parents=True, exist_ok=True)
    failing_test.write_text(
        "class PaymentServiceSandboxPayTest { void sandboxPay() { /* line 88 bug */ } }\n",
        encoding="utf-8",
    )
    passing_test = tmp_path / f"backend/src/test/java/{pkg}/PaymentStateMachineTest.java"
    passing_test.write_text("class PaymentStateMachineTest { void ok() {} }\n", encoding="utf-8")

    mod = Module(
        project_id="1",
        module_id="payment",
        name="支付部",
        based_on_spec_hash="a" * 64,
        profile_ref="demo-ts-mp@0.4",
        owned_paths=[f"backend/src/main/java/{pkg}/"],
        test_paths=[f"backend/src/test/java/{pkg}/"],
    )
    ticket = DefectTicket(
        ticket_id="ticket-14-payment-r1",
        run_id=14,
        phase="backend",
        dept_id="payment",
        round=1,
        error_class="UNIT_TEST_FAILURE",
        summary="单模块单测失败（PaymentServiceSandboxPayTest.sandboxPay）",
        failing_tests=["com.agentforge.template.modules.payment.PaymentServiceSandboxPayTest.sandboxPay"],
        affected_paths=[f"backend/src/test/java/{pkg}/PaymentServiceSandboxPayTest.java"],
        log_excerpt='[surefire-failures]\nexpected: "PENDING"\n but was: "PAID"\n\tat PaymentServiceSandboxPayTest.java:88',
    )
    directive = FixDirective(
        dept_id="payment",
        phase="backend",
        round=2,
        ticket_id="ticket-14-payment-r1",
        root_cause="ArgumentCaptor captured mutable entity after status changed to PAID",
        target_files=[f"backend/src/test/java/{pkg}/PaymentServiceSandboxPayTest.java"],
        instructions=["Capture status snapshot in doAnswer callback during insert"],
        test_adjustments=["Fix assertion at line 88 of PaymentServiceSandboxPayTest.java"],
    )

    prompt = _build_tester_prompt(
        module=mod,
        plan=None,
        directive=directive,
        coder_changes=FileChangeSet(changes=()),
        round_num=2,
        dept_staging=tmp_path,
        ticket=ticket,
    )
    assert "严禁新建重复测试文件" in prompt
    assert "edit_file" in prompt
    assert 'expected: "PENDING"' in prompt
    assert 'but was: "PAID"' in prompt
    assert "/* line 88 bug */" in prompt
    # 已通过测试不浪费 token 内联
    assert "class PaymentStateMachineTest" not in prompt
    # 即使本轮跳过 Coder（coder_changes 为空），也自动内联既有业务源码供测试参考
    assert "public class PaymentService" in prompt


def test_is_test_only_directive_and_runner_skips_dept_coder_on_test_only_rework(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult

    from agentforge_platform.clients import Completion, Usage

    pkg = "com/agentforge/template/modules/payment"
    mod = Module(
        project_id="1",
        module_id="payment",
        name="支付部",
        based_on_spec_hash="a" * 64,
        profile_ref="demo-ts-mp@0.4",
        owned_paths=[f"backend/src/main/java/{pkg}/"],
        test_paths=[f"backend/src/test/java/{pkg}/"],
    )

    # 1. 纯测试目标判断边界测试
    test_only_dir = FixDirective(
        dept_id="payment",
        phase="backend",
        round=2,
        root_cause="test captor bug",
        target_files=[f"/backend/src/test/java/{pkg}/PaymentServiceTest.java"],
        instructions=["edit existing test"],
        test_adjustments=["capture snapshot"],
    )
    assert _is_test_only_directive(test_only_dir, mod) is True

    mixed_dir = FixDirective(
        dept_id="payment",
        phase="backend",
        round=2,
        root_cause="both",
        target_files=[
            f"backend/src/main/java/{pkg}/PaymentService.java",
            f"backend/src/test/java/{pkg}/PaymentServiceTest.java",
        ],
        instructions=["fix both"],
    )
    assert _is_test_only_directive(mixed_dir, mod) is False

    empty_targets_test_adj = FixDirective(
        dept_id="payment",
        phase="backend",
        round=2,
        root_cause="test assertion bug",
        target_files=[],
        instructions=["update test"],
        test_adjustments=["fix expected status"],
    )
    unit_ticket = DefectTicket(
        ticket_id="t1",
        run_id=1,
        phase="backend",
        dept_id="payment",
        round=1,
        error_class="UNIT_TEST_FAILURE",
        summary="unit test failed",
    )
    assert _is_test_only_directive(empty_targets_test_adj, mod, unit_ticket) is True

    # 2. DeptLead 返工提示词包含 test_paths 与纯测试路由指引
    lead_fix_prompt = _build_lead_fix_prompt(
        module=mod, phase=FactoryPhase.BACKEND, round_num=2, ticket=unit_ticket
    )
    assert "test_paths" in lead_fix_prompt
    assert "跳过 DeptCoder" in lead_fix_prompt

    # 3. 完整 DepartmentRunner 端到端验证：Round 1 失败 → Round 2 纯测试返工彻底跳过 DeptCoder 并直达 TESTING → PASSED
    cfg = {
        "version": "test-v1",
        "currency": "CNY",
        "roles": {
            role: {
                "provider": "openai_compatible",
                "model": f"test-{role}",
                "base_url": "https://api.example.test/v1",
                "api_key_env": "TEST_KEY",
                "enabled": True,
                "max_output_tokens": 4096,
                "supports_tools": True,
                "price_micro": {
                    "input_micro_per_million": 1_000_000,
                    "output_micro_per_million": 2_000_000,
                },
            }
            for role in (DEPT_LEAD_ROLE, DEPT_CODER_ROLE, DEPT_TESTER_ROLE)
        },
    }
    cfg_path = tmp_path / "models.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("AGENTFORGE_MODELS_CONFIG", str(cfg_path))

    factory, project_id, run_id = _make_db(tmp_path, name="skip_coder.db")
    store = FactoryStateStore(factory)
    spec = SoftwareSpecification.model_validate(json.loads(CAMPUS_SPEC_PATH.read_text(encoding="utf-8")))
    bp = ArchitectureBlueprint(
        project_id=str(project_id),
        phase="backend",
        blueprint_version=1,
        based_on_spec_hash="a" * 64,
        profile_ref="demo-ts-mp@0.4",
        base_package="com.agentforge.template",
        modules=[mod],
    )

    class _LeadStub:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete(self, *, model: str, prompt: str, **_kwargs: Any) -> Completion:
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                body = json.dumps(
                    {
                        "dept_id": "payment",
                        "phase": "backend",
                        "round": 1,
                        "summary": "implement payment service",
                        "files_to_create": [
                            {"path": f"backend/src/main/java/{pkg}/PaymentService.java", "purpose": "svc"}
                        ],
                        "test_focus": ["PaymentServiceTest"],
                    }
                )
            else:
                body = json.dumps(
                    {
                        "dept_id": "payment",
                        "phase": "backend",
                        "round": 2,
                        "root_cause": "ArgumentCaptor captured mutable entity",
                        "target_files": [f"backend/src/test/java/{pkg}/PaymentServiceTest.java"],
                        "instructions": ["Edit PaymentServiceTest.java in-place to capture status snapshot"],
                        "test_adjustments": ["Fix line 12 assertion"],
                    }
                )
            return Completion(
                content=body,
                model=model,
                request_id=f"lead-{len(self.prompts)}",
                usage=Usage(input_tokens=100, output_tokens=80),
                finish_reason="stop",
            )

    from pydantic import Field

    class _ToolModelStub(BaseChatModel):
        responses: list[AIMessage]
        seen_prompts: list[str] = Field(default_factory=list)
        model_name: str = "test-hardening-model"

        @property
        def _llm_type(self) -> str:
            return "stub"

        def _get_ls_params(self, **kwargs: Any) -> dict[str, Any]:
            return {"ls_provider": "openai", "ls_model_name": self.model_name}

        def bind_tools(self, tools: Any, **kwargs: Any) -> _ToolModelStub:
            return self

        def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any):
            for m in messages:
                if isinstance(m, HumanMessage) and isinstance(m.content, str):
                    self.seen_prompts.append(m.content)
            msg = self.responses.pop(0) if self.responses else AIMessage(content="done")
            return ChatResult(generations=[ChatGeneration(message=msg)])

    coder_rounds: list[int] = []
    tester_stubs_by_round: dict[int, _ToolModelStub] = {}

    def _coder_factory(_binding: LedgerBinding, round_num: int) -> BaseChatModel:
        coder_rounds.append(round_num)
        return _ToolModelStub(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": f"c-r{round_num}",
                            "name": "write_file",
                            "args": {
                                "file_path": f"/backend/src/main/java/{pkg}/PaymentService.java",
                                "content": "public class PaymentService { String status = \"PAID\"; }\n",
                            },
                        }
                    ],
                ),
                AIMessage(content="coder done"),
            ]
        )

    def _tester_factory(_binding: LedgerBinding, round_num: int) -> BaseChatModel:
        if round_num == 1:
            responses = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "t-r1",
                            "name": "write_file",
                            "args": {
                                "file_path": f"/backend/src/test/java/{pkg}/PaymentServiceTest.java",
                                "content": 'class PaymentServiceTest { /* assert PENDING */ }\n',
                            },
                        }
                    ],
                ),
                AIMessage(content="tester r1 done"),
            ]
        else:
            responses = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "t-r2",
                            "name": "edit_file",
                            "args": {
                                "file_path": f"/backend/src/test/java/{pkg}/PaymentServiceTest.java",
                                "old_string": "/* assert PENDING */",
                                "new_string": "/* capture snapshot PENDING */",
                            },
                        }
                    ],
                ),
                AIMessage(content="tester r2 done"),
            ]
        stub = _ToolModelStub(responses=responses, seen_prompts=[])
        tester_stubs_by_round[round_num] = stub
        return stub

    class _VerifierStub:
        verifier_kind = "docker_runner"

        def __init__(self) -> None:
            self.calls = 0

        def verify_module(self, *, artifact_dir: str | Path, module_id: str, stack: str, test_selector: str, **_kwargs: Any) -> ModuleVerificationResult:
            self.calls += 1
            if self.calls == 1:
                # 模拟 Maven 落盘 surefire-reports/*.txt，而 stdout 只含简略报错
                sf = Path(artifact_dir) / "backend" / "target" / "surefire-reports"
                sf.mkdir(parents=True, exist_ok=True)
                (sf / "PaymentServiceTest.txt").write_text(
                    "Test set: PaymentServiceTest\n"
                    "Tests run: 1, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.1 s <<< FAILURE!\n"
                    "com.agentforge.template.modules.payment.PaymentServiceTest.pay -- Time elapsed: 0.1 s <<< FAILURE!\n"
                    'org.opentest4j.AssertionFailedError: expected: "PENDING" but was: "PAID"\n'
                    "\tat com.agentforge.template.modules.payment.PaymentServiceTest.pay(PaymentServiceTest.java:12)\n",
                    encoding="utf-8",
                )
                return ModuleVerificationResult(
                    artifact_dir=str(artifact_dir),
                    module_id=module_id,
                    stack="java",
                    test_selector=test_selector,
                    status="FAIL",
                    verifier_kind="docker_runner",
                    error_code="BUILD_FAILED",
                    exit_code=1,
                    log_excerpt="[ERROR] Failed to execute goal surefire:test",
                )
            return ModuleVerificationResult(
                artifact_dir=str(artifact_dir),
                module_id=module_id,
                stack="java",
                test_selector=test_selector,
                status="PASS",
                verifier_kind="docker_runner",
                exit_code=0,
                log_excerpt="Tests run: 1, Failures: 0, Errors: 0",
            )

    lead_stub = _LeadStub()
    verifier_stub = _VerifierStub()
    runner = DepartmentRunner(
        session_factory=factory,
        verifier=verifier_stub,  # type: ignore[arg-type]
        lead_client=lead_stub,  # type: ignore[arg-type]
        coder_model_factory=_coder_factory,
        tester_model_factory=_tester_factory,
        state_store=store,
    )
    deliverable = runner.run_department(
        run_id,
        mod,
        blueprint=bp,
        spec=spec,
        run_dir=tmp_path / "run",
        phase=FactoryPhase.BACKEND,
        max_rounds=3,
    )
    assert deliverable.status == "PASSED"
    assert deliverable.rounds_used == 2
    # 关键断言：Round 2 完全跳过了 DeptCoder，仅第 1 轮调用过 DeptCoder！
    assert coder_rounds == [1]
    assert verifier_stub.calls == 2
    # 关键断言：第 1 轮 DefectTicket 已从 surefire-reports/*.txt 自动提取精确断言并传给 Round 2 DeptLead 与 DeptTester
    assert len(deliverable.defect_tickets) == 1
    assert 'expected: "PENDING" but was: "PAID"' in deliverable.defect_tickets[0].log_excerpt
    assert 'expected: \\"PENDING\\" but was: \\"PAID\\"' in lead_stub.prompts[1]
    assert any("/* assert PENDING */" in p for p in tester_stubs_by_round[2].seen_prompts)
    assert any("严禁新建重复测试文件" in p for p in tester_stubs_by_round[2].seen_prompts)
    # 关键断言：RunEvent 中记录了 PLANNING -> TESTING (reason=test_only_rework_skip_coder)
    with session_scope(factory) as db:
        events = db.query(RunEvent).filter(RunEvent.run_id == run_id, RunEvent.type == "factory_dept_status").all()
        payloads = [json.loads(e.payload) for e in events]
    assert any(
        p.get("from") == "PLANNING"
        and p.get("to") == "TESTING"
        and p.get("reason") == "test_only_rework_skip_coder"
        for p in payloads
    )
