import os
import re
import threading
import time
from datetime import UTC, datetime
from typing import Self

import pytest

import app.feedback
from app.analysis_service import enqueue_github_analysis
from app.database import SessionLocal
from app.feedback import MAX_TEXT, feedback_body, publish_feedback
from app.github import ChangedPythonFiles, GitHubAPIError, PullRequestMetadata
from app.models import AnalysisRun, AnalysisViolation, Project, PullRequestComment, Rule
from app.schemas import SourceFile
from app.worker import Policy, run_worker

ON_POSTGRESQL = os.environ.get("INVARIANT_TEST_DATABASE_URL", "").startswith(
    "postgresql"
)
HEAD_A = "a" * 40
HEAD_B = "b" * 40
VIOLATION = "import app.infrastructure\n"


class CommentingGitHub:
    """A pull request whose head is ``heads[0]`` (then the next, per call)."""

    comment_enabled = True

    def __init__(self, *heads: str) -> None:
        self.heads = list(heads or [HEAD_A])
        self.content = VIOLATION
        self.comments: dict[int, str] = {}
        self.created = self.updated = 0
        self.deleted: set[int] = set()
        self.errors: list[GitHubAPIError] = []

    def _head(self) -> str:
        return self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]

    def get_pull_request(self, owner, repo, pull_number) -> PullRequestMetadata:
        return PullRequestMetadata(number=pull_number, head_sha=self._head())

    def get_changed_python_files(self, owner, repo, pull_request, **options):
        return ChangedPythonFiles(
            files=[SourceFile(path="app/domain/a.py", content=self.content)],
            skipped=[],
            truncated=False,
            ignored=0,
        )

    def create_comment(self, owner, repo, pull_number, body) -> int:
        if self.errors:
            raise self.errors.pop(0)
        self.created += 1
        comment_id = 100 + self.created
        self.comments[comment_id] = body
        return comment_id

    def update_comment(self, owner, repo, comment_id, body) -> bool:
        if self.errors:
            raise self.errors.pop(0)
        if comment_id in self.deleted:
            return False
        self.updated += 1
        self.comments[comment_id] = body
        return True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def project_with_rule() -> int:
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
                source_pattern="app.domain.*",
                exclude_paths=[],
                params={"forbidden_imports": ["app.infrastructure"]},
            )
        )
        db.commit()
        return project.id


def queue(project_id: int) -> int:
    with SessionLocal() as db:
        project = db.get(Project, project_id)
        return enqueue_github_analysis(db, project, 7, source="github_pr").id


def work(github, **policy) -> int:
    return run_worker(
        github_factory=lambda: github, policy=Policy(**policy), until_idle=True
    )


def run(run_id: int) -> AnalysisRun:
    with SessionLocal() as db:
        return db.get(AnalysisRun, run_id)


def record(project_id: int) -> PullRequestComment:
    with SessionLocal() as db:
        return db.query(PullRequestComment).filter_by(project_id=project_id).one()


def make_feedback_due(run_id: int) -> None:
    with SessionLocal() as db:
        db.get(AnalysisRun, run_id).feedback_next_attempt_at = datetime.now(UTC)
        db.commit()


def test_one_comment_is_edited_for_each_new_commit():
    project_id = project_with_rule()
    github = CommentingGitHub(HEAD_A)
    first = queue(project_id)
    work(github)
    github.heads = [HEAD_B]
    github.content = "x = 1\n"
    second = queue(project_id)
    work(github)

    assert (github.created, github.updated) == (1, 1)
    [body] = github.comments.values()
    assert body.splitlines()[1] == f"**Invariant: PASS** for `{HEAD_B[:12]}`"
    assert (run(first).feedback_status, run(second).feedback_status) == (
        "posted",
        "posted",
    )
    stored = record(project_id)
    assert (stored.comment_id, stored.analysis_run_id, stored.head_sha) == (
        101,
        second,
        HEAD_B,
    )


def test_a_deleted_comment_is_written_again():
    project_id = project_with_rule()
    github = CommentingGitHub(HEAD_A)
    queue(project_id)
    work(github)
    github.deleted.add(101)

    queue(project_id)
    work(github)

    assert github.created == 2
    assert record(project_id).comment_id == 102


def test_a_run_whose_commit_is_no_longer_the_head_is_not_published():
    project_id = project_with_rule()
    # The analysis sees HEAD_A; by the time its comment would be written, the
    # pull request is at HEAD_B.
    github = CommentingGitHub(HEAD_A, HEAD_B)
    run_id = queue(project_id)

    work(github)

    stale = run(run_id)
    assert (stale.status, stale.outcome, stale.head_sha) == (
        "completed",
        "fail",
        HEAD_A,
    )
    assert stale.feedback_status == "stale"
    assert github.comments == {}


