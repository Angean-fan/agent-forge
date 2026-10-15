"""F05：Module Coordinator —— 跨模块依赖请求、接口变更与职责冲突协调。

建立**结构化模块变更请求**机制：消费者提出所需能力或契约修改，Coordinator 核验**所有权/依赖/
DAG/预算**后，把变更任务分配给**真正负责该模块**的 Agent；模块之间**只传经批准的公共接口**，
不交接内部实现。

边界（对齐《AgentForge_03》F05）：

- **不是"监督者"进程，也不是新的"路由决策模型"**：无模型参与，纯确定性 Python；
- **不得因内部请求改变用户冻结业务规格**：涉及新业务范围/权限/技术 Profile 的变更 → ``PENDING_USER``，
  必须回到 DeepSeek 用户确认 + 新 spec 版本；纯技术缺口 → 由现有 Qwen 提议、Python 确认；
- **不越权**：请求只路由到目标模块的所有者；模块 A 不得写 B 文件（由 F01/F04 强制）；
- **破坏性公共接口变更**须做**受影响消费者分析**，禁止静默覆盖；
- **自循环/依赖环/反复提议/预算不足/版本冲突**一律阻塞（转人工），不擅自继续；
- 相同 ``request_id`` **重放幂等**。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.module_registry import ModuleRegistry

COORDINATOR_VERSION = "0.1"
REQUESTS_FILE = "change-requests.json"

KIND_CAPABILITY = "CAPABILITY_REQUEST"  # 需要既有能力的暴露/契约调整（技术）
KIND_SCOPE = "SCOPE_CHANGE"             # 新业务范围/权限/Profile（须用户确认 + 新 spec）

STATUS_PENDING = "PENDING"
STATUS_PENDING_USER = "PENDING_USER"
STATUS_APPROVED = "APPROVED"
STATUS_DONE = "DONE"
STATUS_REJECTED = "REJECTED"
STATUS_BLOCKED = "BLOCKED"

DEFAULT_MAX_PROPOSALS = 3

_STRICT = ConfigDict(extra="forbid")


class ChangeRequest(BaseModel):
    """结构化模块变更请求协议。"""

    model_config = _STRICT

    request_id: str
    requester_module: str
    target_module: str
    capability_id: str
    required_contract: str
    change_reason: str
    acceptance_ids: list[str] = []
    status: str = STATUS_PENDING
    spec_ref: str | None = None
    kind: str = KIND_CAPABILITY
    published_version: int | None = None
    note: str | None = None

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in {KIND_CAPABILITY, KIND_SCOPE}:
            raise ValueError(f"未知请求类型：{value!r}")
        return value


class CoordinatorState(BaseModel):
    model_config = _STRICT

    schema_version: str = COORDINATOR_VERSION
    requests: list[ChangeRequest] = []


def _reachable(start: str, edges: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        for nxt in edges.get(node, set()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


class ModuleCoordinator:
    """确定性协调器：请求受理 → 分类 → 分配 → 结果登记；所有权仍归各模块。"""

    def __init__(self, registry: ModuleRegistry, *, max_proposals: int = DEFAULT_MAX_PROPOSALS):
        self._registry = registry
        self._max_proposals = max_proposals
        self._by_id: dict[str, ChangeRequest] = {}
        self._order: list[str] = []

    # ---- 内部 ----
    def _module_ids(self) -> set[str]:
        return {m.module_id for m in self._registry.modules}

    def _edges(self) -> dict[str, set[str]]:
        return {m.module_id: set(m.depends_on_modules) for m in self._registry.modules}

    def _proposal_count(self, request: ChangeRequest) -> int:
        return sum(
            1 for r in self._by_id.values()
            if (r.requester_module, r.target_module, r.capability_id)
            == (request.requester_module, request.target_module, request.capability_id)
        )

    def _validate(self, request: ChangeRequest) -> list[str]:
        issues: list[str] = []
        ids = self._module_ids()
        if request.requester_module == request.target_module:
            issues.append("自循环请求：请求者与目标模块相同")
        for label, module_id in (("请求者", request.requester_module), ("目标", request.target_module)):
            if module_id not in ids:
                issues.append(f"{label}模块未知：{module_id}")
        # 循环依赖：若目标已（经传递）依赖请求者，则本请求会成环
        if request.requester_module in _reachable(request.target_module, self._edges()):
            issues.append(
                f"循环依赖：{request.target_module} 已依赖 {request.requester_module}"
            )
        if not request.capability_id or "." not in request.capability_id:
            issues.append(f"非法 capability_id：{request.capability_id}")
        return issues

    # ---- 受理 ----
    def submit(self, request: ChangeRequest, *, budget_available: bool = True) -> ChangeRequest:
        """受理变更请求；幂等（同 ``request_id`` 重放返回既有结果）。"""
        if request.request_id in self._by_id:
            return self._by_id[request.request_id]  # 幂等重放

        issues = self._validate(request)
        if issues:
            return self._store(request.model_copy(update={
                "status": STATUS_BLOCKED, "note": "；".join(issues)}))

        if request.kind == KIND_SCOPE:
            return self._store(request.model_copy(update={
                "status": STATUS_PENDING_USER,
                "note": "涉及新业务范围/权限/Profile：须 DeepSeek 用户确认并冻结新 spec。",
            }))

        if self._proposal_count(request) >= self._max_proposals:
            return self._store(request.model_copy(update={
                "status": STATUS_BLOCKED, "note": "同一能力反复提议超上限，转人工。"}))
        if not budget_available:
            return self._store(request.model_copy(update={
                "status": STATUS_BLOCKED, "note": "共享预算不足，未派发。"}))
        return self._store(request.model_copy(update={
            "status": STATUS_PENDING, "note": f"待分配给 {request.target_module}。"}))

    def confirm_scope_change(
        self, request_id: str, *, approved: bool, new_spec_ref: str | None = None
    ) -> ChangeRequest:
        """用户对**新业务变更**的裁决：拒绝则终止/保留草稿，绝不擅自在冻结规格上继续。"""
        request = self._require(request_id)
        if request.kind != KIND_SCOPE:
            raise AppException(ErrorCode.VALIDATION_ERROR, f"{request_id} 非业务范围变更。", status_code=422)
        if approved and not new_spec_ref:
            raise AppException(
                ErrorCode.VALIDATION_ERROR, "批准业务变更必须提供新的冻结 spec 引用。", status_code=422)
        return self._store(request.model_copy(update={
            "status": STATUS_PENDING if approved else STATUS_REJECTED,
            "spec_ref": new_spec_ref if approved else request.spec_ref,
            "note": "用户已确认并冻结新 spec。" if approved else "用户拒绝新业务变更：终止（草稿保留）。",
        }))

    def assign(self, request_id: str) -> ChangeRequest:
        """把技术请求派发给**目标模块的所有者**（模块 A 不得代写 B）。"""
        request = self._require(request_id)
        if request.status != STATUS_PENDING:
            raise AppException(
                ErrorCode.VALIDATION_ERROR, f"{request_id} 当前状态不可派发：{request.status}", status_code=409)
        return self._store(request.model_copy(update={
            "status": STATUS_APPROVED, "note": f"已派发 {request.target_module} 的模块 Agent。"}))

    def complete(self, request_id: str, *, published_version: int) -> ChangeRequest:
        """目标模块发布新接口后登记结果，依赖方可在满足后继续。"""
        request = self._require(request_id)
        if request.status != STATUS_APPROVED:
            raise AppException(
                ErrorCode.VALIDATION_ERROR, f"{request_id} 未派发，不能登记完成。", status_code=409)
        return self._store(request.model_copy(update={
            "status": STATUS_DONE, "published_version": published_version,
            "note": f"已发布 {request.capability_id}@{published_version}，依赖方继续。"}))

    def breaking_consumers(self, capability_id: str) -> list[str]:
        """受影响消费者分析：依赖 **提供者模块** 的模块（破坏性升级前必须核验/迁移）。"""
        owner = capability_id.split(".", 1)[0]
        return sorted(
            m.module_id for m in self._registry.modules
            if owner in m.depends_on_modules and m.module_id != owner
        )

    # ---- 存取 ----
    def get(self, request_id: str) -> ChangeRequest | None:
        return self._by_id.get(request_id)

    def all(self) -> list[ChangeRequest]:
        return [self._by_id[rid] for rid in self._order]

    def _store(self, request: ChangeRequest) -> ChangeRequest:
        if request.request_id not in self._by_id:
            self._order.append(request.request_id)
        self._by_id[request.request_id] = request
        return request

    def _require(self, request_id: str) -> ChangeRequest:
        request = self._by_id.get(request_id)
        if request is None:
            raise AppException(ErrorCode.NOT_FOUND, f"未知请求：{request_id}", status_code=404)
        return request

    # ---- 持久化 ----
    def save(self, run_dir: str | Path) -> Path:
        path = Path(run_dir) / REQUESTS_FILE
        state = CoordinatorState(requests=self.all())
        path.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path


__all__ = [
    "COORDINATOR_VERSION",
    "DEFAULT_MAX_PROPOSALS",
    "KIND_CAPABILITY",
    "KIND_SCOPE",
    "REQUESTS_FILE",
    "STATUS_APPROVED",
    "STATUS_BLOCKED",
    "STATUS_DONE",
    "STATUS_PENDING",
    "STATUS_PENDING_USER",
    "STATUS_REJECTED",
    "ChangeRequest",
    "CoordinatorState",
    "ModuleCoordinator",
]
