from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "(github_owner IS NULL AND github_repo IS NULL) OR "
            "(github_owner IS NOT NULL AND github_repo IS NOT NULL)",
            name="ck_projects_github_pair",
        ),
        UniqueConstraint("github_owner", "github_repo", name="uq_projects_github_repo"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(1000))
    github_owner: Mapped[str | None] = mapped_column(String(100))
    github_repo: Mapped[str | None] = mapped_column(String(100))
    source_roots: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["."])
    exclude_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    fail_on: Mapped[str] = mapped_column(String(10), default="error")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Deleting a project deletes its rules and its whole analysis history; the
    # database cascades it without loading every row first.
    rules: Mapped[list[Rule]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    analysis_runs: Mapped[list[AnalysisRun]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )


class Rule(Base):
    __tablename__ = "rules"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_rules_project_name"),
        UniqueConstraint("project_id", "key", name="uq_rules_project_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    rule_type: Mapped[str] = mapped_column(String(50))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    severity: Mapped[str] = mapped_column(String(10), default="error")
    source_pattern: Mapped[str] = mapped_column(String(255), default="*")
    exclude_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    # The fields specific to rule_type, e.g. {"forbidden_imports": [...]}.
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="rules")

    @property
    def forbidden_imports(self) -> list[str] | None:
        return self.params.get("forbidden_imports")

    @property
    def forbidden_calls(self) -> list[str] | None:
        return self.params.get("forbidden_calls")

    @property
    def max_lines(self) -> int | None:
        return self.params.get("max_lines")


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="completed")
    passed: Mapped[bool] = mapped_column(Boolean)
    pull_number: Mapped[int | None] = mapped_column(Integer)
    repository_owner: Mapped[str | None] = mapped_column(String(100))
    repository_name: Mapped[str | None] = mapped_column(String(100))
    github_delivery_id: Mapped[str | None] = mapped_column(
        String(100), unique=True, index=True
    )
    files_analyzed: Mapped[int] = mapped_column(Integer)
    rules_evaluated: Mapped[int] = mapped_column(Integer)
    violation_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="analysis_runs")
    violations: Mapped[list[AnalysisViolation]] = relationship(
        back_populates="analysis_run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AnalysisViolation.id",
    )


class AnalysisViolation(Base):
    __tablename__ = "analysis_violations"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_run_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), index=True
    )
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("rules.id", ondelete="SET NULL"), index=True
    )
    # "violation" of a rule, or an "error" that kept a file from being analyzed.
    kind: Mapped[str] = mapped_column(String(20), default="violation")
    # The rule type of a violation, or the reason for an error.
    code: Mapped[str] = mapped_column(String(50))
    severity: Mapped[str] = mapped_column(String(10), default="error")
    # Snapshots of the rule as it was when the run happened.
    rule_key: Mapped[str | None] = mapped_column(String(64))
    rule_name: Mapped[str | None] = mapped_column(String(120))
    path: Mapped[str | None] = mapped_column(String(500))
    line: Mapped[int | None] = mapped_column(Integer)
    column: Mapped[int | None] = mapped_column(Integer)
    imported_module: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    analysis_run: Mapped[AnalysisRun] = relationship(back_populates="violations")
