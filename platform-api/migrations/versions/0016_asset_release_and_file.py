"""add asset_release and asset_file tables with PostgreSQL GIN tsvector indexes (A02)

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_release",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("profile_tags_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("provides_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("requires_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("kind", "asset_id", "version", name="uq_asset_release_kind_id_version"),
    )
    op.create_index("ix_asset_release_kind", "asset_release", ["kind"])
    op.create_index("ix_asset_release_asset_id", "asset_release", ["asset_id"])

    op.create_table(
        "asset_file",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "release_id",
            sa.Integer(),
            sa.ForeignKey("asset_release.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rel_path", sa.String(length=512), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("is_binary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("symbol_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("release_id", "rel_path", name="uq_asset_file_release_rel_path"),
    )
    op.create_index("ix_asset_file_release_id", "asset_file", ["release_id"])

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS ix_asset_release_search_fts "
                "ON asset_release USING gin (to_tsvector('simple', search_text))"
            )
        )
        op.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS ix_asset_file_search_fts "
                "ON asset_file USING gin (to_tsvector('simple', search_text))"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP INDEX IF EXISTS ix_asset_file_search_fts"))
        op.execute(sa.text("DROP INDEX IF EXISTS ix_asset_release_search_fts"))

    op.drop_index("ix_asset_file_release_id", table_name="asset_file")
    op.drop_table("asset_file")
    op.drop_index("ix_asset_release_asset_id", table_name="asset_release")
    op.drop_index("ix_asset_release_kind", table_name="asset_release")
    op.drop_table("asset_release")
