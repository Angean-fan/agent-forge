"""Guard draft writes and pin model price per call.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "spec_version",
        sa.Column("draft_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("model_call", sa.Column("input_micro_per_million", sa.BigInteger()))
    op.add_column("model_call", sa.Column("output_micro_per_million", sa.BigInteger()))


def downgrade() -> None:
    with op.batch_alter_table("model_call") as batch_op:
        batch_op.drop_column("output_micro_per_million")
        batch_op.drop_column("input_micro_per_million")
    with op.batch_alter_table("spec_version") as batch_op:
        batch_op.drop_column("draft_revision")
