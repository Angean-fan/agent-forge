"""add run.spec_hash (R06 frozen spec hash binding)

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 可空：R06 之前的旧 run 保持 NULL，读取时按 spec_id 回退解析；新 run 在提交时写入冻结 hash。
    op.add_column("run", sa.Column("spec_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("run") as batch_op:
        batch_op.drop_column("spec_hash")
