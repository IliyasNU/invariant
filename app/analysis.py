"""Run the shared engine on stored projects and rules."""

from dataclasses import dataclass
from typing import Any

from app.models import Project, Rule
from app.schemas import SourceFile, ViolationResponse
from invariant.config import (
    ForbiddenCallRule,
    ForbiddenImportRule,
    MaxFileLinesRule,
    NoRelativeImportRule,
    ProjectSettings,
    RuleConfig,
)
from invariant.engine import Source, analyze
from invariant.result import AnalysisResult

_RULE_MODELS = {
    "forbidden_import": ForbiddenImportRule,
    "forbidden_call": ForbiddenCallRule,
    "no_relative_import": NoRelativeImportRule,
    "max_file_lines": MaxFileLinesRule,
}


@dataclass
class SourceSyntaxError(Exception):
    path: str
    line: int | None
    column: int | None
    message: str


_COMMON_RULE_FIELDS = (
    "key",
    "name",
    "rule_type",
    "enabled",
    "severity",
    "source_pattern",
    "exclude_paths",
)


def rule_columns(rule: RuleConfig) -> dict[str, Any]:
    """The table columns that store a validated engine rule."""
    data = rule.model_dump()
    columns = {field: data.pop(field) for field in _COMMON_RULE_FIELDS}
    columns["name"] = rule.display_name
    columns["params"] = data
    return columns


def rule_data(rule: Rule) -> dict[str, Any]:
    """A stored rule as the plain data an engine rule is validated from."""
    return {
        **{field: getattr(rule, field) for field in _COMMON_RULE_FIELDS},
        "exclude_paths": list(rule.exclude_paths),
        **rule.params,
    }


def engine_rule(rule: Rule) -> RuleConfig:
    # Stored rules were validated when written; constructing them without
    # validation keeps rules saved under older checks usable.
    return _RULE_MODELS[rule.rule_type].model_construct(**rule_data(rule))


def project_settings(project: Project | None) -> ProjectSettings:
    if project is None:
        return ProjectSettings()
    return ProjectSettings.model_construct(
        source_roots=list(project.source_roots),
        exclude_paths=list(project.exclude_paths),
        fail_on=project.fail_on,
    )


def evaluate(
    files: list[SourceFile], rules: list[Rule], project: Project | None = None
) -> AnalysisResult:
    result = analyze(
        [Source(path=item.path, content=item.content) for item in files],
        [engine_rule(rule) for rule in rules],
        project_settings(project),
    )
    # The API has no incomplete result yet: a file that cannot be parsed
    # rejects the whole request.
    if result.errors:
        error = result.errors[0]
        raise SourceSyntaxError(
            path=error.path or "",
            line=error.line,
            column=error.column,
            message=error.message,
        )
    return result


def analyze_files(
    files: list[SourceFile], rules: list[Rule]
) -> list[ViolationResponse]:
    """The forbidden-import violations of ``files``, in the v0.2 order."""
    by_key = {rule.key: rule for rule in rules}
    result = evaluate(files, rules)
    violations = [
        ViolationResponse(
            rule_id=by_key[item.rule_key].id,
            rule_name=item.rule_name,
            path=item.path,
            line=item.line,
            column=item.column,
            imported_module=item.data["imported_module"],
            message=item.message,
        )
        for item in result.violations
        if item.code == "forbidden_import"
    ]
    return sorted(
        violations,
        key=lambda item: (item.path, item.line, item.column, item.rule_id),
    )
