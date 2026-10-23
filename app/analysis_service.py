import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.analysis import evaluate
from app.github import GitHubAPIError, GitHubClient, SkippedFile
from app.models import AnalysisRun, AnalysisViolation, Project, Rule
from app.schemas import SourceFile
from invariant.result import AnalysisResult, Outcome

logger = logging.getLogger(__name__)


class ProjectGitHubNotConfigured(Exception):
    pass


@dataclass
class AnalysisIncomplete(Exception):
    """Some changed Python files could not be analyzed, so there is no verdict."""

    skipped: list[SkippedFile]
    truncated: bool


@dataclass
class AnalysisNotApplicable(Exception):
    """Nothing in the pull request is checked, so there is nothing to verify."""

    ignored: int
    message: str = "The pull request changes no Python files to analyze"


def _project_rules(db: Session, project: Project) -> list[Rule]:
    statement = select(Rule).where(Rule.project_id == project.id).order_by(Rule.id)
    return list(db.scalars(statement))


def _record_run(
    db: Session,
    project: Project,
    rules: list[Rule],
    result: AnalysisResult,
    *,
    source: str,
    pull_number: int | None,
    github_delivery_id: str | None,
) -> AnalysisRun:
    rule_ids = {rule.key: rule.id for rule in rules}
    run = AnalysisRun(
        project_id=project.id,
        source=source,
        status="completed",
        # Warnings below the project's fail_on threshold do not fail a run.
        passed=result.outcome is not Outcome.FAIL,
        pull_number=pull_number,
        repository_owner=project.github_owner,
        repository_name=project.github_repo,
        github_delivery_id=github_delivery_id,
        files_analyzed=result.files.analyzed,
        rules_evaluated=result.rules_enabled,
        violation_count=len(result.violations),
        violations=[
            AnalysisViolation(
                kind=item.kind,
                code=item.code,
                severity=item.severity,
                rule_id=rule_ids.get(item.rule_key),
                rule_key=item.rule_key,
                rule_name=item.rule_name,
                path=item.path,
                line=item.line,
                column=item.column,
                imported_module=item.data.get("imported_module"),
                message=item.message,
                data=item.data,
            )
            for item in result.violations
        ],
    )
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
    pull_number: int | None = None,
    github_delivery_id: str | None = None,
) -> AnalysisRun:
    rules = _project_rules(db, project)
    return _record_run(
        db,
        project,
        rules,
        evaluate(files, rules, project),
        source=source,
        pull_number=pull_number,
        github_delivery_id=github_delivery_id,
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

    pull_request = github.get_pull_request(
        project.github_owner, project.github_repo, pull_number
    )
    changed = github.get_changed_python_files(
        project.github_owner, project.github_repo, pull_request
    )
    # A run can only pass or fail, so an analysis that could not see every changed
    # Python file is refused rather than recorded (and announced) as a pass.
    if not changed.complete:
        raise AnalysisIncomplete(skipped=changed.skipped, truncated=changed.truncated)
    if not changed.files:
        raise AnalysisNotApplicable(ignored=changed.ignored)
    rules = _project_rules(db, project)
    result = evaluate(changed.files, rules, project)
    # Excluded files, or rules that apply to none of them: a PASS would claim a
    # check that never ran.
    if result.outcome is Outcome.NOT_APPLICABLE:
        raise AnalysisNotApplicable(
            ignored=changed.ignored + result.files.excluded,
            message="No enabled rule applies to the changed Python files",
        )
    run = _record_run(
        db,
        project,
        rules,
        result,
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

    items = [
        f"- {violation.path}:{violation.line or '?'}: "
        + ("warning: " if violation.severity == "warning" else "")
        + violation.message
        for violation in run.violations[:10]
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


def load_analysis_run(db: Session, analysis_run_id: int) -> AnalysisRun | None:
    statement = (
        select(AnalysisRun)
        .options(selectinload(AnalysisRun.violations))
        .where(AnalysisRun.id == analysis_run_id)
    )
    return db.scalar(statement)
