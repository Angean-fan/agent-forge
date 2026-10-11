"""平台元数据模型（SQLite）。

M03 仅落地 ``project`` 与 ``spec_version`` 两张最小表；后续 ``run``/``task``/
``model_call`` 等在 M19/M21 追加。平台元数据只存规格与状态，不存放生成项目
的大段源码或日志。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "project"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
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
    body_json: Mapped[str] = mapped_column(Text, nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlatformSession(Base):
    """平台登录会话；仅存不可逆 token 哈希，不存原始令牌。"""

    __tablename__ = "platform_session"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
