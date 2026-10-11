"""add M22 extraction metadata to spec drafts

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("spec_version", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column(
        "spec_version", sa.Column("missing_fields_json", sa.Text(), nullable=False, server_default="[]")
    )
    op.add_column(
        "spec_version", sa.Column("questions_json", sa.Text(), nullable=False, server_default="[]")
    )


def downgrade() -> None:
    with op.batch_alter_table("spec_version") as batch_op:
        batch_op.drop_column("questions_json")
        batch_op.drop_column("missing_fields_json")
        batch_op.drop_column("summary")
