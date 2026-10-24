"""The worker that runs queued GitHub analyses: ``python -m app.worker``.

Runs are the jobs. A worker claims the oldest queued run that is due, with a
lease, so that two workers never run the same job: the claim locks the row
and skips rows other workers hold. A worker that dies leaves its lease to
expire, and the run is queued again, or failed after its last attempt. A
transient GitHub error (the network, a rate limit, a server error) queues the
run again after a backoff that honours Retry-After; any other error fails it.

A worker records a result only while it still holds the run, so a worker that
lost its lease to another cannot overwrite that worker's result. Files it has
not downloaded within the job time limit make the run incomplete.

When pull request comments are enabled, a completed run's comment is
published right after the run, and retried on its own (``app.feedback``).
"""

import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.analysis_service import execute_github_analysis, fail_run
from app.config import Settings
from app.database import SessionLocal
from app.feedback import claim_feedback, hold, publish_feedback, wants_feedback
from app.github import GitHubAPIError, GitHubClient
from app.logs import configure_logging, run_context
from app.models import AnalysisRun

logger = logging.getLogger("app.worker")

GitHubFactory = Callable[[], AbstractContextManager[GitHubClient]]


@dataclass(frozen=True)
class Policy:
    max_attempts: int = 3
    lease: timedelta = timedelta(minutes=10)
    # Within the lease, so that a slow run is not taken over while it runs.
    job_time: timedelta = timedelta(minutes=5)
    retry_base: timedelta = timedelta(seconds=30)
    retry_max: timedelta = timedelta(hours=1)
    # Where the API is reachable, for the run link in pull request comments.
    public_url: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> Policy:
        lease = timedelta(seconds=settings.worker_lease_seconds)
        return cls(
            max_attempts=settings.worker_max_attempts,
            lease=lease,
            job_time=min(timedelta(seconds=settings.worker_job_seconds), lease * 0.8),
            retry_base=timedelta(seconds=settings.worker_retry_seconds),
            public_url=settings.public_url,
        )

    def retry_delay(self, attempt: int, retry_after: float | None) -> timedelta:
        """Twice as long after each attempt, but never before GitHub asked."""
        delay = self.retry_base * 2 ** (attempt - 1)
        if retry_after is not None:
            delay = max(delay, timedelta(seconds=retry_after))
        return min(delay, self.retry_max)


def _now() -> datetime:
    return datetime.now(UTC)


def _ms(began: float) -> int:
    return round((time.monotonic() - began) * 1000)


def recover_expired(db: Session, policy: Policy, now: datetime) -> int:
    """Queue again, or fail, the runs whose worker's lease has expired."""
    expired = db.scalars(
        select(AnalysisRun)
        .where(AnalysisRun.status == "running", AnalysisRun.lease_expires_at < now)
        .with_for_update(skip_locked=True)
    ).all()
    for run in expired:
        message = "The worker running the analysis stopped before it finished"
        holder, requeued = run.locked_by, run.attempts < policy.max_attempts
        if requeued:
            run.status = "queued"
            run.error_code, run.error_message = "worker_lost", message
            run.next_attempt_at = now
            run.lease_expires_at = run.locked_by = None
        else:
            fail_run(run, "worker_lost", message)
        logger.warning(
            "Run %s lost its worker %s; %s",
            run.id,
            holder,
            "queued again" if requeued else "failed",
            extra={"event": "run_worker_lost", "run_id": run.id, "requeued": requeued},
        )
    db.commit()
    return len(expired)


