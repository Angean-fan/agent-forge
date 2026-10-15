"""F05：Module Coordinator（跨模块请求、接口变更、职责冲突协调）。

覆盖（**确定性、零模型调用**）：

- 结构化 change request：受理 → 派发（仅目标模块所有者）→ 发布登记 → 依赖方继续；
- **幂等重放**：同 `request_id` 返回既有结果、不新增；
- **业务范围变更须用户确认**（拒绝则终止/保留草稿）；
- 负例：自循环、未知模块、**循环依赖**、预算不足、**反复提议上限**、未派发即登记；
- **破坏性变更受影响消费者分析**；协调器不越权、无模型参与。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentforge_platform.errors import AppException
from agentforge_platform.module_coordinator import (
    KIND_SCOPE,
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_PENDING,
    STATUS_PENDING_USER,
    STATUS_REJECTED,
    ChangeRequest,
    ModuleCoordinator,
)
from agentforge_platform.module_registry import derive_modules
from agentforge_platform.spec import SoftwareSpecification

REPO = Path(__file__).resolve().parents[2]
CAMPUS = REPO / "schemas" / "campus-activity.spec.json"


def _coordinator() -> ModuleCoordinator:
    spec = SoftwareSpecification.model_validate_json(CAMPUS.read_text(encoding="utf-8"))
    return ModuleCoordinator(derive_modules(spec, project_id="p"))  # enrollment 依赖 activity


def _request(**overrides) -> ChangeRequest:
    payload = {
        "request_id": "req-1", "requester_module": "enrollment", "target_module": "activity",
        "capability_id": "activity.find_by_ids", "required_contract": "List<ActivityDTO> findByIds(List<Long>)",
        "change_reason": "报名需批量读取活动", "acceptance_ids": ["AC01"],
    }
    payload.update(overrides)
    return ChangeRequest(**payload)


# --- 全链路 -------------------------------------------------------------------

def test_full_request_flow_dispatches_to_owner_and_completes() -> None:
    coordinator = _coordinator()
    submitted = coordinator.submit(_request())
    assert submitted.status == STATUS_PENDING  # 可追踪

    assigned = coordinator.assign("req-1")
    assert assigned.status == STATUS_APPROVED and "activity" in assigned.note  # 仅目标模块所有者

    done = coordinator.complete("req-1", published_version=2)
    assert done.status == STATUS_DONE and done.published_version == 2  # 依赖方可在满足后继续
    assert [r.request_id for r in coordinator.all()] == ["req-1"]


def test_replay_same_request_id_is_idempotent() -> None:
    coordinator = _coordinator()
    first = coordinator.submit(_request())
    second = coordinator.submit(_request())  # 同 request_id 重放

    assert second == first and len(coordinator.all()) == 1


# --- 负例：自循环 / 未知 / 循环依赖 / 预算 ------------------------------------

def test_self_loop_and_unknown_modules_blocked() -> None:
    coordinator = _coordinator()
    self_loop = coordinator.submit(_request(request_id="a", target_module="enrollment"))
    unknown = coordinator.submit(_request(request_id="b", requester_module="ghost"))

    assert self_loop.status == STATUS_BLOCKED and "自循环" in self_loop.note
    assert unknown.status == STATUS_BLOCKED and "未知" in unknown.note


def test_dependency_cycle_blocked() -> None:
    # activity 反过来请求 enrollment：enrollment 已依赖 activity → 成环
    coordinator = _coordinator()
    cyclic = coordinator.submit(_request(
        request_id="cyc", requester_module="activity", target_module="enrollment",
        capability_id="enrollment.find_by_id"))

    assert cyclic.status == STATUS_BLOCKED and "循环依赖" in cyclic.note


def test_budget_insufficient_blocked() -> None:
    coordinator = _coordinator()
    blocked = coordinator.submit(_request(), budget_available=False)
    assert blocked.status == STATUS_BLOCKED and "预算" in blocked.note


def test_repeated_proposals_capped_and_escalated() -> None:
    coordinator = ModuleCoordinator(derive_modules(
        SoftwareSpecification.model_validate_json(CAMPUS.read_text(encoding="utf-8")),
        project_id="p"), max_proposals=2)
    for index in range(2):
        coordinator.submit(_request(request_id=f"r{index}"))  # 同 (requester,target,capability)
    over = coordinator.submit(_request(request_id="r2"))

    assert over.status == STATUS_BLOCKED and "反复提议" in over.note


def test_complete_requires_assignment() -> None:
    coordinator = _coordinator()
    coordinator.submit(_request())
    with pytest.raises(AppException):
        coordinator.complete("req-1", published_version=1)  # 未派发
    with pytest.raises(AppException):
        coordinator.assign("missing")


# --- 业务范围变更须用户确认 ----------------------------------------------------

def test_scope_change_requires_user_confirmation() -> None:
    coordinator = _coordinator()
    request = coordinator.submit(_request(kind=KIND_SCOPE, change_reason="新增活动审批权限"))
    assert request.status == STATUS_PENDING_USER and "用户确认" in request.note

    with pytest.raises(AppException):
        coordinator.confirm_scope_change("req-1", approved=True)  # 缺新 spec 引用
    approved = coordinator.confirm_scope_change("req-1", approved=True, new_spec_ref="spec:v2")
    assert approved.status == STATUS_PENDING and approved.spec_ref == "spec:v2"


def test_rejected_scope_change_terminates_without_proceeding() -> None:
    coordinator = _coordinator()
    coordinator.submit(_request(kind=KIND_SCOPE))
    rejected = coordinator.confirm_scope_change("req-1", approved=False)

    assert rejected.status == STATUS_REJECTED and "草稿保留" in rejected.note
    with pytest.raises(AppException):
        coordinator.assign("req-1")  # 拒绝后不得擅自继续


def test_confirm_scope_change_rejects_non_scope() -> None:
    coordinator = _coordinator()
    coordinator.submit(_request())  # 技术请求
    with pytest.raises(AppException):
        coordinator.confirm_scope_change("req-1", approved=True, new_spec_ref="s")


# --- 破坏性变更受影响消费者分析 -----------------------------------------------

def test_breaking_consumers_lists_dependents() -> None:
    coordinator = _coordinator()
    assert coordinator.breaking_consumers("activity.find_by_id") == ["enrollment"]


# --- 持久化与"非模型"性质 -----------------------------------------------------

def test_requests_persist(tmp_path: Path) -> None:
    coordinator = _coordinator()
    coordinator.submit(_request())
    path = coordinator.save(tmp_path)
    assert path.is_file() and "req-1" in path.read_text(encoding="utf-8")


def test_no_model_coupling() -> None:
    """协调器不是"路由决策模型"：不引用任何模型客户端/网关。"""
    source = (REPO / "platform-api" / "src" / "agentforge_platform" / "module_coordinator.py").read_text(
        encoding="utf-8")
    for token in ("model_gateway", "ModelClient", "invoke(", "deepseek", "qwen"):
        assert token not in source
