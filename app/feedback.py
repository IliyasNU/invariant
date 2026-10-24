"""One comment per pull request, kept current by the worker.

When a GitHub run completes and comments are enabled, its feedback is due.
The worker publishes it by editing Invariant's comment on the pull request,
or creating it the first time, and remembers which run the comment shows.
Publishing is retried on its own schedule, so a failure to comment never
changes the run.

A run is not published when it would mislead: when its commit is no longer
the pull request's head, or when the comment already shows a newer run. It
stays in the history; only the comment ignores it.
"""

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.github import GitHubAPIError, GitHubClient
from app.models import AnalysisRun, PullRequestComment

logger = logging.getLogger(__name__)

MARKER = "<!-- invariant:feedback -->"
MAX_ITEMS = 10
# Characters of a path or message shown, which keeps the comment far below
# GitHub's limit on its length.
MAX_TEXT = 300


def _now() -> datetime:
    return datetime.now(UTC)


def _code(text: str) -> str:
    """``text`` as a Markdown code span: no formatting, links or @mentions."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _shorten(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def feedback_body(run: AnalysisRun, public_url: str | None = None) -> str:
    """The comment for a completed run: commit, verdict, findings, limits, link."""
    commit = _code((run.head_sha or "unknown")[:12])
    warnings, errors = run.warning_count, run.error_count
    if run.outcome == "pass":
        headline = f"**Invariant: PASS** for {commit}"
        if warnings:
            headline += f" with {_plural(warnings, 'warning')}"
    elif run.outcome == "fail":
        blocking = errors + (warnings if run.fail_on == "warning" else 0)
        headline = (
            f"**Invariant: FAIL** for {commit}: "
            f"{_plural(blocking, 'violation')} at or above {run.fail_on}"
        )
        if blocking < run.violation_count:
            headline += f", {_plural(run.violation_count - blocking, 'warning')}"
    elif run.outcome == "incomplete":
        headline = (
            f"**Invariant: INCOMPLETE** for {commit}: "
            f"{_plural(run.analysis_error_count, 'problem')} kept files from being "
            "checked, so there is no verdict"
        )
    else:
        headline = (
            f"**Invariant: NOT APPLICABLE** for {commit}: no enabled rule applies "
            "to the changed Python files"
        )

    lines = [MARKER, headline, ""]
    for item in run.violations[:MAX_ITEMS]:
        where = _shorten(item.path or "pull request")
        if item.line:
            where += f":{item.line}"
        label = item.severity if item.kind == "violation" else "not analyzed"
        lines.append(f"- {_code(where)} {label}: {_code(_shorten(item.message))}")
    if len(run.violations) > MAX_ITEMS:
        lines.append(f"- … and {len(run.violations) - MAX_ITEMS} more")
    if run.violations:
        lines.append("")
    lines.append(
        f"Checked {_plural(run.files_analyzed, 'changed Python file')} against "
        f"{_plural(run.rules_evaluated, 'rule')}; {run.files_excluded or 0} "
        f"excluded, {run.files_skipped or 0} not analyzed. Invariant reads the "
        "code without running it, so dynamic imports and calls made through "
        "variables are not seen."
    )
    run_label = f"run {run.id}"
    if public_url:
        run_label = f"[{run_label}]({public_url.rstrip('/')}/analysis-runs/{run.id})"
    lines.append(f"Details: {run_label} · Invariant {run.engine_version}")
    return "\n".join(lines)


def wants_feedback(run: AnalysisRun, github: GitHubClient) -> bool:
    return bool(
        github.comment_enabled
        and run.status == "completed"
        and run.repository_owner
        and run.repository_name
        and run.pull_number is not None
    )


def _comment_record(db: Session, run: AnalysisRun) -> PullRequestComment:
    """The pull request's comment record, locked until the next commit."""
    statement = (
        select(PullRequestComment)
        .where(
            PullRequestComment.project_id == run.project_id,
            PullRequestComment.pull_number == run.pull_number,
        )
        .with_for_update()
    )
    record = db.scalar(statement)
    if record is None:
        try:
            with db.begin_nested():
                db.add(
                    PullRequestComment(
                        project_id=run.project_id, pull_number=run.pull_number
                    )
                )
        except IntegrityError:
            pass  # another worker created it first
        record = db.scalar(statement)
    assert record is not None
    return record


