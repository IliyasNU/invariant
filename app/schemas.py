import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    StringConstraints,
    Tag,
    field_validator,
    model_validator,
)

from invariant.config import (
    CallName,
    DisplayName,
    ForbiddenCallRule,
    ForbiddenImportRule,
    MaxFileLinesRule,
    ModuleGlob,
    ModuleName,
    NoRelativeImportRule,
    PathGlob,
    PathGlobList,
    RuleKey,
    Severity,
    SourceRootList,
)
from invariant.paths import PathError, normalize_path

GITHUB_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
# PostgreSQL cannot store NUL, and other control characters only break output.
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Free text may keep tabs and line breaks.
TEXT_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _reject_control_characters(
    value: str | None, pattern: re.Pattern[str] = CONTROL_CHARACTERS
) -> str | None:
    if value is not None and pattern.search(value):
        raise ValueError("must not contain control characters")
    return value


def _normalize_github_name(value: str | None) -> str | None:
    if value is not None and not GITHUB_NAME.fullmatch(value):
        raise ValueError("must contain only letters, numbers, '.', '_', or '-'")
    return value.lower() if value is not None else None


class APIModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class ProjectCreate(APIModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    github_owner: str | None = Field(default=None, min_length=1, max_length=100)
    github_repo: str | None = Field(default=None, min_length=1, max_length=100)
    source_roots: SourceRootList = Field(default_factory=lambda: ["."])
    exclude_paths: PathGlobList = Field(default_factory=list)
    fail_on: Severity = "error"

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _reject_control_characters(value)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str | None) -> str | None:
        return _reject_control_characters(value, TEXT_CONTROL_CHARACTERS)

    @field_validator("github_owner", "github_repo")
    @classmethod
    def normalize_github_name(cls, value: str | None) -> str | None:
        return _normalize_github_name(value)

    @model_validator(mode="after")
    def github_fields_are_paired(self) -> ProjectCreate:
        if (self.github_owner is None) != (self.github_repo is None):
            raise ValueError("github_owner and github_repo must be provided together")
        return self


class ProjectUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    github_owner: str | None = Field(default=None, min_length=1, max_length=100)
    github_repo: str | None = Field(default=None, min_length=1, max_length=100)
    source_roots: SourceRootList | None = None
    exclude_paths: PathGlobList | None = None
    fail_on: Severity | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return _reject_control_characters(value)

    @model_validator(mode="after")
    def required_fields_are_not_cleared(self) -> ProjectUpdate:
        for field in ("name", "source_roots", "exclude_paths", "fail_on"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str | None) -> str | None:
        return _reject_control_characters(value, TEXT_CONTROL_CHARACTERS)

    @field_validator("github_owner", "github_repo")
    @classmethod
    def normalize_github_name(cls, value: str | None) -> str | None:
        return _normalize_github_name(value)

    @model_validator(mode="after")
    def github_fields_are_updated_together(self) -> ProjectUpdate:
        github_fields = {"github_owner", "github_repo"}
        supplied = self.model_fields_set & github_fields
        if supplied and supplied != github_fields:
            raise ValueError("github_owner and github_repo must be updated together")
        if supplied and ((self.github_owner is None) != (self.github_repo is None)):
            raise ValueError("github_owner and github_repo must both be set or cleared")
        return self


