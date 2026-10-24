from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.analysis_service import enqueue_github_analysis, fail_run, persist_analysis
from app.database import SessionLocal
from app.models import (
    AnalysisRun,
    AnalysisViolation,
    Project,
    PullRequestComment,
    Rule,
)
from app.retention import main, prune
from app.schemas import SourceFile
from app.worker import run_worker

NOW = datetime.now(UTC)
VIOLATING = [SourceFile(path="app/domain/a.py", content="import app.infrastructure\n")]


def history() -> dict[str, int]:
    """Runs of every kind, some finished long ago; their IDs by name."""
    with SessionLocal() as db:
        project = Project(name="Shop", github_owner="o", github_repo="r")
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
        runs = {
            name: persist_analysis(db, project, VIOLATING, source="direct")
            for name in ("old", "recent", "old_comment_pending")
        }
        runs["old_failed"] = enqueue_github_analysis(db, project, 7, source="github_pr")
        fail_run(runs["old_failed"], "github_not_found", "Not found")
        runs["old_queued"] = enqueue_github_analysis(db, project, 7, source="github_pr")
        runs["old_running"] = enqueue_github_analysis(
            db, project, 8, source="github_pr"
        )
        runs["old_running"].status = "running"
        runs["old_comment_pending"].feedback_status = "pending"
        long_ago = NOW - timedelta(days=40)
        for name, run in runs.items():
            when = NOW - timedelta(days=1) if name == "recent" else long_ago
            run.created_at = when
            if run.finished_at is not None:
                run.finished_at = when
        db.add(
            PullRequestComment(
                project_id=project.id,
                pull_number=7,
                comment_id=5,
                analysis_run_id=runs["old"].id,
            )
        )
        db.commit()
        return {name: run.id for name, run in runs.items()}


def remaining() -> set[int]:
    with SessionLocal() as db:
        return set(db.scalars(select(AnalysisRun.id)))


def test_old_finished_runs_go_and_everything_still_to_do_stays():
    ids = history()

    with SessionLocal() as db:
        assert prune(db, timedelta(days=30), dry_run=True) == 2
        assert remaining() == set(ids.values())  # a dry run deletes nothing
        assert prune(db, timedelta(days=30)) == 2

    assert remaining() == {
        ids[name]
        for name in ("recent", "old_queued", "old_running", "old_comment_pending")
    }
    with SessionLocal() as db:
        diagnostics = db.scalars(select(AnalysisViolation.analysis_run_id)).all()
        assert ids["old"] not in diagnostics  # deleted with their run
        assert ids["recent"] in diagnostics
        record = db.scalars(select(PullRequestComment)).one()
        assert (record.comment_id, record.analysis_run_id) == (5, None)
        assert db.scalar(select(func.count()).select_from(Project)) == 1
        assert db.scalar(select(func.count()).select_from(Rule)) == 1


def test_the_command_counts_or_deletes(capsys, monkeypatch):
    # The command configures logging for its process; a test must not.
    monkeypatch.setattr("app.retention.configure_logging", lambda *args: None)
    history()

    assert main(["--days", "30", "--dry-run"]) == 0
    assert capsys.readouterr().out == "2 runs would be deleted\n"
    assert main(["--days", "30"]) == 0
    assert capsys.readouterr().out == "2 runs deleted\n"
    assert main(["--days", "30"]) == 0
    assert capsys.readouterr().out == "0 runs deleted\n"


@pytest.mark.parametrize("argv", [[], ["--days", "0"]])
def test_the_command_needs_a_number_of_days(argv):
    with pytest.raises(SystemExit) as stopped:
        main(argv)

    assert stopped.value.code == 2


def test_the_worker_prunes_when_retention_is_set(monkeypatch):
    ids = history()
    monkeypatch.setenv("INVARIANT_RETENTION_DAYS", "30")

    class NoGitHub:
        def __enter__(self):
            raise AssertionError("no run is due")

    with SessionLocal() as db:
        # Neither the queued run nor the comment is due: the worker only prunes.
        later = NOW + timedelta(days=1)
        db.get(AnalysisRun, ids["old_queued"]).next_attempt_at = later
        db.get(AnalysisRun, ids["old_comment_pending"]).feedback_next_attempt_at = later
        db.commit()
    run_worker(github_factory=NoGitHub, until_idle=True)

    assert ids["old"] not in remaining()
    assert ids["old_queued"] in remaining()


def test_the_application_never_creates_its_schema_itself():
    # Only the migrations create tables: a missing one must not be hidden.
    package = Path(__file__).resolve().parents[1] / "app"
    callers = [
        path.name for path in package.glob("*.py") if "create_all" in path.read_text()
    ]

    assert callers == []
