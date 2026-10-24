"""Store the full result of every analysis run, including runs without a verdict.

A run's technical status (queued, running, completed, failed) is now separate
from its analytic outcome (pass, fail, incomplete, not_applicable), which only
a completed run has. passed becomes nullable: true only for pass, false only
for fail. Runs also record the fail_on threshold, file and rule counters,
diagnostic counts, timing, the engine version, a snapshot and hash of the
configuration that was checked, a safe error code for failed runs, and the
base and head commits of GitHub runs. Check constraints keep these consistent.

Existing runs, all completed with a verdict, get outcome pass or fail, the
"error" threshold and their error count (v0.2 violations were all errors);
counters and timing that v0.2 did not record stay null.

Downgrading deletes the runs v0.2 cannot represent (those without a pass or
fail verdict, with their diagnostics) and drops the new columns.

Revision ID: 20261024_0005
Revises: 20261024_0004
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261024_0005"
down_revision: str | None = "20261024_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

runs = sa.table(
    "analysis_runs",
    sa.column("passed", sa.Boolean),
    sa.column("outcome", sa.String),
    sa.column("fail_on", sa.String),
    sa.column("violation_count", sa.Integer),
    sa.column("error_count", sa.Integer),
)

NEW_COLUMNS = [
    sa.Column("outcome", sa.String(length=20), nullable=True),
    sa.Column("fail_on", sa.String(length=10), nullable=True),
    sa.Column("files_total", sa.Integer(), nullable=True),
    sa.Column("files_eligible", sa.Integer(), nullable=True),
    sa.Column("files_excluded", sa.Integer(), nullable=True),
    sa.Column("files_skipped", sa.Integer(), nullable=True),
    sa.Column("files_ignored", sa.Integer(), nullable=True),
    sa.Column("rules_total", sa.Integer(), nullable=True),
    sa.Column("evaluations", sa.Integer(), nullable=True),
    sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("warning_count", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("analysis_error_count", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("duration_ms", sa.Integer(), nullable=True),
    sa.Column("engine_version", sa.String(length=50), nullable=True),
    sa.Column("config_hash", sa.String(length=64), nullable=True),
    sa.Column("config_snapshot", sa.JSON(), nullable=True),
    sa.Column("error_code", sa.String(length=50), nullable=True),
    sa.Column("error_message", sa.Text(), nullable=True),
    sa.Column("base_sha", sa.String(length=64), nullable=True),
    sa.Column("head_sha", sa.String(length=64), nullable=True),
]

# The same constraints as app.models.AnalysisRun. A CHECK constraint accepts a
# NULL result, so no condition may yield NULL.
CHECKS = {
    "ck_analysis_runs_status": (
        "status IN ('queued', 'running', 'completed', 'failed')"
    ),
    "ck_analysis_runs_outcome": (
        "(status = 'completed' AND COALESCE(outcome, '') IN "
        "('pass', 'fail', 'incomplete', 'not_applicable')) "
        "OR (status <> 'completed' AND outcome IS NULL)"
    ),
    "ck_analysis_runs_passed": (
        "(COALESCE(outcome, '') = 'pass' AND passed IS NOT NULL AND passed = true) "
        "OR (COALESCE(outcome, '') = 'fail' AND passed IS NOT NULL "
        "AND passed = false) "
        "OR (COALESCE(outcome, '') NOT IN ('pass', 'fail') AND passed IS NULL)"
    ),
    "ck_analysis_runs_error": "(status = 'failed') = (error_code IS NOT NULL)",
}


def upgrade() -> None:
    for column in NEW_COLUMNS:
        op.add_column("analysis_runs", column)
    op.alter_column(
        "analysis_runs", "passed", existing_type=sa.Boolean(), nullable=True
    )

    bind = op.get_bind()
    bind.execute(
        runs.update().values(
            outcome=sa.case((runs.c.passed, "pass"), else_="fail"),
            fail_on="error",
            error_count=runs.c.violation_count,
        )
    )
    for column in ("error_count", "warning_count", "analysis_error_count"):
        op.alter_column("analysis_runs", column, server_default=None)
    for name, condition in CHECKS.items():
        op.create_check_constraint(name, "analysis_runs", condition)


def downgrade() -> None:
    for name in CHECKS:
        op.drop_constraint(name, "analysis_runs", type_="check")
    bind = op.get_bind()
    # Their diagnostics go with them (ON DELETE CASCADE).
    bind.execute(sa.text("DELETE FROM analysis_runs WHERE passed IS NULL"))
    op.alter_column(
        "analysis_runs", "passed", existing_type=sa.Boolean(), nullable=False
    )
    for column in reversed(NEW_COLUMNS):
        op.drop_column("analysis_runs", column.name)