def test_an_older_run_never_replaces_a_newer_ones_comment():
    project_id = project_with_rule()
    github = CommentingGitHub(HEAD_A)
    github.errors = [
        GitHubAPIError(503, "github_unavailable", "GitHub could not be reached", True)
    ]
    older = queue(project_id)
    work(github)
    assert run(older).feedback_status == "pending"  # will be retried

    newer = queue(project_id)
    work(github)
    make_feedback_due(older)
    work(github)

    assert (run(older).feedback_status, run(newer).feedback_status) == (
        "stale",
        "posted",
    )
    assert record(project_id).analysis_run_id == newer
    assert github.created == 1


def test_publishing_is_retried_on_its_own_and_never_changes_the_run(client):
    project_id = project_with_rule()
    github = CommentingGitHub(HEAD_A)
    github.errors = [
        GitHubAPIError(429, "github_rate_limited", "Rate limited", True, 120.0)
    ]
    run_id = queue(project_id)

    work(github)
    waiting = run(run_id)
    assert (waiting.status, waiting.outcome) == ("completed", "fail")
    assert (waiting.feedback_status, waiting.feedback_error) == (
        "pending",
        "github_rate_limited",
    )
    wait = waiting.feedback_next_attempt_at.replace(tzinfo=UTC) - datetime.now(UTC)
    assert wait.total_seconds() > 100  # GitHub asked for two minutes

    feedback = client.get(f"/analysis-runs/{run_id}").json()["feedback"]
    assert (feedback["status"], feedback["attempts"], feedback["error"]) == (
        "pending",
        1,
        "github_rate_limited",
    )
    assert feedback["next_attempt_at"] is not None

    make_feedback_due(run_id)
    work(github)
    posted = run(run_id)
    assert (posted.feedback_status, posted.feedback_attempts) == ("posted", 2)
    assert posted.feedback_error is None
    assert client.get(f"/analysis-runs/{run_id}").json()["feedback"] == {
        "status": "posted",
        "attempts": 2,
        "next_attempt_at": None,
        "error": None,
    }


def test_a_permanent_publishing_error_gives_up_without_failing_the_run():
    project_id = project_with_rule()
    github = CommentingGitHub(HEAD_A)
    github.errors = [GitHubAPIError(502, "github_authentication_failed", "Rejected")]
    run_id = queue(project_id)

    work(github)

    done = run(run_id)
    assert (done.status, done.outcome) == ("completed", "fail")
    assert (done.feedback_status, done.feedback_error) == (
        "failed",
        "github_authentication_failed",
    )


