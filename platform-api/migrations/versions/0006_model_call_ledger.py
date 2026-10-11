"""add model_call ledger

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_call",
        sa.Column("call_id", sa.String(length=64), primary_key=True),
        sa.Column("model_role", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("draft_id", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("thinking_mode", sa.String(length=16), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("price_version", sa.String(length=32), nullable=True),
        sa.Column("reserved_micro", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_micro", sa.BigInteger(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_class", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_model_call_project_id", "model_call", ["project_id"])
    op.create_index("ix_model_call_run_id", "model_call", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_model_call_run_id", table_name="model_call")
    op.drop_index("ix_model_call_project_id", table_name="model_call")
    op.drop_table("model_call")
