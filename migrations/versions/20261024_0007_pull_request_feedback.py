"""Keep one updatable comment per pull request, and its delivery state per run.

pull_request_comments remembers the comment Invariant writes on each pull
request, so later runs edit it instead of adding comments, and which run it
shows, so an older run never replaces a newer one's. A run's feedback_status
says whether its comment is pending, posted, stale (its commit is no longer the
pull request's head) or failed; delivery is retried on its own schedule.

Downgrading drops both. Comments already on GitHub stay there; later runs of a
v0.2 deployment would post new ones.

Revision ID: 20261024_0007
Revises: 20261024_0006
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261024_0007"
down_revision: str | None = "20261024_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEEDBACK_CHECK = (
    "feedback_status IS NULL "
    "OR feedback_status IN ('pending', 'posted', 'stale', 'failed')"
)


def upgrade() -> None:
    op.create_table(
        "pull_request_comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("pull_number", sa.Integer(), nullable=False),
        sa.Column("comment_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "analysis_run_id",
            sa.Integer(),
            sa.ForeignKey("analysis_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("head_sha", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "project_id", "pull_number", name="uq_pull_request_comments_pull"
        ),
    )
    op.add_column(
        "analysis_runs", sa.Column("feedback_status", sa.String(20), nullable=True)
    )
    op.add_column(
        "analysis_runs",
        sa.Column(
            "feedback_attempts", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.alter_column("analysis_runs", "feedback_attempts", server_default=None)
    op.add_column(
        "analysis_runs",
        sa.Column(
            "feedback_next_attempt_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "analysis_runs", sa.Column("feedback_error", sa.String(50), nullable=True)
    )
    op.create_check_constraint(
        "ck_analysis_runs_feedback", "analysis_runs", FEEDBACK_CHECK
    )


def downgrade() -> None:
    op.drop_constraint("ck_analysis_runs_feedback", "analysis_runs", type_="check")
    for column in (
        "feedback_error",
        "feedback_next_attempt_at",
        "feedback_attempts",
        "feedback_status",
    ):
        op.drop_column("analysis_runs", column)
    op.drop_table("pull_request_comments")
