"""add external_readiness to run (C03 integration readiness snapshot)

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 可空：旧 run 行为 NULL → API 读取时派生（无侵入迁移，不重写历史数据）。
    op.add_column("run", sa.Column("external_readiness", sa.String(length=24), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("run") as batch_op:
        batch_op.drop_column("external_readiness")
