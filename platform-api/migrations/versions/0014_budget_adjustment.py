"""add budget_adjustment table (R24 traceable budget changes)

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budget_adjustment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("project.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("delta_micro", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("budget_adjustment")
