"""Exports of recorded runs, without fetching source or re-running analysis."""

from typing import Any

from pydantic import ValidationError

from app.errors import APIError
from app.models import AnalysisRun
from invariant.config import InvariantConfig
from invariant.result import AnalysisResult, Diagnostic, FileCounts, Outcome
from invariant.sarif import to_sarif


def _unavailable() -> APIError:
    return APIError(
        409,
        "sarif_unavailable",
        "The run lacks a supported configuration snapshot or complete result metadata",
    )


def sarif_document(run: AnalysisRun) -> dict[str, Any]:
    """Render the stored configuration/diagnostics, including omitted counts.

    Old runs without snapshots remain available through the JSON history API.
    Exporting them as SARIF would invent rule metadata, so it is refused.
    """
    if run.status != "completed":
        raise APIError(
            409,
            "analysis_not_completed",
            "Only completed runs can be exported as SARIF",
        )
    files = run.file_counts
    if (
        run.config_snapshot is None
        or not run.engine_version
        or run.outcome is None
        or run.fail_on is None
        or any(value is None for value in files.values())
        or run.rules_total is None
        or run.evaluations is None
    ):
        raise _unavailable()
    try:
        config = InvariantConfig.model_validate(run.config_snapshot)
    except ValidationError as exc:
        raise _unavailable() from exc

    diagnostics = tuple(
        Diagnostic(
            kind=item.kind,
            code=item.code,
            severity=item.severity,
            message=item.message,
            path=item.path,
            line=item.line,
            column=item.column,
            rule_key=item.rule_key,
            rule_name=item.rule_name,
            data=item.data,
        )
        for item in run.violations
    )
    keys = {rule.key for rule in config.rules}
    if any(
        item.kind == "violation" and item.rule_key not in keys for item in diagnostics
    ):
        raise _unavailable()
    listed_errors = sum(
        item.kind == "violation" and item.severity == "error" for item in diagnostics
    )
    listed_warnings = sum(
        item.kind == "violation" and item.severity == "warning" for item in diagnostics
    )
    omitted_errors = run.error_count - listed_errors
    omitted_warnings = run.warning_count - listed_warnings
    if omitted_errors < 0 or omitted_warnings < 0:
        raise _unavailable()
    result = AnalysisResult(
        outcome=Outcome(run.outcome),
        fail_on=run.fail_on,
        files=FileCounts(**files),
        rules_total=run.rules_total,
        rules_enabled=run.rules_evaluated,
        evaluations=run.evaluations,
        diagnostics=diagnostics,
        omitted_errors=omitted_errors,
        omitted_warnings=omitted_warnings,
    )
    document = to_sarif(
        result,
        config.rules,
        engine_version=run.engine_version,
        source={
            "kind": run.source,
            "repository_owner": run.repository_owner,
            "repository_name": run.repository_name,
            "pull_number": run.pull_number,
            "base_sha": run.base_sha,
            "head_sha": run.head_sha,
        },
    )
    document["runs"][0]["properties"].update(
        analysis_run_id=run.id,
        project_id=run.project_id,
        config_hash=run.config_hash,
    )
    return document
