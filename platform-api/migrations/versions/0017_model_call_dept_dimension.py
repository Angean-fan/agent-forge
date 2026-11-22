"""add dept_id, phase, round columns to model_call for A07 concurrent department budget ledger

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_call") as batch_op:
        batch_op.add_column(sa.Column("dept_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("phase", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("round", sa.Integer(), nullable=True))
    op.create_index(
        "ix_model_call_run_dept_phase",
        "model_call",
        ["run_id", "dept_id", "phase"],
    )


def downgrade() -> None:
    op.drop_index("ix_model_call_run_dept_phase", table_name="model_call")
    with op.batch_alter_table("model_call") as batch_op:
        batch_op.drop_column("round")
        batch_op.drop_column("phase")
        batch_op.drop_column("dept_id")
