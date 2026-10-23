"""Add GitHub configuration and persistent analysis runs.

Revision ID: 20260906_0002
Revises: 20260903_0001
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_0002"
down_revision: str | None = "20260903_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects", sa.Column("github_owner", sa.String(length=100), nullable=True)
    )
    op.add_column(
        "projects", sa.Column("github_repo", sa.String(length=100), nullable=True)
    )
    op.create_check_constraint(
        "ck_projects_github_pair",
        "projects",
        "(github_owner IS NULL AND github_repo IS NULL) OR "
        "(github_owner IS NOT NULL AND github_repo IS NOT NULL)",
    )
    op.create_unique_constraint(
        "uq_projects_github_repo", "projects", ["github_owner", "github_repo"]
    )

    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("pull_number", sa.Integer(), nullable=True),
        sa.Column("repository_owner", sa.String(length=100), nullable=True),
        sa.Column("repository_name", sa.String(length=100), nullable=True),
        sa.Column("github_delivery_id", sa.String(length=100), nullable=True),
        sa.Column("files_analyzed", sa.Integer(), nullable=False),
        sa.Column("rules_evaluated", sa.Integer(), nullable=False),
        sa.Column("violation_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_analysis_runs_project_id", "analysis_runs", ["project_id"], unique=False
    )
    op.create_index(
        "ix_analysis_runs_github_delivery_id",
        "analysis_runs",
        ["github_delivery_id"],
        unique=True,
    )

    op.create_table(
        "analysis_violations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("analysis_run_id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=True),
        sa.Column("rule_name", sa.String(length=120), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("column", sa.Integer(), nullable=True),
        sa.Column("imported_module", sa.String(length=500), nullable=True),
        sa.Column("message", sa.String(length=1000), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"], ["analysis_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["rule_id"], ["rules.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_analysis_violations_analysis_run_id",
        "analysis_violations",
        ["analysis_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_analysis_violations_rule_id",
        "analysis_violations",
        ["rule_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_analysis_violations_rule_id", table_name="analysis_violations")
    op.drop_index(
        "ix_analysis_violations_analysis_run_id", table_name="analysis_violations"
    )
    op.drop_table("analysis_violations")
    op.drop_index("ix_analysis_runs_github_delivery_id", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_project_id", table_name="analysis_runs")
    op.drop_table("analysis_runs")
    op.drop_constraint("uq_projects_github_repo", "projects", type_="unique")
    op.drop_constraint("ck_projects_github_pair", "projects", type_="check")
    op.drop_column("projects", "github_repo")
    op.drop_column("projects", "github_owner")
