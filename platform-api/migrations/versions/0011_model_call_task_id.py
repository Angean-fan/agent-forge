"""add nullable task_id to model_call (R13 task-scoped ledger)

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 可空：生成前（project_id + draft_id）调用与旧行均为 NULL；仅 DAG 任务内的生成期调用写入 task_id。
    op.add_column("model_call", sa.Column("task_id", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("model_call") as batch_op:
        batch_op.drop_column("task_id")
