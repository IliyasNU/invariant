"""Run the shared engine on API and GitHub input with the v0.2 result contract."""

from dataclasses import dataclass

from app.models import Rule
from app.schemas import SourceFile, ViolationResponse
from invariant.config import ForbiddenImportRule
from invariant.engine import Source, analyze


@dataclass
class SourceSyntaxError(Exception):
    path: str
    line: int | None
    column: int | None
    message: str


def _engine_rule(rule: Rule) -> ForbiddenImportRule:
    # Stored rules were validated when written; constructing them without
    # validation keeps rules saved under older checks usable.
    return ForbiddenImportRule.model_construct(
        rule_type="forbidden_import",
        key=f"rule-{rule.id}",
        name=rule.name,
        enabled=True,
        severity="error",
        source_pattern=rule.source_pattern,
        exclude_paths=[],
        forbidden_imports=list(rule.forbidden_imports),
    )


def analyze_files(
    files: list[SourceFile], rules: list[Rule]
) -> list[ViolationResponse]:
    applicable = [rule for rule in rules if rule.rule_type == "forbidden_import"]
    by_key = {f"rule-{rule.id}": rule for rule in applicable}
    result = analyze(
        [Source(path=item.path, content=item.content) for item in files],
        [_engine_rule(rule) for rule in applicable],
    )
    # v0.2 has no incomplete result: a file that cannot be parsed rejects the
    # whole request, as before.
    if result.errors:
        error = result.errors[0]
        raise SourceSyntaxError(
            path=error.path or "",
            line=error.line,
            column=error.column,
            message=error.message,
        )
    violations = [
        ViolationResponse(
            rule_id=by_key[item.rule_key].id,
            rule_name=by_key[item.rule_key].name,
            path=item.path,
            line=item.line,
            column=item.column,
            imported_module=item.data["imported_module"],
            message=item.message,
        )
        for item in result.violations
    ]
    return sorted(
        violations,
        key=lambda item: (item.path, item.line, item.column, item.rule_id),
    )
