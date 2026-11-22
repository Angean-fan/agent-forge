"""平台元数据模型（SQLite）。

M03 仅落地 ``project`` 与 ``spec_version`` 两张最小表；后续 ``run``/``task``/
``model_call`` 等在 M19/M21 追加。平台元数据只存规格与状态，不存放生成项目
的大段源码或日志。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "project"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    # 用户创建项目时输入的原始需求文本（M05）；M06 据此生成规格草稿。
    requirement_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # 指向 spec_version.id；因与 spec_version.project_id 形成循环引用，DB 层不加外键，
    # 由应用层在 M07 确认规格时维护。
    active_spec_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SpecVersion(Base):
    __tablename__ = "spec_version"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_spec_version_project_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    body_json: Mapped[str] = mapped_column(Text, nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    missing_fields_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    questions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")


class PlatformSession(Base):
    """平台登录会话；仅存不可逆 token 哈希，不存原始令牌。"""

    __tablename__ = "platform_session"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class Run(Base):
    """一次代码生成运行；状态为数据库权威值，只能通过白名单迁移。"""

    __tablename__ = "run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    spec_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # R06：提交时记录的**冻结** spec hash；旧 run 为 NULL，按 spec_id 回退解析。
    spec_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # R22：取消请求标志（幂等置位）；worker 在节点边界观察后把 run 落 CANCELLED。
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # C03：外部服务就绪快照（NOT_REQUIRED/MOCK_ONLY/AWAITING_USER_CONFIG/LIVE_VERIFIED/LIVE_FAILED）。
    # 旧数据为 NULL → API 读取时派生（无侵入迁移）。
    external_readiness: Mapped[str | None] = mapped_column(String(24), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )


class RunTask(Base):
    """run 内的一个任务节点（DAG 由 task_id/deps 表达）。"""

    __tablename__ = "task"
    __table_args__ = (UniqueConstraint("run_id", "task_id", name="uq_task_run_task_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )


class RunEvent(Base):
    """运行事件；sequence 在每个 run 内单调递增，支持断点读取。"""

    __tablename__ = "event"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_event_run_sequence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )


class Artifact(Base):
    """run 产物文件记录（路径属于该 run 的目录）。"""

    __tablename__ = "artifact"
    __table_args__ = (UniqueConstraint("run_id", "path", name="uq_artifact_run_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )


class Worker(Base):
    """独立 worker 的注册与最近心跳；用于健康判定（停滞即不健康）。"""

    __tablename__ = "worker"

    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(128), nullable=False)
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )


class BudgetAdjustment(Base):
    """R24：可追溯的预算调整（项目级；可选关联 run 与操作者）。"""

    __tablename__ = "budget_adjustment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delta_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )


class ModelCall(Base):
    """模型调用账本；以 call_id 幂等结算。

    支持生成前草稿（project_id + draft_id，run_id 为空）与生成期（run_id）两种用量。
    金额使用整数**微单位**（1 元 = 1_000_000 微），并记录 `price_version`。
    """

    __tablename__ = "model_call"

    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_role: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    draft_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # R13：生成期调用所属 DAG 任务；生成前（draft）与旧行均为 NULL。
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # A07：工厂模式部门与阶段维度记账（可空，旧数据与草稿均为 NULL）。
    dept_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phase: Mapped[str | None] = mapped_column(String(16), nullable=True)
    round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thinking_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    price_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # R20：定价币种 + 锁定汇率快照（折算为人民币微单位时记录；未知则不写零、保持 UNKNOWN）。
    price_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    fx_rate_micro: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fx_captured_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    input_micro_per_million: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_micro_per_million: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reserved_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cost_micro: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )


class AssetRelease(Base):
    """A02：不可变资产版本表（覆盖 template / capability-pack / method-unit / integration-pack）。

    ``checksum`` 与 ``asset_registry.compute_checksum(asset_dir)`` 字节级一致。
    """

    __tablename__ = "asset_release"
    __table_args__ = (
        UniqueConstraint("kind", "asset_id", "version", name="uq_asset_release_kind_id_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    provides_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    requires_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )

    @property
    def profile_tags(self) -> list[str]:
        return list(json.loads(self.profile_tags_json or "[]"))

    @property
    def provides(self) -> list[str]:
        return list(json.loads(self.provides_json or "[]"))

    @property
    def requires(self) -> list[str]:
        return list(json.loads(self.requires_json or "[]"))

    @property
    def manifest_dict(self) -> dict[str, Any]:
        return dict(json.loads(self.manifest_json or "{}"))


class AssetFile(Base):
    """A02：资产版本内的逐文件源码与符号摘要表（供物化缓存与子 Agent 精准检索/读取）。"""

    __tablename__ = "asset_file"
    __table_args__ = (
        UniqueConstraint("release_id", "rel_path", name="uq_asset_file_release_rel_path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("asset_release.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rel_path: Mapped[str] = mapped_column(String(512), nullable=False)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    is_binary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    symbol_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )


class FactoryBlueprint(Base):
    """FP01：异步工厂流水线的阶段架构蓝图持久化表（按 run + phase + version 唯一）。"""

    __tablename__ = "factory_blueprint"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "phase",
            "version",
            name="uq_factory_blueprint_run_phase_version",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    blueprint_json: Mapped[str] = mapped_column(Text, nullable=False)
    blueprint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )


class DepartmentRun(Base):
    """FP01：工厂流水线部门级持久化运行状态（支撑并发更新、取消与崩溃断点续跑）。"""

    __tablename__ = "department_run"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "phase",
            "dept_id",
            name="uq_department_run_run_phase_dept",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    dept_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    current_round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    staging_dir: Mapped[str | None] = mapped_column(String(512), nullable=True)
    api_fragment_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )


class DefectTicketRow(Base):
    """FP01：工厂流水线缺陷单持久化记录（部门小循环或总装归因大循环产出的结构化缺陷）。"""

    __tablename__ = "defect_ticket"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    dept_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error_class: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
