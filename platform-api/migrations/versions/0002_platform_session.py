"""create platform_session

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_session",
        sa.Column("token_hash", sa.String(length=64), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_platform_session_account_id", "platform_session", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_platform_session_account_id", table_name="platform_session")
    op.drop_table("platform_session")
