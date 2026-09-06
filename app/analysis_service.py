import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.analysis import analyze_files
from app.github import GitHubAPIError, GitHubClient
from app.models import AnalysisRun, AnalysisViolation, Project, Rule
from app.schemas import SourceFile

logger = logging.getLogger(__name__)


class ProjectGitHubNotConfigured(Exception):
    pass


def persist_analysis(
    db: Session,
    project: Project,
    files: list[SourceFile],
    *,
    source: str,
    pull_number: int | None = None,
    github_delivery_id: str | None = None,
) -> AnalysisRun:
    statement = select(Rule).where(Rule.project_id == project.id).order_by(Rule.id)
    rules = list(db.scalars(statement))
    violations = analyze_files(files, rules)

    run = AnalysisRun(
        project_id=project.id,
        source=source,
        status="completed",
        passed=not violations,
        pull_number=pull_number,
        repository_owner=project.github_owner,
        repository_name=project.github_repo,
        github_delivery_id=github_delivery_id,
        files_analyzed=len(files),
        rules_evaluated=len(rules),
        violation_count=len(violations),
        violations=[
            AnalysisViolation(
                rule_id=violation.rule_id,
                rule_name=violation.rule_name,
                path=violation.path,
                line=violation.line,
                column=violation.column,
                imported_module=violation.imported_module,
                message=violation.message,
            )
            for violation in violations
        ],
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


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

    pull_request = github.get_pull_request(
        project.github_owner, project.github_repo, pull_number
    )
    files = github.get_changed_python_files(
        project.github_owner,
        project.github_repo,
        pull_request.number,
        pull_request.head_sha,
    )
    run = persist_analysis(
        db,
        project,
        files,
        source=source,
        pull_number=pull_request.number,
        github_delivery_id=github_delivery_id,
    )
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

    if run.passed:
        body = "Invariant: PASS"
    else:
        items = [
            f"- {violation.path}:{violation.line or '?'}: {violation.message}"
            for violation in run.violations[:10]
        ]
        suffix = "\n- …" if run.violation_count > 10 else ""
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


def load_analysis_run(db: Session, analysis_run_id: int) -> AnalysisRun | None:
    statement = (
        select(AnalysisRun)
        .options(selectinload(AnalysisRun.violations))
        .where(AnalysisRun.id == analysis_run_id)
    )
    return db.scalar(statement)
