"""平台元数据模型（SQLite）。

M03 仅落地 ``project`` 与 ``spec_version`` 两张最小表；后续 ``run``/``task``/
``model_call`` 等在 M19/M21 追加。平台元数据只存规格与状态，不存放生成项目
的大段源码或日志。
"""

from __future__ import annotations

from datetime import datetime

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
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
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
    thinking_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    price_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
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
