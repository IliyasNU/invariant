"""Render engine results as SARIF 2.1.0 without changing their outcome."""

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any
from urllib.parse import quote

from invariant import __version__
from invariant.config import RuleConfig
from invariant.paths import PathError, normalize_path
from invariant.result import AnalysisResult, Diagnostic

SARIF_SCHEMA = (
    "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/"
    "sarif-schema-2.1.0.json"
)


def _locations(item: Diagnostic) -> list[dict[str, Any]]:
    if item.path is None:
        return []
    try:
        path = normalize_path(item.path)
    except PathError:
        # An invalid source path is still explained in the notification, but
        # must not become a misleading absolute or traversal URI in a report.
        return []
    location: dict[str, Any] = {"artifactLocation": {"uri": quote(path, safe="/")}}
    if item.line is not None and item.line > 0:
        region = {"startLine": item.line}
        if item.column is not None and item.column > 0:
            region["startColumn"] = item.column
        location["region"] = region
    return [{"physicalLocation": location}]


def to_sarif(
    result: AnalysisResult,
    rules: Sequence[RuleConfig],
    *,
    source: dict[str, Any] | None = None,
    engine_version: str | None = None,
) -> dict[str, Any]:
    """A portable report with violations as results and errors as notifications.

    Paths stay relative to the checked project, percent-encoded as URI paths.
    Rule keys are the stable SARIF rule IDs; disabled and unmatched rules stay
    in the metadata as well. Input errors make the invocation unsuccessful,
    even when a blocking violation makes the engine outcome ``fail``.
    A capped result retains full severity totals and explicitly announces the
    omitted violations. Stored runs can supply their producing engine version.
    """
    ordered = sorted(rules, key=lambda rule: rule.key)
    indices = {rule.key: index for index, rule in enumerate(ordered)}
    descriptors = [
        {
            "id": rule.key,
            "shortDescription": {"text": rule.display_name},
            "defaultConfiguration": {"level": rule.severity, "enabled": rule.enabled},
            "properties": {"rule_type": rule.rule_type, "tags": ["architecture"]},
        }
        for rule in ordered
    ]
    results = []
    for item in result.violations:
        finding: dict[str, Any] = {
            "ruleId": item.rule_key,
            "ruleIndex": indices[item.rule_key],
            "level": item.severity,
            "message": {"text": item.message},
            "properties": item.data,
        }
        if locations := _locations(item):
            finding["locations"] = locations
        results.append(finding)

    invocation: dict[str, Any] = {"executionSuccessful": not result.errors}
    notifications = []
    for item in result.errors:
        notification: dict[str, Any] = {
            "level": "error",
            "message": {"text": item.message},
            "properties": {"code": item.code, "path": item.path, **item.data},
        }
        if locations := _locations(item):
            notification["locations"] = locations
        notifications.append(notification)

    properties: dict[str, Any] = {
        "outcome": result.outcome.value,
        "fail_on": result.fail_on,
        "files": asdict(result.files),
        "summary": {
            "errors": result.error_count,
            "warnings": result.warning_count,
            "analysis_errors": len(result.errors),
        },
    }
    omitted = result.omitted_errors + result.omitted_warnings
    if omitted:
        properties["omitted_violations"] = {
            "errors": result.omitted_errors,
            "warnings": result.omitted_warnings,
        }
        properties["diagnostics_truncated"] = True
        notifications.append(
            {
                "level": "warning",
                "message": {
                    "text": (
                        f"The report omits {omitted} architecture violation(s) "
                        "because the diagnostic limit was reached; summary "
                        "totals include the omitted violations."
                    )
                },
                "properties": {"code": "diagnostics_truncated"},
            }
        )
    if notifications:
        invocation["toolExecutionNotifications"] = notifications
    if source is not None:
        properties["source"] = source
    return {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Invariant",
                        "version": __version__
                        if engine_version is None
                        else engine_version,
                        "informationUri": "https://github.com/IliyasNU/invariant",
                        "rules": descriptors,
                    }
                },
                "columnKind": "unicodeCodePoints",
                "invocations": [invocation],
                "results": results,
                "properties": properties,
            }
        ],
    }