class ProjectResponse(APIModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    github_owner: str | None
    github_repo: str | None
    source_roots: list[str]
    exclude_paths: list[str]
    fail_on: str
    created_at: datetime


# Rules are validated by the engine's own models. On create, the key may be
# left out (it is derived from the name) and rule_type defaults to
# forbidden_import, as in v0.2.


class ForbiddenImportRuleCreate(ForbiddenImportRule):
    rule_type: Literal["forbidden_import"] = "forbidden_import"
    key: RuleKey | None = None


class ForbiddenCallRuleCreate(ForbiddenCallRule):
    key: RuleKey | None = None


class NoRelativeImportRuleCreate(NoRelativeImportRule):
    key: RuleKey | None = None


class MaxFileLinesRuleCreate(MaxFileLinesRule):
    key: RuleKey | None = None


def _rule_type(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("rule_type", "forbidden_import")
    return getattr(value, "rule_type", None)


RuleCreate = Annotated[
    Annotated[ForbiddenImportRuleCreate, Tag("forbidden_import")]
    | Annotated[ForbiddenCallRuleCreate, Tag("forbidden_call")]
    | Annotated[NoRelativeImportRuleCreate, Tag("no_relative_import")]
    | Annotated[MaxFileLinesRuleCreate, Tag("max_file_lines")],
    Discriminator(_rule_type),
]


class RuleUpdate(BaseModel):
    """Fields to change; the merged rule is validated again as a whole.

    The rule type cannot change: delete the rule and create a new one.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: RuleKey | None = None
    name: DisplayName | None = None
    enabled: bool | None = None
    severity: Severity | None = None
    source_pattern: ModuleGlob | None = None
    exclude_paths: list[PathGlob] | None = None
    forbidden_imports: list[ModuleName] | None = None
    forbidden_calls: list[CallName] | None = None
    max_lines: int | None = None

    @model_validator(mode="after")
    def fields_are_not_cleared(self) -> RuleUpdate:
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class RuleResponse(APIModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    key: str
    name: str
    rule_type: str
    enabled: bool
    severity: str
    source_pattern: str
    exclude_paths: list[str]
    # Set for forbidden_import rules.
    forbidden_imports: list[str] | None
    # Set for forbidden_call rules.
    forbidden_calls: list[str] | None
    # Set for max_file_lines rules.
    max_lines: int | None
    created_at: datetime


class SourceFile(APIModel):
    path: str = Field(min_length=1, max_length=500)
    # Kept verbatim: stripping leading blank lines would shift reported line numbers.
    content: Annotated[
        str, StringConstraints(strip_whitespace=False, max_length=1_000_000)
    ]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        # The engine's own normalization, so "./app/x.py" and "app//x.py" are
        # both the module app.x everywhere.
        try:
            normalized = normalize_path(value)
        except PathError as exc:
            raise ValueError(str(exc)) from exc
        if not normalized.endswith(".py"):
            raise ValueError("must identify a Python file")
        return normalized


class AnalysisRequest(APIModel):
    files: list[SourceFile] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def paths_are_unique(self) -> AnalysisRequest:
        paths = [source.path for source in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("file paths must be unique")
        return self


class ViolationResponse(APIModel):
    rule_id: int
    rule_name: str
    path: str
    line: int
    column: int
    imported_module: str
    message: str


RunStatus = Literal["queued", "running", "completed", "failed"]
RunOutcome = Literal["pass", "fail", "incomplete", "not_applicable"]


class FileCountsResponse(APIModel):
    """``total = eligible + excluded + ignored``, ``eligible = analyzed + skipped``.

    Null where a run recorded before v0.3, or a failed run, has no count.
    """

    total: int | None
    eligible: int | None
    analyzed: int
    excluded: int | None
    skipped: int | None
    ignored: int | None


class RuleCountsResponse(APIModel):
    total: int | None
    enabled: int
    # Pairs of (analyzed file, enabled rule that applies to it).
    evaluations: int | None


class DiagnosticCountsResponse(APIModel):
    # Violations by severity, and files that could not be analyzed.
    errors: int
    warnings: int
    analysis_errors: int


class RunErrorResponse(APIModel):
    code: str
    message: str | None


class AnalysisRunSummaryResponse(APIModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    source: str
    # Whether the analysis ran: queued, running, completed or failed.
    status: RunStatus
    # What a completed run found; null for any other status.
    outcome: RunOutcome | None
    # True only for pass, false only for fail, otherwise null.
    passed: bool | None
    fail_on: str | None
    pull_number: int | None
    repository_owner: str | None
    repository_name: str | None
    base_sha: str | None
    head_sha: str | None
    files: FileCountsResponse = Field(validation_alias="file_counts")
    rules: RuleCountsResponse = Field(validation_alias="rule_counts")
    summary: DiagnosticCountsResponse = Field(validation_alias="diagnostic_counts")
    # Why a failed run did not complete, or a queued run's previous attempt.
    error: RunErrorResponse | None
    # Attempts made by the worker, and when a queued retry may start.
    attempts: int
    next_attempt_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    engine_version: str | None
    config_hash: str | None
    # The v0.2 fields: files.analyzed, rules.enabled and the number of violations.
    files_analyzed: int
    rules_evaluated: int
    violation_count: int
    created_at: datetime


class PersistedViolationResponse(APIModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    analysis_run_id: int
    # "violation" of a rule, or an "error" that kept a file from being analyzed.
    kind: str
    # The rule type of a violation, or the reason for an error.
    code: str
    severity: str
    rule_id: int | None
    # The rule's key and name when the run happened.
    rule_key: str | None
    rule_name: str | None
    path: str | None
    line: int | None
    column: int | None
    # Set for forbidden_import violations.
    imported_module: str | None
    message: str
    data: dict[str, Any]


class AnalysisRunResponse(AnalysisRunSummaryResponse):
    # The project settings and rules that were checked.
    config: dict[str, Any] | None = Field(validation_alias="config_snapshot")
    # Every diagnostic: violations, and errors for files that were not analyzed.
    violations: list[PersistedViolationResponse]


class PassRateResponse(APIModel):
    """``rate = passed / decided``, over the runs that have a verdict."""

    passed: int
    # Runs whose outcome is pass or fail.
    decided: int
    # Incomplete, not-applicable, failed and unfinished runs: not in the rate.
    without_verdict: int
    rate: float | None


class DurationResponse(APIModel):
    # Runs with a recorded duration; runs recorded by v0.2 have none.
    runs: int
    average_ms: float | None
    max_ms: int | None


class ProjectStatsResponse(APIModel):
    project_id: int
    runs: int
    by_status: dict[RunStatus, int]
    by_outcome: dict[RunOutcome, int]
    pass_rate: PassRateResponse
    # Totals over every run of the project.
    diagnostics: DiagnosticCountsResponse
    duration: DurationResponse
    # The ten newest runs, newest first.
    latest: list[AnalysisRunSummaryResponse]


class DiagnosticResponse(APIModel):
    kind: Literal["violation", "error"]
    code: str
    severity: str
    message: str
    path: str | None
    line: int | None
    column: int | None
    rule_key: str | None
    rule_name: str | None
    data: dict[str, Any]


class ResultSourceResponse(APIModel):
    kind: str
    repository_owner: str | None
    repository_name: str | None
    pull_number: int | None
    base_sha: str | None
    head_sha: str | None


class ResultRunResponse(APIModel):
    id: int
    project_id: int
    status: RunStatus
    error: RunErrorResponse | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    config_hash: str | None


class AnalysisResultDocument(APIModel):
    """A stored result in the engine's own format, as ``invariant check`` prints it.

    The fields up to ``diagnostics`` are those of the engine's result
    (``schema_version`` 1); ``source`` and ``run`` say where it came from.
    """

    schema_version: int
    engine_version: str | None
    outcome: RunOutcome | None
    passed: bool | None
    fail_on: str | None
    files: FileCountsResponse
    rules: RuleCountsResponse
    summary: DiagnosticCountsResponse
    diagnostics: list[DiagnosticResponse]
    source: ResultSourceResponse
    run: ResultRunResponse


class _StrictPayload(BaseModel):
    # Webhook JSON is never coerced: "12" or true is not a pull request number.
    model_config = ConfigDict(strict=True)


class _GitHubAccount(_StrictPayload):
    login: str = Field(min_length=1)


class _GitHubRepository(_StrictPayload):
    name: str = Field(min_length=1)
    owner: _GitHubAccount


class _GitHubPullRequest(_StrictPayload):
    number: int = Field(ge=1)


class PullRequestEvent(_StrictPayload):
    """The fields of a GitHub ``pull_request`` webhook payload that Invariant uses."""

    action: str
    repository: _GitHubRepository
    pull_request: _GitHubPullRequest


class ConfigImportRequest(BaseModel):
    # The file is used as written.
    content: str = Field(max_length=1_000_000)
    # merge keeps rules missing from the file; replace deletes them.
    mode: Literal["merge", "replace"] = "merge"
    # Report the changes without applying them.
    dry_run: bool = False


class SettingChange(APIModel):
    field: str
    before: Any
    after: Any


class ConfigImportPlan(APIModel):
    mode: Literal["merge", "replace"]
    dry_run: bool
    applied: bool
    project_changes: list[SettingChange]
    created: list[str]
    updated: list[str]
    deleted: list[str]
    unchanged: list[str]


class GitHubAnalysisRequest(APIModel):
    pull_number: int = Field(ge=1)


class WebhookResponse(APIModel):
    # queued: accepted for the worker; duplicate: a delivery seen before.
    status: Literal["ignored", "queued", "duplicate"]
    analysis_run_id: int | None = None
    # The outcome of a duplicate delivery's run, once it has one.
    outcome: RunOutcome | None = None
    detail: str | None = None
