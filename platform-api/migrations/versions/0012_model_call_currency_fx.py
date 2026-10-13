"""add price currency and locked FX snapshot to model_call (R20 shared budget)

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # R20：记录定价币种与锁定汇率快照（非 CNY 时按快照折算，未知则不写零/保持 UNKNOWN）。
    op.add_column("model_call", sa.Column("price_currency", sa.String(8), nullable=True))
    op.add_column("model_call", sa.Column("fx_rate_micro", sa.BigInteger(), nullable=True))
    op.add_column("model_call", sa.Column("fx_captured_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("model_call") as batch_op:
        batch_op.drop_column("fx_captured_at")
        batch_op.drop_column("fx_rate_micro")
        batch_op.drop_column("price_currency")
