"""FP04：部门三角色引擎的严格 Pydantic 契约（``factory_tickets.py``）。

定义单部门内部流转的全部结构化数据对象（``extra="forbid"``）：
- :class:`DepartmentPlan`：``DeptLead`` 首次规划方案；
- :class:`FixDirective`：``DeptLead`` 收到缺陷单后的局部修复指令；
- :class:`DefectTicket`：失败后生成的脱敏缺陷单（``log_excerpt`` 强制脱敏并截断至 ≤ 4 KB）；
- :class:`TestEvidence`：由真实校验工具（``ModuleVerifier``）产出的不可伪造测试证据；
- :class:`DepartmentDeliverable`：部门运行完成后的结构化交付物。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from agentforge_platform.module_verifier import redact_and_truncate_log

_STRICT = ConfigDict(extra="forbid")

MAX_DEFECT_LOG_BYTES = 4096  # 硬上限：传递给 DeptLead 的缺陷日志脱敏且 <= 4 KB


def sanitize_defect_log(raw_log: str | None, *, max_bytes: int = MAX_DEFECT_LOG_BYTES) -> str:
    """对失败日志先脱敏（清除密钥/Token/DB 密码与无害噪音）、优先保留 Surefire 精确断言与报错段并按 UTF-8 字节数硬截断至 ``<= max_bytes``。"""
    if not raw_log:
        return ""
    if raw_log.lstrip().startswith("[surefire-failures]"):
        # Surefire 失败摘要已高度凝练且核心断言在最前方，保留头部优先，避免二次截断把 expected/but was 切掉
        redacted = redact_and_truncate_log(raw_log, limit=max_bytes)
        encoded = redacted.encode("utf-8")
        if len(encoded) <= max_bytes:
            return redacted
        marker = "\n...[truncated]"
        budget = max(0, max_bytes - len(marker.encode("utf-8")))
        head = encoded[:budget].decode("utf-8", errors="ignore").rstrip()
        result = f"{head}{marker}"
        while len(result.encode("utf-8")) > max_bytes and result:
            result = result[:-1]
        return result

    redacted = redact_and_truncate_log(raw_log, limit=max_bytes)
    encoded = redacted.encode("utf-8")
    if len(encoded) <= max_bytes:
        return redacted
    marker = "\n...[truncated]...\n"
    marker_bytes = marker.encode("utf-8")
    budget = max(0, max_bytes - len(marker_bytes))
    head_bytes = (budget * 4 // 5) if redacted.startswith("[error-focused excerpt]") else (budget * 2 // 3)
    tail_bytes = budget - head_bytes
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore") if tail_bytes > 0 else ""
    result = f"{head}{marker}{tail}"
    # 兜底确保字节数严格 <= max_bytes
    while len(result.encode("utf-8")) > max_bytes and result:
        result = result[:-1]
    return result


class PlannedFile(BaseModel):
    """副总管规划的单文件创建/复用项。"""

    model_config = _STRICT

    path: str
    purpose: str
    source: Literal["write_new", "reuse_asset"] = "write_new"
    asset_ref: str | None = None


class DepartmentPlan(BaseModel):
    """``DeptLead`` 首次规划产出（只读角色，不直接写源码）。"""

    model_config = _STRICT

    dept_id: str
    phase: Literal["backend", "frontend"] = "backend"
    round: int = 1
    summary: str
    files_to_create: list[PlannedFile] = []
    reused_assets: list[str] = []
    consumed_contracts: list[str] = []
    exposed_endpoints: list[str] = []
    test_focus: list[str] = []
    notes: list[str] = []


class FixDirective(BaseModel):
    """``DeptLead`` 收到脱敏 ``DefectTicket`` 后产出的局部返工指令。"""

    model_config = _STRICT

    dept_id: str
    phase: Literal["backend", "frontend"] = "backend"
    round: int
    ticket_id: str | int | None = None
    root_cause: str
    target_files: list[str]
    instructions: list[str]
    do_not_touch: list[str] = []
    test_adjustments: list[str] = []


class DefectTicket(BaseModel):
    """部门小循环或总装归因大循环产出的脱敏结构化缺陷单。"""

    model_config = _STRICT

    ticket_id: str
    run_id: int
    phase: Literal["backend", "frontend"]
    dept_id: str
    scope: Literal["department", "integration", "contract"] = "department"
    round: int
    error_class: str
    summary: str
    failing_tests: list[str] = []
    affected_paths: list[str] = []
    log_excerpt: str = ""
    verifier_kind: str | None = None
    exit_code: int | None = None
    resolved: bool = False

    @field_validator("log_excerpt")
    @classmethod
    def _sanitize_log(cls, value: str) -> str:
        return sanitize_defect_log(value, max_bytes=MAX_DEFECT_LOG_BYTES)


class TestEvidence(BaseModel):
    """真实编译与单测工具（``ModuleVerifier``）执行证据；是否通过完全由退出码与报告决定。"""

    __test__ = False
    model_config = _STRICT

    dept_id: str
    phase: Literal["backend", "frontend"]
    round: int
    verifier_kind: Literal["docker_runner", "local_process"]
    stack: Literal["java", "frontend"]
    test_selector: str
    status: Literal["PASS", "FAIL", "NOT_RUN"]
    executed: bool
    exit_code: int | None = None
    duration_ms: int = 0
    command: list[str] = []
    test_files: list[str] = []
    error_code: str | None = None
    log_excerpt: str = ""

    @field_validator("log_excerpt")
    @classmethod
    def _sanitize_evidence_log(cls, value: str) -> str:
        return sanitize_defect_log(value, max_bytes=MAX_DEFECT_LOG_BYTES)

    @property
    def passed(self) -> bool:
        """铁律：仅当工具真实执行、状态为 PASS 且退出码为 0 时才判定通过，绝不凭口头放行。"""
        return bool(self.executed and self.status == "PASS" and self.exit_code == 0)


class DepartmentDeliverable(BaseModel):
    """单部门小循环结束后的结构化交付包。"""

    model_config = _STRICT

    run_id: int
    phase: Literal["backend", "frontend"]
    dept_id: str
    status: Literal["PASSED", "FAILED", "CANCELLED"]
    rounds_used: int
    staging_dir: str
    plan: DepartmentPlan | None = None
    fix_directives: list[FixDirective] = []
    change_set: dict[str, Any] = {"changes": []}
    coder_changes: list[dict[str, Any]] = []
    tester_changes: list[dict[str, Any]] = []
    api_fragment_path: str | None = None
    api_fragment: dict[str, Any] = {}
    evidence: TestEvidence | None = None
    defect_tickets: list[DefectTicket] = []
    reused_assets: list[str] = []
    error_code: str | None = None


__all__ = [
    "MAX_DEFECT_LOG_BYTES",
    "DefectTicket",
    "DepartmentDeliverable",
    "DepartmentPlan",
    "FixDirective",
    "PlannedFile",
    "TestEvidence",
    "sanitize_defect_log",
]
