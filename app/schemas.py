import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MODULE_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9_.*?\[\]-]+$")


class APIModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class ProjectCreate(APIModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class ProjectResponse(APIModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
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
    content: str = Field(max_length=1_000_000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("must be a relative path without '..'")
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


class AnalysisResponse(APIModel):
    project_id: int
    files_analyzed: int
    rules_evaluated: int
    violations: list[ViolationResponse]
