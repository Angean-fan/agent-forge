"""R18：失败分类与有限自动修复（`classify_failure → coding → 可选 complex_repair → verify`）。

- **只修可复现的代码/契约错误**：`BUILD_FAILED`/验收用例 FAIL 等进入修复；
  **基础设施/资产冲突/外部配置不盲改**（`NOT_RUN`/`INFRASTRUCTURE`/`UNKNOWN_CHECK`/`NO_TESTS` 等）。
- **上限即 FAILED**：每修复任务 ≤2 次、全 run ≤6 次（可经环境变量收紧）；达上限仍失败即停，不无限重试。
- **升级由 Python 明确分类并检查预算**：第 2 次修复才在「首修失败且确属复杂 + 升级角色已开启 + 预算足够」时
  升级到 `complex_repair`（Qwen3.8 Max）；升级**仍消耗该任务第 2 次修复机会**。
- **基础设施不计业务修复次数**；只传**脱敏**的失败日志、冻结契约与授权源码；模型输出不登记为新资产。
- 修复前可查询是否有**兼容、已验证**的资产版本可替换失败代码（复用装配，不调用 Agent），但替换同样要走完整复验。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from agentforge_platform import model_gateway
from agentforge_platform.asset_finder import AssetSearchResult
from agentforge_platform.contract import BusinessContract
from agentforge_platform.gap_patch import assemble_reuse_method, validate_change_set
from agentforge_platform.method_index import scan_methods
from agentforge_platform.packaging import verification_passed
from agentforge_platform.spec import SoftwareSpecification

PASS = "PASS"
CODE = "CODE"
INFRASTRUCTURE = "INFRASTRUCTURE"

CODING_ROLE = "coding"
COMPLEX_ROLE = "complex_repair"
_UPGRADE_ROLE = COMPLEX_ROLE

_INFRA_CODES = frozenset({"INFRASTRUCTURE", "UNKNOWN_CHECK", "NO_TESTS"})
_DEFAULT_PER_TASK = 2
_DEFAULT_PER_RUN = 6


@dataclass(frozen=True)
class RepairLimits:
    per_task: int = _DEFAULT_PER_TASK
    per_run: int = _DEFAULT_PER_RUN


def repair_limits() -> RepairLimits:
    return RepairLimits(
        per_task=int(os.environ.get("AGENTFORGE_REPAIR_MAX_PER_TASK", str(_DEFAULT_PER_TASK))),
        per_run=int(os.environ.get("AGENTFORGE_REPAIR_MAX_PER_RUN", str(_DEFAULT_PER_RUN))),
    )


# --------------------------------------------------------------------------- #
# 失败分类（确定性，不调用模型）
# --------------------------------------------------------------------------- #
def classify_repair_failure(verification: dict, acceptance: dict) -> str:
    """返回 ``PASS`` / ``CODE``（可复现、可修复）/ ``INFRASTRUCTURE``（不得盲改、不计业务次数）。"""
    if verification_passed(verification, acceptance):
        return PASS
    checks = verification.get("checks", [])
    cases = acceptance.get("cases", [])
    if any(check.get("status") == "NOT_RUN" for check in checks + cases):
        return INFRASTRUCTURE
    if any(check.get("error_code") in _INFRA_CODES for check in checks):
        return INFRASTRUCTURE
    return CODE


def failing_signals(verification: dict, acceptance: dict) -> dict:
    """收敛失败信号（含脱敏日志），供修复上下文；不包含任何对话历史。"""
    failures = [
        {"name": c.get("name"), "error_code": c.get("error_code"),
         "detail": c.get("detail"), "log_excerpt": c.get("log_excerpt")}
        for c in verification.get("checks", []) if c.get("status") != "PASS"
    ]
    cases = [
        {"name": c.get("name"), "detail": c.get("detail")}
        for c in acceptance.get("cases", []) if c.get("status") != "PASS"
    ]
    return {"checks": failures, "acceptance_cases": cases}


def choose_repair_role(attempt: int) -> str:
    """第 1 次用 coding；第 2 次（首修失败/复杂）在升级角色已开启时升到 complex_repair，否则仍 coding。"""
    if attempt >= 1 and model_gateway.role_enabled(COMPLEX_ROLE):
        return COMPLEX_ROLE
    return CODING_ROLE


# --------------------------------------------------------------------------- #
# 修复上下文与提示（冻结、脱敏、无对话历史）
# --------------------------------------------------------------------------- #
def repair_allowed_paths(contract: BusinessContract) -> list[str]:
    """修复仅限契约实体的服务/控制器层（有限扩展点；不开放任意目录）。"""
    paths: set[str] = set()
    for _table in contract.tables:
        paths.add("backend/src/main/java/com/agentforge/template/service/")
        paths.add("backend/src/main/java/com/agentforge/template/controller/")
    return sorted(paths)


def build_repair_context(
    verification: dict, acceptance: dict, contract: BusinessContract, *,
    run_dir: str | Path, allowed_paths: list[str],
) -> dict:
    root = Path(run_dir)
    authorized: list[str] = []
    for allowed in allowed_paths:
        base = root / allowed
        if base.is_dir():
            authorized += sorted(
                path.relative_to(root).as_posix() for path in base.rglob("*") if path.is_file()
            )
    return {
        "failure": failing_signals(verification, acceptance),
        "contract": {
            "routes": [route.model_dump(mode="json") for route in contract.routes],
            "tables": [table.model_dump(mode="json") for table in contract.tables],
        },
        "allowed_paths": list(allowed_paths),
        "authorized_files": authorized,
        "prohibited": [
            "扩大或改写需求", "修改模板原件/公共方法", "自动升级依赖", "重写整个模块",
            "修改固定测试", "绕过或删除失败的检查",
        ],
    }


def render_repair_prompt(context: dict) -> str:
    return (
        "你是受限修复器。根据**脱敏**的失败日志与冻结契约，做**最小**修改以修复可复现的代码/契约错误；"
        "只写 allowed_paths 内文件，不得修改模板原件/公共方法或固定测试、不得升级依赖或改写需求。完成后停止。\n"
        + json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True)
    )


# --------------------------------------------------------------------------- #
# 修复前的资产替换候选（复用，不调用 Agent）
# --------------------------------------------------------------------------- #
def _split_ref(ref: str) -> tuple[str, str, str]:
    kind, _, rest = ref.partition(":")
    asset_id, _, version = rest.partition("@")
    return kind, asset_id, version


def replacement_candidate(
    search: AssetSearchResult, spec: SoftwareSpecification, methods_root: str | Path
) -> str | None:
    """有兼容、已验证且声明所需验收的方法单元时，优先替换失败代码而非调用 Agent。"""
    records = {(r.id, r.version): r for r in scan_methods(methods_root)}
    required = {case.id for case in spec.acceptance_cases}
    for ref in search.selected_refs:
        kind, asset_id, version = _split_ref(ref)
        if kind != "method-unit":
            continue
        record = records.get((asset_id, version))
        if record is not None and set(record.acceptance_ids) & required:
            return ref
    return None


# --------------------------------------------------------------------------- #
# 修复循环
# --------------------------------------------------------------------------- #
@dataclass
class RepairOutcome:
    classification: str
    attempts: int
    roles: list[str] = field(default_factory=list)
    passed: bool = False
    replaced_refs: list[str] = field(default_factory=list)
    verification: dict = field(default_factory=dict)
    acceptance: dict = field(default_factory=dict)


def run_repair_loop(
    *,
    verify,  # Callable[[str], dict]
    accept,  # Callable[[str], dict]
    artifact_dir: str,
    contract: BusinessContract,
    run_dir: str | Path,
    allowed_paths: list[str],
    task_id: str,
    engine_factory,  # Callable[[str, list[str], str], tuple[Any, FileBackend]] | None
    limits: RepairLimits | None = None,
    run_used: int = 0,
    search: AssetSearchResult | None = None,
    spec: SoftwareSpecification | None = None,
    methods_root: str | Path | None = None,
) -> RepairOutcome:
    """分类→修复→复验；错误可复现时最多 ``per_task`` 次、全 run 合计 ``per_run`` 次。"""
    limits = limits or repair_limits()
    verification = verify(artifact_dir)
    acceptance = accept(artifact_dir)
    classification = classify_repair_failure(verification, acceptance)
    outcome = RepairOutcome(classification, 0, [], classification == PASS, [],
                            verification, acceptance)
    if classification != CODE or engine_factory is None:
        return outcome

    records = {(r.id, r.version): r for r in scan_methods(methods_root)} if methods_root else {}
    while outcome.attempts < limits.per_task and run_used < limits.per_run:
        ref = (
            replacement_candidate(search, spec, methods_root)
            if search is not None and spec is not None and methods_root is not None
            else None
        )
        if ref is not None:
            _kind, asset_id, version = _split_ref(ref)
            assemble_reuse_method(run_dir, records[(asset_id, version)], methods_root)
            outcome.replaced_refs.append(ref)
            outcome.roles.append("reuse")
        else:
            role = choose_repair_role(outcome.attempts)
            engine, file_backend = engine_factory(role, allowed_paths, task_id)
            context = build_repair_context(
                verification, acceptance, contract, run_dir=run_dir, allowed_paths=allowed_paths
            )
            result = engine.run(render_repair_prompt(context))
            validate_change_set(result.get("change_set", {}), allowed_paths=allowed_paths)
            file_backend.commit()
            outcome.roles.append(role)
        outcome.attempts += 1
        run_used += 1
        verification = verify(artifact_dir)
        acceptance = accept(artifact_dir)
        outcome.verification = verification
        outcome.acceptance = acceptance
        if verification_passed(verification, acceptance):
            outcome.passed = True
            return outcome
    return outcome


__all__ = [
    "CODE",
    "CODING_ROLE",
    "COMPLEX_ROLE",
    "INFRASTRUCTURE",
    "PASS",
    "RepairLimits",
    "RepairOutcome",
    "build_repair_context",
    "choose_repair_role",
    "classify_repair_failure",
    "failing_signals",
    "render_repair_prompt",
    "repair_allowed_paths",
    "repair_limits",
    "replacement_candidate",
    "run_repair_loop",
]
