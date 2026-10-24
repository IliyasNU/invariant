"""Run analyses and record them, whatever their outcome.

Every attempt is recorded as a run. A completed run has an outcome: pass,
fail, incomplete (some file could not be analyzed) or not_applicable (nothing
was checked). A run that could not complete, such as a GitHub failure, is
recorded as failed with a safe error code and no outcome. A run and its
diagnostics are written in one transaction.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.analysis import engine_rule, evaluate, project_settings
from app.github import GitHubAPIError, GitHubClient, PullRequestMetadata
from app.models import AnalysisRun, AnalysisViolation, Project, Rule
from app.schemas import SourceFile
from invariant import __version__
from invariant.config import SCHEMA_VERSION, InvariantConfig
from invariant.engine import Unreadable
from invariant.result import RESULT_SCHEMA_VERSION, AnalysisResult, Diagnostic, Outcome

logger = logging.getLogger(__name__)


class ProjectGitHubNotConfigured(Exception):
    pass


@dataclass
class AnalysisFailed(Exception):
    """The analysis could not complete; ``run`` records the failure."""

    run: AnalysisRun
    error: GitHubAPIError


def _now() -> datetime:
    return datetime.now(UTC)


def _project_rules(db: Session, project: Project) -> list[Rule]:
    statement = select(Rule).where(Rule.project_id == project.id).order_by(Rule.id)
    return list(db.scalars(statement))


def config_snapshot(project: Project, rules: list[Rule]) -> tuple[dict, str]:
    """The configuration that is checked, and the SHA-256 of its canonical JSON."""
    config = InvariantConfig.model_construct(
        version=SCHEMA_VERSION,
        project=project_settings(project),
        rules=[engine_rule(rule) for rule in rules],
    )
    snapshot = config.model_dump(mode="json")
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return snapshot, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _new_run(
    project: Project,
    rules: list[Rule],
    *,
    source: str,
    started_at: datetime,
    pull_number: int | None,
    github_delivery_id: str | None,
    pull_request: PullRequestMetadata | None,
) -> AnalysisRun:
    snapshot, digest = config_snapshot(project, rules)
    finished_at = _now()
    return AnalysisRun(
        project_id=project.id,
        source=source,
        pull_number=pull_number,
        repository_owner=project.github_owner,
        repository_name=project.github_repo,
        github_delivery_id=github_delivery_id,
        base_sha=pull_request.base_sha if pull_request else None,
        head_sha=pull_request.head_sha if pull_request else None,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=round((finished_at - started_at).total_seconds() * 1000),
        engine_version=__version__,
        config_snapshot=snapshot,
        config_hash=digest,
    )


def _save(db: Session, run: AnalysisRun) -> AnalysisRun:
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _record_result(
    db: Session,
    project: Project,
    rules: list[Rule],
    result: AnalysisResult,
    **context,
) -> AnalysisRun:
    rule_ids = {rule.key: rule.id for rule in rules}
    violations = result.violations
    run = _new_run(project, rules, **context)
    run.status = "completed"
    run.outcome = result.outcome.value
    run.passed = result.passed
    run.fail_on = result.fail_on
    files = result.files
    run.files_total, run.files_eligible = files.total, files.eligible
    run.files_analyzed, run.files_excluded = files.analyzed, files.excluded
    run.files_skipped, run.files_ignored = files.skipped, files.ignored
    run.rules_total = result.rules_total
    run.rules_evaluated = result.rules_enabled
    run.evaluations = result.evaluations
    run.violation_count = len(violations)
    run.error_count = sum(item.severity == "error" for item in violations)
    run.warning_count = sum(item.severity == "warning" for item in violations)
    run.analysis_error_count = len(result.errors)
    run.violations = [
        AnalysisViolation(
            kind=item.kind,
            code=item.code,
            severity=item.severity,
            rule_id=rule_ids.get(item.rule_key) if item.rule_key else None,
            rule_key=item.rule_key,
            rule_name=item.rule_name,
            path=item.path,
            line=item.line,
            column=item.column,
            imported_module=item.data.get("imported_module"),
            message=item.message,
            data=item.data,
        )
        for item in result.diagnostics
    ]
    return _save(db, run)


def _record_failure(
    db: Session,
    project: Project,
    rules: list[Rule],
    error: GitHubAPIError,
    **context,
) -> AnalysisRun:
    run = _new_run(project, rules, **context)
    run.status = "failed"
    run.files_analyzed = run.rules_evaluated = run.violation_count = 0
    run.error_code = error.code
    # GitHubAPIError messages are written by Invariant, never copied from GitHub.
    run.error_message = error.message
    return _save(db, run)


def persist_analysis(
    db: Session,
    project: Project,
    files: list[SourceFile],
    *,
    source: str,
) -> AnalysisRun:
    started_at = _now()
    rules = _project_rules(db, project)
    return _record_result(
        db,
        project,
        rules,
        evaluate(files, rules, project),
        source=source,
        started_at=started_at,
        pull_number=None,
        github_delivery_id=None,
        pull_request=None,
    )


def analyze_github_pull_request(
    db: Session,
    project: Project,
    pull_number: int,
    github: GitHubClient,
    *,
    source: str,
    github_delivery_id: str | None = None,
) -> AnalysisRun:
    if not project.github_owner or not project.github_repo:
        raise ProjectGitHubNotConfigured

    started_at = _now()
    rules = _project_rules(db, project)
    pull_request = None
    context = {
        "source": source,
        "started_at": started_at,
        "pull_number": pull_number,
        "github_delivery_id": github_delivery_id,
    }
    try:
        pull_request = github.get_pull_request(
            project.github_owner, project.github_repo, pull_number
        )
        changed = github.get_changed_python_files(
            project.github_owner, project.github_repo, pull_request
        )
    except GitHubAPIError as exc:
        run = _record_failure(
            db, project, rules, exc, pull_request=pull_request, **context
        )
        raise AnalysisFailed(run=run, error=exc) from exc

    errors = []
    if changed.truncated:
        errors.append(
            Diagnostic(
                kind="error",
                code="file_list_truncated",
                severity="error",
                message=(
                    "GitHub listed fewer changed files than the pull request has, "
                    "so not every file could be checked"
                ),
            )
        )
    result = evaluate(
        changed.files,
        rules,
        project,
        unreadable=[Unreadable(item.path, item.reason) for item in changed.skipped],
        ignored=changed.ignored,
        errors=errors,
    )
    run = _record_result(
        db, project, rules, result, pull_request=pull_request, **context
    )
    # Only a verdict is announced: a pass would claim a check that did not run.
    if result.outcome in (Outcome.PASS, Outcome.FAIL):
        _post_feedback(github, project, run)
    return run


def _post_feedback(github: GitHubClient, project: Project, run: AnalysisRun) -> None:
    if (
        not github.comment_enabled
        or not project.github_owner
        or not project.github_repo
        or run.pull_number is None
    ):
        return

    violations = [item for item in run.violations if item.kind == "violation"]
    items = [
        f"- {violation.path}:{violation.line or '?'}: "
        + ("warning: " if violation.severity == "warning" else "")
        + violation.message
        for violation in violations[:10]
    ]
    suffix = "\n- …" if run.violation_count > 10 else ""
    if run.passed and not items:
        body = "Invariant: PASS"
    elif run.passed:
        body = (
            f"Invariant: PASS with {run.violation_count} warning(s):\n\n"
            + "\n".join(items)
            + suffix
        )
    else:
        body = (
            f"Invariant found {run.violation_count} architecture violation(s):\n\n"
            + "\n".join(items)
            + suffix
        )

    try:
        github.post_pull_request_comment(
            project.github_owner, project.github_repo, run.pull_number, body
        )
    except GitHubAPIError as exc:
        logger.warning("Could not post optional GitHub PR feedback: %s", exc.code)


def result_document(run: AnalysisRun) -> dict[str, Any]:
    """A stored run in the engine's result format, plus where it came from."""
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "engine_version": run.engine_version,
        "outcome": run.outcome,
        "passed": run.passed,
        "fail_on": run.fail_on,
        "files": run.file_counts,
        "rules": run.rule_counts,
        "summary": run.diagnostic_counts,
        "diagnostics": [
            {
                "kind": item.kind,
                "code": item.code,
                "severity": item.severity,
                "message": item.message,
                "path": item.path,
                "line": item.line,
                "column": item.column,
                "rule_key": item.rule_key,
                "rule_name": item.rule_name,
                "data": item.data,
            }
            for item in run.violations
        ],
        "source": {
            "kind": run.source,
            "repository_owner": run.repository_owner,
            "repository_name": run.repository_name,
            "pull_number": run.pull_number,
            "base_sha": run.base_sha,
            "head_sha": run.head_sha,
        },
        "run": {
            "id": run.id,
            "project_id": run.project_id,
            "status": run.status,
            "error": run.error,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "duration_ms": run.duration_ms,
            "config_hash": run.config_hash,
        },
    }


def load_analysis_run(db: Session, analysis_run_id: int) -> AnalysisRun | None:
    statement = (
        select(AnalysisRun)
        .options(selectinload(AnalysisRun.violations))
        .where(AnalysisRun.id == analysis_run_id)
    )
    return db.scalar(statement)