def claim_feedback(
    db: Session, hold_seconds: float, now: datetime
) -> AnalysisRun | None:
    """Take the oldest run whose feedback is due, holding it for a while.

    Holding it moves the next attempt into the future, so other workers leave
    it alone, and a worker that dies only delays it.
    """
    run = db.scalar(
        select(AnalysisRun)
        .where(
            AnalysisRun.feedback_status == "pending",
            or_(
                AnalysisRun.feedback_next_attempt_at.is_(None),
                AnalysisRun.feedback_next_attempt_at <= now,
            ),
        )
        .order_by(AnalysisRun.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if run is None:
        db.rollback()
        return None
    hold(run, hold_seconds, now)
    db.commit()
    return run


def hold(run: AnalysisRun, hold_seconds: float, now: datetime) -> None:
    run.feedback_attempts += 1
    run.feedback_next_attempt_at = now + timedelta(seconds=hold_seconds)


def publish_feedback(
    db: Session,
    run: AnalysisRun,
    github: GitHubClient,
    *,
    public_url: str | None,
    max_attempts: int,
    retry_delay: Callable[[int, float | None], timedelta],
) -> None:
    """Edit or create the pull request's comment for ``run``, then commit.

    ``retry_delay(attempt, retry_after)`` says how long to wait after a
    transient failure. An unexpected error gives up on this run's comment
    rather than stopping the worker.
    """
    owner, repo = run.repository_owner, run.repository_name
    assert owner and repo and run.pull_number is not None
    try:
        head = github.get_pull_request(owner, repo, run.pull_number).head_sha
        record = _comment_record(db, run)
        newer = record.analysis_run_id is not None and record.analysis_run_id > run.id
        if head != run.head_sha or newer:
            run.feedback_status = "stale"
            run.feedback_next_attempt_at = None
            db.commit()
            logger.info(
                "The comment for run %s is stale; not published",
                run.id,
                extra={
                    "event": "feedback_stale",
                    "reason": "newer_run" if newer else "head_moved",
                },
            )
            return
        body = feedback_body(run, public_url)
        created = record.comment_id is None or not github.update_comment(
            owner, repo, record.comment_id, body
        )
        if created:
            record.comment_id = github.create_comment(
                owner, repo, run.pull_number, body
            )
        record.analysis_run_id, record.head_sha = run.id, run.head_sha
        comment_id = record.comment_id
        run.feedback_status = "posted"
        run.feedback_error = run.feedback_next_attempt_at = None
        db.commit()
        logger.info(
            "The comment for run %s is posted",
            run.id,
            extra={
                "event": "feedback_posted",
                "comment_id": comment_id,
                "new_comment": created,
            },
        )
    except GitHubAPIError as exc:
        db.rollback()
        run.feedback_error = exc.code
        if exc.transient and run.feedback_attempts < max_attempts:
            delay = retry_delay(run.feedback_attempts, exc.retry_after)
            run.feedback_next_attempt_at = _now() + delay
            logger.info(
                "The comment for run %s: %s, retrying in %s",
                run.id,
                exc.code,
                delay,
                extra={
                    "event": "feedback_retry_scheduled",
                    "error_code": exc.code,
                    "delay_seconds": delay.total_seconds(),
                },
            )
        else:
            run.feedback_status = "failed"
            run.feedback_next_attempt_at = None
            logger.warning(
                "The comment for run %s failed: %s",
                run.id,
                exc.code,
                extra={"event": "feedback_failed", "error_code": exc.code},
            )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "The comment for run %s failed unexpectedly",
            run.id,
            extra={"event": "feedback_failed", "error_code": "internal_error"},
        )
        run.feedback_status, run.feedback_error = "failed", "internal_error"
        run.feedback_next_attempt_at = None
        db.commit()
