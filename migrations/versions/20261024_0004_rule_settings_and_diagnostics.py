"""Add rule and project settings and diagnostic fields on violations.

Rules gain keys, enabled flags, severities, exclusions and typed parameters;
projects gain source roots, exclusions and fail_on.

Existing rules keep working: each gets a key derived from its name (unique in
its project) and its forbidden_imports move into params. Existing violations
become kind "violation" with code "forbidden_import".

Downgrading removes what v0.2 cannot represent: rules of any type other than
forbidden_import are deleted, and keys, enabled flags, severities, exclusions,
project source roots, project exclusions and fail_on are dropped (disabled
rules become active again, warnings become errors).

Revision ID: 20261024_0004
Revises: 20261024_0003
Create Date: 2026-10-24
"""

import re
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261024_0004"
down_revision: str | None = "20261024_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

rules = sa.table(
    "rules",
    sa.column("id", sa.Integer),
    sa.column("project_id", sa.Integer),
    sa.column("name", sa.String),
    sa.column("key", sa.String),
    sa.column("rule_type", sa.String),
    sa.column("forbidden_imports", sa.JSON),
    sa.column("params", sa.JSON),
)
violations = sa.table(
    "analysis_violations",
    sa.column("rule_id", sa.Integer),
    sa.column("rule_key", sa.String),
    sa.column("imported_module", sa.Text),
    sa.column("data", sa.JSON),
)


def _key_from_name(name: str) -> str:
    # The same shape the API requires: lowercase letters, digits, ".", "_", "-".
    key = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-._")[:56]
    return key or "rule"


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("source_roots", sa.JSON(), nullable=False, server_default='["."]'),
    )
    op.add_column(
        "projects",
        sa.Column("exclude_paths", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "projects",
        sa.Column(
            "fail_on", sa.String(length=10), nullable=False, server_default="error"
        ),
    )

    op.add_column("rules", sa.Column("key", sa.String(length=64), nullable=True))
    op.add_column(
        "rules",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "rules",
        sa.Column(
            "severity", sa.String(length=10), nullable=False, server_default="error"
        ),
    )
    op.add_column(
        "rules",
        sa.Column("exclude_paths", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "rules", sa.Column("params", sa.JSON(), nullable=False, server_default="{}")
    )

    bind = op.get_bind()
    taken: dict[int, set[str]] = defaultdict(set)
    rows = bind.execute(
        sa.select(
            rules.c.id, rules.c.project_id, rules.c.name, rules.c.forbidden_imports
        ).order_by(rules.c.id)
    ).all()
    for row in rows:
        base = _key_from_name(row.name)
        key, suffix = base, 2
        while key in taken[row.project_id]:
            key, suffix = f"{base}-{suffix}", suffix + 1
        taken[row.project_id].add(key)
        bind.execute(
            rules.update()
            .where(rules.c.id == row.id)
            .values(key=key, params={"forbidden_imports": row.forbidden_imports})
        )

    op.alter_column("rules", "key", existing_type=sa.String(length=64), nullable=False)
    op.create_unique_constraint("uq_rules_project_key", "rules", ["project_id", "key"])
    op.drop_column("rules", "forbidden_imports")

    op.add_column(
        "analysis_violations",
        sa.Column(
            "kind", sa.String(length=20), nullable=False, server_default="violation"
        ),
    )
    op.add_column(
        "analysis_violations",
        sa.Column(
            "code",
            sa.String(length=50),
            nullable=False,
            server_default="forbidden_import",
        ),
    )
    op.add_column(
        "analysis_violations",
        sa.Column(
            "severity", sa.String(length=10), nullable=False, server_default="error"
        ),
    )
    op.add_column(
        "analysis_violations",
        sa.Column("rule_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "analysis_violations",
        sa.Column("data", sa.JSON(), nullable=False, server_default="{}"),
    )
    bind.execute(
        violations.update()
        .where(violations.c.imported_module.is_not(None))
        .values(
            data=sa.func.json_build_object(
                "imported_module", violations.c.imported_module
            )
        )
    )
    bind.execute(
        violations.update()
        .where(violations.c.rule_id == rules.c.id)
        .values(rule_key=rules.c.key)
    )
    # Analysis errors (a syntax error, a truncated file list) need not have a
    # rule or a path.
    op.alter_column(
        "analysis_violations",
        "rule_name",
        existing_type=sa.String(length=120),
        nullable=True,
    )
    op.alter_column(
        "analysis_violations",
        "path",
        existing_type=sa.String(length=500),
        nullable=True,
    )

    # The defaults only filled existing rows; the application sets every value.
    for table, column in (
        ("projects", "source_roots"),
        ("projects", "exclude_paths"),
        ("projects", "fail_on"),
        ("rules", "enabled"),
        ("rules", "severity"),
        ("rules", "exclude_paths"),
        ("rules", "params"),
        ("analysis_violations", "kind"),
        ("analysis_violations", "code"),
        ("analysis_violations", "severity"),
        ("analysis_violations", "data"),
    ):
        op.alter_column(table, column, server_default=None)


def downgrade() -> None:
    op.alter_column(
        "analysis_violations",
        "path",
        existing_type=sa.String(length=500),
        nullable=False,
    )
    op.alter_column(
        "analysis_violations",
        "rule_name",
        existing_type=sa.String(length=120),
        nullable=False,
    )
    for column in ("data", "rule_key", "severity", "code", "kind"):
        op.drop_column("analysis_violations", column)

    op.add_column("rules", sa.Column("forbidden_imports", sa.JSON(), nullable=True))
    bind = op.get_bind()
    bind.execute(rules.delete().where(rules.c.rule_type != "forbidden_import"))
    rows = bind.execute(sa.select(rules.c.id, rules.c.params)).all()
    for row in rows:
        bind.execute(
            rules.update()
            .where(rules.c.id == row.id)
            .values(forbidden_imports=row.params["forbidden_imports"])
        )
    op.alter_column(
        "rules", "forbidden_imports", existing_type=sa.JSON(), nullable=False
    )
    op.drop_constraint("uq_rules_project_key", "rules", type_="unique")
    for column in ("params", "exclude_paths", "severity", "enabled", "key"):
        op.drop_column("rules", column)

    for column in ("fail_on", "exclude_paths", "source_roots"):
        op.drop_column("projects", column)