def test_an_unexpected_publishing_error_does_not_stop_the_worker(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(app.feedback, "feedback_body", broken)
    run_id = queue(project_with_rule())

    work(CommentingGitHub(HEAD_A))
    done = run(run_id)
    assert (done.status, done.feedback_status, done.feedback_error) == (
        "completed",
        "failed",
        "internal_error",
    )

    # Nor does a retry the worker picks up from the queue of comments.
    with SessionLocal() as db:
        db.get(AnalysisRun, run_id).feedback_status = "pending"
        db.commit()
    make_feedback_due(run_id)
    assert work(CommentingGitHub(HEAD_A)) == 0
    assert run(run_id).feedback_status == "failed"


class RacingGitHub(CommentingGitHub):
    """Edits the comment more slowly the older the run it shows.

    Unless publishers take turns, the last edit to land is the oldest run's.
    """

    def __init__(self, newest: int) -> None:
        super().__init__(HEAD_A)
        self.newest = newest

    def update_comment(self, owner, repo, comment_id, body) -> bool:
        shown = int(re.search(r"run (\d+)", body).group(1))
        time.sleep(0.1 * (self.newest - shown))
        return super().update_comment(owner, repo, comment_id, body)


@pytest.mark.skipif(not ON_POSTGRESQL, reason="row locks need PostgreSQL")
def test_concurrent_publishers_leave_the_newest_run_on_the_comment():
    project_id = project_with_rule()
    run_ids = [queue(project_id) for _ in range(4)]
    quiet = CommentingGitHub(HEAD_A)
    quiet.comment_enabled = False
    work(quiet)
    with SessionLocal() as db:
        db.add(PullRequestComment(project_id=project_id, pull_number=7, comment_id=101))
        for run_id in run_ids:
            db.get(AnalysisRun, run_id).feedback_status = "pending"
        db.commit()
    github = RacingGitHub(newest=max(run_ids))
    github.comments[101] = "an earlier comment"
    barrier = threading.Barrier(len(run_ids))

    def publish(run_id: int) -> None:
        with SessionLocal() as db:
            pending = db.get(AnalysisRun, run_id)
            barrier.wait()
            publish_feedback(
                db,
                pending,
                github,
                public_url=None,
                max_attempts=3,
                retry_delay=Policy().retry_delay,
            )

    threads = [threading.Thread(target=publish, args=(i,)) for i in run_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert f"run {max(run_ids)} " in github.comments[101]
    assert (github.created, record(project_id).analysis_run_id) == (0, max(run_ids))
    assert run(max(run_ids)).feedback_status == "posted"
    assert {run(i).feedback_status for i in run_ids} <= {"posted", "stale"}


def test_nothing_is_written_without_comments_enabled(client):
    project_id = project_with_rule()
    github = CommentingGitHub(HEAD_A)
    github.comment_enabled = False
    run_id = queue(project_id)

    work(github)

    assert run(run_id).feedback_status is None
    assert client.get(f"/analysis-runs/{run_id}").json()["feedback"] is None
    assert github.comments == {}


def completed_run(**fields) -> AnalysisRun:
    values = {
        "id": 42,
        "status": "completed",
        "outcome": "fail",
        "fail_on": "error",
        "head_sha": HEAD_A,
        "files_analyzed": 3,
        "files_excluded": 1,
        "files_skipped": 0,
        "rules_evaluated": 2,
        "violation_count": 1,
        "error_count": 1,
        "warning_count": 0,
        "analysis_error_count": 0,
        "engine_version": "0.3.0",
        **fields,
    }
    return AnalysisRun(**values)


def violation(path: str, message: str, **fields) -> AnalysisViolation:
    return AnalysisViolation(
        kind="violation",
        code="forbidden_import",
        severity="error",
        path=path,
        line=1,
        message=message,
        **fields,
    )


def test_the_comment_names_the_commit_the_findings_the_limits_and_the_run():
    run = completed_run()
    run.violations = [violation("app/a.py", "app.a must not import app.db")]

    body = feedback_body(run, "https://invariant.example/")

    assert body.splitlines() == [
        "<!-- invariant:feedback -->",
        f"**Invariant: FAIL** for `{HEAD_A[:12]}`: 1 violation at or above error",
        "",
        "- `app/a.py:1` error: `app.a must not import app.db`",
        "",
        (
            "Checked 3 changed Python files against 2 rules; 1 excluded, 0 not "
            "analyzed. Invariant reads the code without running it, so dynamic "
            "imports and calls made through variables are not seen."
        ),
        (
            "Details: [run 42](https://invariant.example/analysis-runs/42) · "
            "Invariant 0.3.0"
        ),
    ]


def test_every_outcome_has_its_own_headline():
    headlines = {
        outcome: feedback_body(completed_run(outcome=outcome, **fields)).splitlines()[1]
        for outcome, fields in {
            "pass": {"warning_count": 2, "error_count": 0},
            "incomplete": {"analysis_error_count": 2},
            "not_applicable": {},
        }.items()
    }

    assert headlines == {
        "pass": f"**Invariant: PASS** for `{HEAD_A[:12]}` with 2 warnings",
        "incomplete": (
            f"**Invariant: INCOMPLETE** for `{HEAD_A[:12]}`: 2 problems kept files "
            "from being checked, so there is no verdict"
        ),
        "not_applicable": (
            f"**Invariant: NOT APPLICABLE** for `{HEAD_A[:12]}`: no enabled rule "
            "applies to the changed Python files"
        ),
    }
    assert "[run" not in feedback_body(completed_run())  # no public URL, no link


def test_names_from_the_pull_request_cannot_format_or_mention():
    run = completed_run(violation_count=12, error_count=12)
    run.violations = [violation("app/@admin `x`.py", "**bold** @team")] + [
        violation(f"app/{index}.py", "m") for index in range(11)
    ]

    body = feedback_body(run)

    assert "- ``app/@admin `x`.py:1`` error: `**bold** @team`" in body
    assert "- … and 2 more" in body
    assert body.count("\n- ") == 11


def test_long_paths_and_messages_are_shortened():
    run = completed_run()
    run.violations = [violation("app/" + "p" * 1000 + ".py", "m" * 5000)]

    [item] = [line for line in feedback_body(run).splitlines() if line.startswith("- ")]

    path, message = item.removeprefix("- `").split("` error: `")
    assert len(path) == len(":1") + MAX_TEXT
    assert path.endswith("…:1")
    assert len(message.removesuffix("`")) == MAX_TEXT
