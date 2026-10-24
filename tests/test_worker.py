import os
import threading
from datetime import UTC, datetime, timedelta
from typing import Self

import pytest
from sqlalchemy.exc import IntegrityError

from app.analysis_service import enqueue_github_analysis
from app.database import SessionLocal
from app.github import ChangedPythonFiles, GitHubAPIError, PullRequestMetadata
from app.models import AnalysisRun, Project, Rule
from app.schemas import SourceFile
from app.worker import Policy, claim, process, recover_expired, run_worker

ON_POSTGRESQL = os.environ.get("INVARIANT_TEST_DATABASE_URL", "").startswith(
    "postgresql"
)
RATE_LIMITED = GitHubAPIError(
    429, "github_rate_limited", "GitHub API rate limit exceeded", transient=True
)
NOT_FOUND = GitHubAPIError(404, "github_not_found", "Not found")


class FakeGitHub:
    """Answers with ``errors`` in turn, then with one clean changed file."""

    comment_enabled = False

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    def get_pull_request(self, owner, repo, pull_number) -> PullRequestMetadata:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return PullRequestMetadata(number=pull_number, head_sha="a" * 40)

    def get_changed_python_files(self, owner, repo, pull_request, **options):
        return ChangedPythonFiles(
            files=[SourceFile(path="app/x.py", content="x = 1\n")],
            skipped=[],
            truncated=False,
            ignored=0,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def queue_runs(count: int = 1) -> list[int]:
    with SessionLocal() as db:
        project = Project(name="Invariant", github_owner="o", github_repo="r")
        db.add(project)
        db.flush()
        db.add(
            Rule(
                project_id=project.id,
                key="domain",
                name="Domain",
                rule_type="forbidden_import",
                enabled=True,
                severity="error",
                source_pattern="*",
                exclude_paths=[],
                params={"forbidden_imports": ["app.infrastructure"]},
            )
        )
        db.commit()
        return [
            enqueue_github_analysis(db, project, 7, source="github_pr").id
            for _ in range(count)
        ]


def load(run_id: int) -> AnalysisRun:
    with SessionLocal() as db:
        return db.get(AnalysisRun, run_id)


def work(github: FakeGitHub, **policy) -> int:
    return run_worker(
        github_factory=lambda: github, policy=Policy(**policy), until_idle=True
    )


def make_due(run_id: int) -> None:
    with SessionLocal() as db:
        db.get(AnalysisRun, run_id).next_attempt_at = datetime.now(UTC)
        db.commit()


def test_a_transient_error_is_retried_after_a_backoff():
    [run_id] = queue_runs()
    github = FakeGitHub(RATE_LIMITED)

    before = datetime.now(UTC)
    assert work(github) == 1

    waiting = load(run_id)
    assert (waiting.status, waiting.attempts, waiting.outcome) == ("queued", 1, None)
    assert (waiting.error_code, waiting.locked_by) == ("github_rate_limited", None)
    wait = waiting.next_attempt_at.replace(tzinfo=UTC) - before
    assert timedelta(seconds=29) < wait < timedelta(seconds=35)
    # Not due yet: an idle worker leaves it alone.
    assert work(github) == 0

    make_due(run_id)
    assert work(github) == 1
    done = load(run_id)
    assert (done.status, done.outcome, done.attempts) == ("completed", "pass", 2)
    assert (done.error_code, done.error_message, done.next_attempt_at) == (
        None,
        None,
        None,
    )


def test_the_backoff_doubles_and_honours_retry_after():
    policy = Policy(retry_base=timedelta(seconds=30), retry_max=timedelta(minutes=5))

    assert [policy.retry_delay(attempt, None).seconds for attempt in (1, 2, 3)] == [
        30,
        60,
        120,
    ]
    assert policy.retry_delay(1, 90).seconds == 90
    assert policy.retry_delay(3, 10).seconds == 120  # never sooner than the backoff
    assert policy.retry_delay(10, None) == timedelta(minutes=5)
    assert policy.retry_delay(1, 86_400) == timedelta(minutes=5)
    # Too long for a timedelta: capped, not an OverflowError.
    assert policy.retry_delay(1, 1e15) == timedelta(minutes=5)


def test_a_retry_after_too_long_for_a_timedelta_does_not_stop_the_worker():
    # A Retry-After past timedelta's range raised OverflowError out of the
    # worker's error handler, which stopped the worker with the run held.
    [run_id] = queue_runs()
    error = GitHubAPIError(
        429,
        "github_rate_limited",
        "GitHub API rate limit exceeded",
        transient=True,
        retry_after=1e15,
    )

    before = datetime.now(UTC)
    assert work(FakeGitHub(error), retry_max=timedelta(hours=1)) == 1

    waiting = load(run_id)
    assert (waiting.status, waiting.locked_by) == ("queued", None)
    wait = waiting.next_attempt_at.replace(tzinfo=UTC) - before
    assert timedelta(minutes=59) < wait <= timedelta(hours=1, seconds=5)


def test_attempts_are_limited():
    [run_id] = queue_runs()
    github = FakeGitHub(RATE_LIMITED, RATE_LIMITED, RATE_LIMITED)

    work(github, max_attempts=2)
    make_due(run_id)
    work(github, max_attempts=2)

    failed = load(run_id)
    assert (failed.status, failed.attempts, failed.error_code) == (
        "failed",
        2,
        "github_rate_limited",
    )
    assert failed.finished_at is not None
    assert github.calls == 2


@pytest.mark.parametrize(
    "error",
    [
        NOT_FOUND,
        GitHubAPIError(502, "github_authentication_failed", "Rejected"),
        GitHubAPIError(502, "github_invalid_response", "Invalid"),
    ],
)
def test_a_permanent_error_fails_at_once(error):
    [run_id] = queue_runs()

    work(FakeGitHub(error), max_attempts=5)

    failed = load(run_id)
    assert (failed.status, failed.attempts, failed.error_code) == (
        "failed",
        1,
        error.code,
    )


def test_an_unexpected_error_fails_the_run_without_details():
    [run_id] = queue_runs()

    work(FakeGitHub(RuntimeError("token ghp_secret in a traceback")))

    failed = load(run_id)
    assert (failed.status, failed.error_code) == ("failed", "internal_error")
    assert "ghp_secret" not in failed.error_message


def test_a_run_whose_worker_died_is_queued_again_then_failed():
    [run_id] = queue_runs()
    policy = Policy(max_attempts=2, lease=timedelta(minutes=10))
    now = datetime.now(UTC)
    with SessionLocal() as db:
        assert claim(db, "dead-worker", policy, now).id == run_id
    # Ten minutes later the lease has run out.
    later = now + timedelta(minutes=11)
    with SessionLocal() as db:
        assert recover_expired(db, policy, now + timedelta(minutes=9)) == 0
        assert recover_expired(db, policy, later) == 1

    retried = load(run_id)
    assert (retried.status, retried.error_code, retried.locked_by) == (
        "queued",
        "worker_lost",
        None,
    )
    with SessionLocal() as db:
        assert claim(db, "dead-worker", policy, later).attempts == 2
    with SessionLocal() as db:
        assert recover_expired(db, policy, later + timedelta(minutes=11)) == 1
    assert (load(run_id).status, load(run_id).error_code) == ("failed", "worker_lost")


def test_a_worker_that_lost_its_lease_does_not_record_a_result():
    [run_id] = queue_runs()
    policy = Policy()
    now = datetime.now(UTC)
    with SessionLocal() as db:
        run = claim(db, "slow-worker", policy, now)
        # Meanwhile the lease expired and another worker took the run.
        with SessionLocal() as other:
            taken = other.get(AnalysisRun, run_id)
            taken.locked_by = "new-worker"
            other.commit()

        process(db, run, lambda: FakeGitHub(), policy, "slow-worker")

    current = load(run_id)
    assert (current.status, current.locked_by, current.outcome) == (
        "running",
        "new-worker",
        None,
    )


def test_a_stopped_worker_claims_nothing():
    queue_runs()
    stop = threading.Event()
    stop.set()

    assert run_worker(github_factory=lambda: FakeGitHub(), stop=stop) == 0


@pytest.mark.skipif(not ON_POSTGRESQL, reason="row locks need PostgreSQL")
def test_concurrent_workers_never_run_the_same_job():
    run_ids = queue_runs(12)
    claimed: dict[str, list[int]] = {}
    barrier = threading.Barrier(4)

    def worker(name: str) -> None:
        barrier.wait()
        mine = claimed.setdefault(name, [])
        while True:
            with SessionLocal() as db:
                run = claim(db, name, Policy(), datetime.now(UTC))
                if run is None:
                    return
                mine.append(run.id)

    threads = [threading.Thread(target=worker, args=(f"w{n}",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    everything = [run_id for runs in claimed.values() for run_id in runs]
    assert sorted(everything) == run_ids  # each run claimed exactly once


@pytest.mark.skipif(not ON_POSTGRESQL, reason="concurrent inserts need PostgreSQL")
def test_concurrent_identical_deliveries_queue_one_job():
    with SessionLocal() as db:
        project = Project(name="Invariant", github_owner="o", github_repo="r")
        db.add(project)
        db.commit()
        project_id = project.id
    barrier = threading.Barrier(5)
    outcomes: list[str] = []

    def deliver() -> None:
        with SessionLocal() as db:
            project = db.get(Project, project_id)
            barrier.wait()
            try:
                enqueue_github_analysis(
                    db,
                    project,
                    7,
                    source="github_webhook",
                    github_delivery_id="same-delivery",
                )
                outcomes.append("queued")
            except IntegrityError:
                outcomes.append("duplicate")

    threads = [threading.Thread(target=deliver) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["duplicate"] * 4 + ["queued"]
