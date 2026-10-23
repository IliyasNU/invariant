"""Store violation diagnostics as unbounded text.

A valid import can be longer than 500 characters and the message that names it
longer than 1,000, which PostgreSQL rejected for the previous VARCHAR columns.

Revision ID: 20261024_0003
Revises: 20260906_0002
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261024_0003"
down_revision: str | None = "20260906_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "analysis_violations",
        "imported_module",
        existing_type=sa.String(length=500),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "analysis_violations",
        "message",
        existing_type=sa.String(length=1000),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Fails rather than truncating when a stored value no longer fits.
    op.alter_column(
        "analysis_violations",
        "message",
        existing_type=sa.Text(),
        type_=sa.String(length=1000),
        existing_nullable=False,
    )
    op.alter_column(
        "analysis_violations",
        "imported_module",
        existing_type=sa.Text(),
        type_=sa.String(length=500),
        existing_nullable=True,
    )