def claim(
    db: Session, worker_id: str, policy: Policy, now: datetime
) -> AnalysisRun | None:
    """Take the oldest queued run that is due, or None if there is none."""
    run = db.scalar(
        select(AnalysisRun)
        .where(
            AnalysisRun.status == "queued",
            or_(
                AnalysisRun.next_attempt_at.is_(None),
                AnalysisRun.next_attempt_at <= now,
            ),
        )
        .order_by(AnalysisRun.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if run is None:
        db.rollback()
        return None
    run.status = "running"
    run.attempts += 1
    run.locked_by = worker_id
    run.lease_expires_at = now + policy.lease
    run.next_attempt_at = None
    db.commit()
    return run


def _still_ours(db: Session, run: AnalysisRun, worker_id: str) -> bool:
    # Locks the row until the commit, so the lease cannot change in between.
    holder = db.execute(
        select(AnalysisRun.status, AnalysisRun.locked_by)
        .where(AnalysisRun.id == run.id)
        .with_for_update()
    ).one()
    return tuple(holder) == ("running", worker_id)


def process(
    db: Session,
    run: AnalysisRun,
    github_factory: GitHubFactory,
    policy: Policy,
    worker_id: str,
) -> None:
    """Execute a claimed run and record how it ended."""
    # A rollback forgets what the attempt changed, its start time included.
    started_at = _now()
    began = time.monotonic()
    deadline = began + policy.job_time.total_seconds()
    logger.info(
        "Run %s claimed, attempt %s",
        run.id,
        run.attempts,
        extra={"event": "run_claimed", "attempt": run.attempts, "worker": worker_id},
    )
    try:
        with github_factory() as github:
            execute_github_analysis(db, run, github, deadline=deadline)
            if not _still_ours(db, run, worker_id):
                db.rollback()
                logger.warning(
                    "Run %s was taken over; its result is dropped",
                    run.id,
                    extra={"event": "run_result_dropped"},
                )
                return
            run.lease_expires_at = None
            outcome = run.outcome
            publish = wants_feedback(run, github)
            if publish:
                # Due with the result, so a crash before publishing only delays it.
                run.feedback_status = "pending"
                hold(run, policy.lease.total_seconds(), _now())
            db.commit()
            logger.info(
                "Run %s completed: %s",
                run.id,
                outcome,
                extra={
                    "event": "run_completed",
                    "outcome": outcome,
                    "duration_ms": _ms(began),
                },
            )
            if publish:
                _publish(db, run, github, policy)
    except GitHubAPIError as exc:
        db.rollback()
        if not _still_ours(db, run, worker_id):
            db.rollback()
            return
        run.started_at = started_at
        if exc.transient and run.attempts < policy.max_attempts:
            delay = policy.retry_delay(run.attempts, exc.retry_after)
            run.status = "queued"
            run.error_code, run.error_message = exc.code, exc.message
            run.next_attempt_at = _now() + delay
            run.lease_expires_at = run.locked_by = None
            logger.info(
                "Run %s: %s, retrying in %s",
                run.id,
                exc.code,
                delay,
                extra={
                    "event": "run_retry_scheduled",
                    "error_code": exc.code,
                    "delay_seconds": delay.total_seconds(),
                },
            )
        else:
            fail_run(run, exc.code, exc.message)
            logger.warning(
                "Run %s failed: %s",
                run.id,
                exc.code,
                extra={"event": "run_failed", "error_code": exc.code},
            )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "Run %s failed unexpectedly",
            run.id,
            extra={"event": "run_failed", "error_code": "internal_error"},
        )
        if _still_ours(db, run, worker_id):
            run.started_at = started_at
            fail_run(run, "internal_error", "The analysis failed unexpectedly")
            db.commit()
        else:
            db.rollback()


def _publish(
    db: Session, run: AnalysisRun, github: GitHubClient, policy: Policy
) -> None:
    publish_feedback(
        db,
        run,
        github,
        public_url=policy.public_url,
        max_attempts=policy.max_attempts,
        retry_delay=policy.retry_delay,
    )


def run_worker(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    github_factory: GitHubFactory | None = None,
    policy: Policy | None = None,
    worker_id: str | None = None,
    stop: threading.Event | None = None,
    until_idle: bool = False,
) -> int:
    """Process runs until ``stop`` is set, or the queue is empty if ``until_idle``.

    Returns how many runs were processed.
    """
    settings = Settings()
    policy = policy or Policy.from_settings(settings)
    github_factory = github_factory or (lambda: GitHubClient(settings))
    worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    stop = stop or threading.Event()
    processed = 0
    while not stop.is_set():
        with session_factory() as db:
            now = _now()
            recover_expired(db, policy, now)
            run = claim(db, worker_id, policy, now)
            if run is not None:
                with run_context(run.id):
                    process(db, run, github_factory, policy, worker_id)
                processed += 1
                continue
            feedback = claim_feedback(db, policy.lease.total_seconds(), now)
            if feedback is not None:
                with run_context(feedback.id), github_factory() as github:
                    _publish(db, feedback, github, policy)
                continue
        if until_idle:
            break
        stop.wait(settings.worker_poll_seconds)
    return processed


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format, settings.secrets())
    stop = threading.Event()

    def shut_down(signum: int, _frame: object) -> None:
        # The run in progress finishes; no new one is claimed.
        logger.info("Stopping after the current run (signal %s)", signum)
        stop.set()

    signal.signal(signal.SIGTERM, shut_down)
    signal.signal(signal.SIGINT, shut_down)
    logger.info("Worker started")
    processed = run_worker(stop=stop)
    logger.info("Worker stopped after %s runs", processed)


if __name__ == "__main__":
    main()
