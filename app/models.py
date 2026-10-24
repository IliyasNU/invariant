from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
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
    """One analysis: its execution status, and its outcome once completed.

    Runs recorded before v0.3 lack the counters, timing and configuration
    snapshot, which are null for them.
    """

    __tablename__ = "analysis_runs"
    # A CHECK constraint accepts a NULL result, so no condition may yield NULL.
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="ck_analysis_runs_status",
        ),
        CheckConstraint(
            "(status = 'completed' AND COALESCE(outcome, '') IN "
            "('pass', 'fail', 'incomplete', 'not_applicable')) "
            "OR (status <> 'completed' AND outcome IS NULL)",
            name="ck_analysis_runs_outcome",
        ),
        CheckConstraint(
            "(COALESCE(outcome, '') = 'pass' AND passed IS NOT NULL AND passed = true) "
            "OR (COALESCE(outcome, '') = 'fail' AND passed IS NOT NULL "
            "AND passed = false) "
            "OR (COALESCE(outcome, '') NOT IN ('pass', 'fail') AND passed IS NULL)",
            name="ck_analysis_runs_passed",
        ),
        # A queued run may keep the error of its previous attempt.
        CheckConstraint(
            "(status <> 'failed' OR error_code IS NOT NULL) "
            "AND (status <> 'completed' OR error_code IS NULL)",
            name="ck_analysis_runs_error",
        ),
        Index("ix_analysis_runs_status_next_attempt_at", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(30))
    # queued, running, completed or failed: whether the analysis ran.
    status: Mapped[str] = mapped_column(String(30))
    # pass, fail, incomplete or not_applicable: what a completed run found.
    outcome: Mapped[str | None] = mapped_column(String(20))
    # True only for pass, False only for fail.
    passed: Mapped[bool | None] = mapped_column(Boolean)
    fail_on: Mapped[str | None] = mapped_column(String(10))
    pull_number: Mapped[int | None] = mapped_column(Integer)
    repository_owner: Mapped[str | None] = mapped_column(String(100))
    repository_name: Mapped[str | None] = mapped_column(String(100))
    github_delivery_id: Mapped[str | None] = mapped_column(
        String(100), unique=True, index=True
    )
    base_sha: Mapped[str | None] = mapped_column(String(64))
    head_sha: Mapped[str | None] = mapped_column(String(64))
    files_total: Mapped[int | None] = mapped_column(Integer)
    files_eligible: Mapped[int | None] = mapped_column(Integer)
    files_analyzed: Mapped[int] = mapped_column(Integer)
    files_excluded: Mapped[int | None] = mapped_column(Integer)
    files_skipped: Mapped[int | None] = mapped_column(Integer)
    files_ignored: Mapped[int | None] = mapped_column(Integer)
    rules_total: Mapped[int | None] = mapped_column(Integer)
    # The number of enabled rules (its v0.2 name).
    rules_evaluated: Mapped[int] = mapped_column(Integer)
    # Pairs of (analyzed file, enabled rule that applies to it).
    evaluations: Mapped[int | None] = mapped_column(Integer)
    violation_count: Mapped[int] = mapped_column(Integer)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    analysis_error_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    engine_version: Mapped[str | None] = mapped_column(String(50))
    # The project settings and rules that were checked, and their SHA-256.
    config_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    config_hash: Mapped[str | None] = mapped_column(String(64))
    # Why a failed run did not complete, or why a queued run's previous attempt
    # did not; never a token or an upstream payload.
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    # The worker queue: attempts made, when the next may start, and the lease
    # of the worker running the run.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
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

    @property
    def file_counts(self) -> dict[str, int | None]:
        return {
            "total": self.files_total,
            "eligible": self.files_eligible,
            "analyzed": self.files_analyzed,
            "excluded": self.files_excluded,
            "skipped": self.files_skipped,
            "ignored": self.files_ignored,
        }

    @property
    def rule_counts(self) -> dict[str, int | None]:
        return {
            "total": self.rules_total,
            "enabled": self.rules_evaluated,
            "evaluations": self.evaluations,
        }

    @property
    def diagnostic_counts(self) -> dict[str, int]:
        return {
            "errors": self.error_count,
            "warnings": self.warning_count,
            "analysis_errors": self.analysis_error_count,
        }

    @property
    def error(self) -> dict[str, str | None] | None:
        if self.error_code is None:
            return None
        return {"code": self.error_code, "message": self.error_message}


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
