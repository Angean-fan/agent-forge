"""add run lease/idempotency columns and worker table

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.add_column("run", sa.Column("lease_owner", sa.String(length=64), nullable=True))
    op.add_column("run", sa.Column("lease_until", sa.DateTime(), nullable=True))
    op.create_index(
        "uq_run_project_idempotency",
        "run",
        ["project_id", "idempotency_key"],
        unique=True,
    )

    op.create_table(
        "worker",
        sa.Column("worker_id", sa.String(length=64), primary_key=True),
        sa.Column("hostname", sa.String(length=128), nullable=False),
        sa.Column("last_heartbeat", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("worker")
    op.drop_index("uq_run_project_idempotency", table_name="run")
    with op.batch_alter_table("run") as batch_op:
        batch_op.drop_column("lease_until")
        batch_op.drop_column("lease_owner")
        batch_op.drop_column("idempotency_key")
