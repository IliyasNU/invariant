import posixpath
import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

MODULE_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9_.*?\[\]-]+$")
GITHUB_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


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
    description: str | None = Field(default=None, max_length=1000)
    github_owner: str | None = Field(default=None, min_length=1, max_length=100)
    github_repo: str | None = Field(default=None, min_length=1, max_length=100)

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
    created_at: datetime


class RuleCreate(APIModel):
    name: str = Field(min_length=1, max_length=120)
    rule_type: Literal["forbidden_import"] = "forbidden_import"
    source_pattern: str = Field(default="*", min_length=1, max_length=255)
    forbidden_imports: list[str] = Field(min_length=1, max_length=100)

    @field_validator("source_pattern")
    @classmethod
    def validate_source_pattern(cls, value: str) -> str:
        if not SOURCE_PATTERN.fullmatch(value):
            raise ValueError("must be a dotted module glob such as 'app.services.*'")
        return value

    @field_validator("forbidden_imports")
    @classmethod
    def validate_forbidden_imports(cls, value: list[str]) -> list[str]:
        if any(not MODULE_NAME.fullmatch(item) for item in value):
            raise ValueError("entries must be dotted Python module names")
        if len(set(value)) != len(value):
            raise ValueError("entries must be unique")
        return value


class RuleResponse(APIModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    rule_type: str
    source_pattern: str
    forbidden_imports: list[str]
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
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("must be a relative path without '..'")
        # "./app/x.py" and "app//x.py" must map to the module app.x.
        normalized = posixpath.normpath(normalized)
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


class AnalysisRunSummaryResponse(APIModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    source: str
    status: str
    passed: bool
    pull_number: int | None
    repository_owner: str | None
    repository_name: str | None
    files_analyzed: int
    rules_evaluated: int
    violation_count: int
    created_at: datetime


class PersistedViolationResponse(ViolationResponse):
    model_config = ConfigDict(from_attributes=True)

    id: int
    analysis_run_id: int
    rule_id: int | None
    line: int | None
    column: int | None
    imported_module: str | None


class AnalysisRunResponse(AnalysisRunSummaryResponse):
    violations: list[PersistedViolationResponse]


class GitHubAnalysisRequest(APIModel):
    pull_number: int = Field(ge=1)


class WebhookResponse(APIModel):
    status: Literal["ignored", "analyzed", "duplicate"]
    analysis_run_id: int | None = None
    detail: str | None = None
