"""Add nullable reasoning_effort to model_call (R03).

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 可空：旧账本行保持 NULL；仅对经探针验证的思考强度写入低/高。
    op.add_column("model_call", sa.Column("reasoning_effort", sa.String(16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("model_call") as batch_op:
        batch_op.drop_column("reasoning_effort")
