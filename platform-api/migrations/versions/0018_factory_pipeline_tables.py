"""add factory_blueprint, department_run, and defect_ticket tables for FP01 factory pipeline

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "factory_blueprint",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("blueprint_json", sa.Text(), nullable=False),
        sa.Column("blueprint_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "run_id",
            "phase",
            "version",
            name="uq_factory_blueprint_run_phase_version",
        ),
    )
    op.create_index("ix_factory_blueprint_run_id", "factory_blueprint", ["run_id"])
    op.create_index("ix_factory_blueprint_run_phase", "factory_blueprint", ["run_id", "phase"])

    op.create_table(
        "department_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("dept_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("current_round", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_rounds", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("staging_dir", sa.String(length=512), nullable=True),
        sa.Column("api_fragment_json", sa.Text(), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=48), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "run_id",
            "phase",
            "dept_id",
            name="uq_department_run_run_phase_dept",
        ),
    )
    op.create_index("ix_department_run_run_id", "department_run", ["run_id"])
    op.create_index(
        "ix_department_run_run_phase_status",
        "department_run",
        ["run_id", "phase", "status"],
    )

    op.create_table(
        "defect_ticket",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("dept_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error_class", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_defect_ticket_run_id", "defect_ticket", ["run_id"])
    op.create_index(
        "ix_defect_ticket_run_phase_dept_resolved",
        "defect_ticket",
        ["run_id", "phase", "dept_id", "resolved"],
    )


def downgrade() -> None:
    op.drop_index("ix_defect_ticket_run_phase_dept_resolved", table_name="defect_ticket")
    op.drop_index("ix_defect_ticket_run_id", table_name="defect_ticket")
    op.drop_table("defect_ticket")

    op.drop_index("ix_department_run_run_phase_status", table_name="department_run")
    op.drop_index("ix_department_run_run_id", table_name="department_run")
    op.drop_table("department_run")

    op.drop_index("ix_factory_blueprint_run_phase", table_name="factory_blueprint")
    op.drop_index("ix_factory_blueprint_run_id", table_name="factory_blueprint")
    op.drop_table("factory_blueprint")
