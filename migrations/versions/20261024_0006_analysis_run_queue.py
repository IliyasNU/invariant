"""Make analysis runs a durable job queue for the worker.

A GitHub analysis is now accepted as a queued run and executed by a worker
process. Runs gain the number of attempts made, when the next one may start
(after a transient failure), and the lease of the worker running them: a run
whose lease expires is queued again or, after its last attempt, failed.

A queued run may now keep the error of its previous attempt, so error_code is
required for failed runs and forbidden only for completed ones.

Downgrading drops the queue columns. Runs still queued or running then have no
worker to finish them; they keep their status.

Revision ID: 20261024_0006
Revises: 20261024_0005
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261024_0006"
down_revision: str | None = "20261024_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The same constraint as app.models.AnalysisRun.
ERROR_CHECK = (
    "(status <> 'failed' OR error_code IS NOT NULL) "
    "AND (status <> 'completed' OR error_code IS NULL)"
)
PREVIOUS_ERROR_CHECK = "(status = 'failed') = (error_code IS NOT NULL)"


def upgrade() -> None:
    op.add_column(
        "analysis_runs",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "analysis_runs",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "analysis_runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "analysis_runs", sa.Column("locked_by", sa.String(length=100), nullable=True)
    )
    # Runs recorded before the queue each took one attempt.
    op.execute("UPDATE analysis_runs SET attempts = 1")
    op.alter_column("analysis_runs", "attempts", server_default=None)
    op.create_index(
        "ix_analysis_runs_status_next_attempt_at",
        "analysis_runs",
        ["status", "next_attempt_at"],
    )
    op.drop_constraint("ck_analysis_runs_error", "analysis_runs", type_="check")
    op.create_check_constraint("ck_analysis_runs_error", "analysis_runs", ERROR_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_analysis_runs_error", "analysis_runs", type_="check")
    # The previous constraint forbids an error code on a queued or running run.
    op.execute(
        "UPDATE analysis_runs SET error_code = NULL, error_message = NULL "
        "WHERE status IN ('queued', 'running')"
    )
    op.create_check_constraint(
        "ck_analysis_runs_error", "analysis_runs", PREVIOUS_ERROR_CHECK
    )
    op.drop_index("ix_analysis_runs_status_next_attempt_at", "analysis_runs")
    for column in ("locked_by", "lease_expires_at", "next_attempt_at", "attempts"):
        op.drop_column("analysis_runs", column)
