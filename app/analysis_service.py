"""Run analyses and record them, whatever their outcome.

Every analysis is a run. A completed run has an outcome: pass, fail,
incomplete (some file could not be analyzed) or not_applicable (nothing was
checked). Direct analyses complete at once; GitHub analyses are queued and run
by the worker (``app.worker``), which records a run that could not complete as
failed, with a safe error code and no outcome. A run and its diagnostics are
written in one transaction.
"""

import hashlib
import json
import logging
import time
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
from invariant.result import RESULT_SCHEMA_VERSION, AnalysisResult, Diagnostic

logger = logging.getLogger(__name__)


class ProjectGitHubNotConfigured(Exception):
    pass


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


def _complete(
    run: AnalysisRun,
    project: Project,
    rules: list[Rule],
    result: AnalysisResult,
    *,
    started_at: datetime,
    pull_request: PullRequestMetadata | None = None,
) -> None:
    """Record ``result`` in ``run``, with what was checked and when."""
    snapshot, digest = config_snapshot(project, rules)
    finished_at = _now()
    run.repository_owner = project.github_owner
    run.repository_name = project.github_repo
    if pull_request is not None:
        run.base_sha, run.head_sha = pull_request.base_sha, pull_request.head_sha
    run.started_at, run.finished_at = started_at, finished_at
    run.duration_ms = round((finished_at - started_at).total_seconds() * 1000)
    run.engine_version = __version__
    run.config_snapshot, run.config_hash = snapshot, digest
    run.status = "completed"
    run.error_code = run.error_message = None
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
    violations = result.violations
    run.violation_count = len(violations)
    run.error_count = sum(item.severity == "error" for item in violations)
    run.warning_count = sum(item.severity == "warning" for item in violations)
    run.analysis_error_count = len(result.errors)
    rule_ids = {rule.key: rule.id for rule in rules}
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


def fail_run(run: AnalysisRun, code: str, message: str) -> None:
    """Record that ``run`` could not complete. ``message`` is Invariant's own."""
    run.status = "failed"
    run.outcome = run.passed = None
    run.error_code, run.error_message = code, message
    run.finished_at = _now()
    if run.started_at is not None:
        run.duration_ms = round(
            (run.finished_at - run.started_at).total_seconds() * 1000
        )
    run.lease_expires_at = run.locked_by = run.next_attempt_at = None


def _save(db: Session, run: AnalysisRun) -> AnalysisRun:
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def persist_analysis(
    db: Session,
    project: Project,
    files: list[SourceFile],
    *,
    source: str,
) -> AnalysisRun:
    started_at = _now()
    rules = _project_rules(db, project)
    run = AnalysisRun(project_id=project.id, source=source, attempts=1)
    _complete(
        run, project, rules, evaluate(files, rules, project), started_at=started_at
    )
    _save(db, run)
    logger.info(
        "Run %s completed: %s",
        run.id,
        run.outcome,
        extra={
            "event": "run_completed",
            "run_id": run.id,
            "source": source,
            "outcome": run.outcome,
            "duration_ms": run.duration_ms,
        },
    )
    return run


def enqueue_github_analysis(
    db: Session,
    project: Project,
    pull_number: int,
    *,
    source: str,
    github_delivery_id: str | None = None,
) -> AnalysisRun:
    """Accept a pull request analysis durably; the worker runs it.

    A repeated ``github_delivery_id`` raises IntegrityError: deliveries are
    unique.
    """
    if not project.github_owner or not project.github_repo:
        raise ProjectGitHubNotConfigured
    run = AnalysisRun(
        project_id=project.id,
        source=source,
        status="queued",
        pull_number=pull_number,
        repository_owner=project.github_owner,
        repository_name=project.github_repo,
        github_delivery_id=github_delivery_id,
        files_analyzed=0,
        rules_evaluated=0,
        violation_count=0,
    )
    _save(db, run)
    logger.info(
        "Run %s queued for pull request %s",
        run.id,
        pull_number,
        extra={
            "event": "run_queued",
            "run_id": run.id,
            "source": source,
            "pull_number": pull_number,
            "github_delivery_id": github_delivery_id,
        },
    )
    return run


def execute_github_analysis(
    db: Session,
    run: AnalysisRun,
    github: GitHubClient,
    *,
    deadline: float | None = None,
) -> None:
    """Analyze the pull request of a claimed run and record the result in it.

    Files not downloaded by ``deadline`` (a ``time.monotonic()`` value) make the
    run incomplete. Nothing is committed: the worker commits only while it
    still holds the run. A GitHubAPIError propagates, for the worker to retry
    or fail the run.
    """
    started_at = _now()
    run.started_at = started_at
    project = db.get(Project, run.project_id)
    if project is None or not project.github_owner or not project.github_repo:
        raise GitHubAPIError(
            status_code=409,
            code="github_not_configured",
            message="Project has no GitHub owner/repository configuration",
        )
    rules = _project_rules(db, project)
    assert run.pull_number is not None
    began = time.monotonic()
    pull_request = github.get_pull_request(
        project.github_owner, project.github_repo, run.pull_number
    )
    changed = github.get_changed_python_files(
        project.github_owner, project.github_repo, pull_request, deadline=deadline
    )
    logger.info(
        "Pull request %s fetched at %s",
        run.pull_number,
        pull_request.head_sha[:12],
        extra={
            "event": "pull_request_fetched",
            "head_sha": pull_request.head_sha,
            "files": len(changed.files),
            "skipped": len(changed.skipped),
            "truncated": changed.truncated,
            "duration_ms": round((time.monotonic() - began) * 1000),
        },
    )
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
    began = time.monotonic()
    result = evaluate(
        changed.files,
        rules,
        project,
        unreadable=[Unreadable(item.path, item.reason) for item in changed.skipped],
        ignored=changed.ignored,
        errors=errors,
    )
    logger.info(
        "Analysis finished: %s",
        result.outcome.value,
        extra={
            "event": "analysis_finished",
            "outcome": result.outcome.value,
            "diagnostics": len(result.diagnostics),
            "duration_ms": round((time.monotonic() - began) * 1000),
        },
    )
    _complete(
        run, project, rules, result, started_at=started_at, pull_request=pull_request
    )


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
