"""What an analysis found: diagnostics, file counts and the overall outcome."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from invariant import __version__
from invariant.config import Severity

RESULT_SCHEMA_VERSION = 1

DiagnosticKind = Literal["violation", "error"]


class Outcome(StrEnum):
    # Every applicable check ran and no blocking violation was found.
    PASS = "pass"
    # At least one violation at or above the fail_on severity.
    FAIL = "fail"
    # Some eligible file could not be analyzed, so a pass cannot be claimed.
    INCOMPLETE = "incomplete"
    # Nothing was checked: no eligible file, or no enabled rule applied to one.
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class Diagnostic:
    """A violation of a rule, or an error that kept a file from being analyzed.

    Lines and columns are 1-based; columns count Unicode characters.
    """

    kind: DiagnosticKind
    # The rule type for a violation; the reason for an error, e.g. syntax_error.
    code: str
    severity: Severity
    message: str
    path: str | None = None
    line: int | None = None
    column: int | None = None
    rule_key: str | None = None
    rule_name: str | None = None
    # Facts specific to the rule type, such as the imported module.
    data: dict[str, Any] = field(default_factory=dict)

    def sort_key(self) -> tuple[str, int, int, str, str, str]:
        return (
            self.path or "",
            self.line or 0,
            self.column or 0,
            self.rule_key or "",
            self.code,
            self.message,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "rule_key": self.rule_key,
            "rule_name": self.rule_name,
            "data": self.data,
        }


@dataclass(frozen=True)
class FileCounts:
    """How the files given to an analysis were used.

    ``total = eligible + excluded + ignored`` and ``eligible = analyzed + skipped``.
    """

    # Every file the caller considered, Python or not.
    total: int = 0
    # Python files inside a source root and not excluded by the project.
    eligible: int = 0
    # Eligible files that were parsed and checked.
    analyzed: int = 0
    # Python files outside every source root or matching a project exclusion.
    excluded: int = 0
    # Eligible files that could not be analyzed; each has an error diagnostic.
    skipped: int = 0
    # Removed and non-Python files, which no rule can apply to.
    ignored: int = 0


@dataclass(frozen=True)
class AnalysisResult:
    outcome: Outcome
    fail_on: Severity
    files: FileCounts
    rules_total: int
    rules_enabled: int
    # Pairs of (analyzed file, enabled rule that applies to it).
    evaluations: int
    diagnostics: tuple[Diagnostic, ...]
    # Violations found but not listed, past the caller's ``max_violations``.
    omitted_errors: int = 0
    omitted_warnings: int = 0

    @property
    def passed(self) -> bool | None:
        """True for a pass, False for a fail, None when there is no verdict."""
        if self.outcome is Outcome.PASS:
            return True
        if self.outcome is Outcome.FAIL:
            return False
        return None

    @property
    def violations(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.kind == "violation")

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.kind == "error")

    @property
    def error_count(self) -> int:
        """Violations of severity error, listed or not."""
        listed = sum(item.severity == "error" for item in self.violations)
        return listed + self.omitted_errors

    @property
    def warning_count(self) -> int:
        """Violations of severity warning, listed or not."""
        listed = sum(item.severity == "warning" for item in self.violations)
        return listed + self.omitted_warnings

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "engine_version": __version__,
            "outcome": self.outcome.value,
            "passed": self.passed,
            "fail_on": self.fail_on,
            "files": {
                "total": self.files.total,
                "eligible": self.files.eligible,
                "analyzed": self.files.analyzed,
                "excluded": self.files.excluded,
                "skipped": self.files.skipped,
                "ignored": self.files.ignored,
            },
            "rules": {
                "total": self.rules_total,
                "enabled": self.rules_enabled,
                "evaluations": self.evaluations,
            },
            "summary": {
                "errors": self.error_count,
                "warnings": self.warning_count,
                "analysis_errors": len(self.errors),
            },
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }
