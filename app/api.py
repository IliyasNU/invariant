import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analysis import rule_columns, rule_data
from app.analysis_service import load_analysis_run, persist_analysis, result_document
from app.config_service import SOURCE, apply_import, export_config, plan_import
from app.database import get_db
from app.errors import APIError
from app.limits import rate_limited
from app.models import AnalysisRun, AnalysisViolation, Project, Rule
from app.schemas import (
    MAX_INTEGER,
    AnalysisRequest,
    AnalysisResultDocument,
    AnalysisRunResponse,
    AnalysisRunSummaryResponse,
    ConfigImportPlan,
    ConfigImportRequest,
    PersistedViolationResponse,
    ProjectCreate,
    ProjectResponse,
    ProjectStatsResponse,
    ProjectUpdate,
    RuleCreate,
    RuleResponse,
    RuleUpdate,
    RunOutcome,
    RunStatus,
    SettingChange,
)
from app.security import APIKey
from invariant.config import MAX_RULES, RULES, ConfigError, RuleConfig, parse_config

# The whole business API requires the operator's key.
router = APIRouter(dependencies=[APIKey])
DatabaseSession = Annotated[Session, Depends(get_db)]
# Analyses and imports take time; each client may start only so many.
RateLimited = Depends(rate_limited)
RULE_CONFLICT = (
    "rule_conflict",
    "A rule with this key or name already exists in the project",
)
PROJECT_CONFLICT = (
    "project_conflict",
    "A project with this name or GitHub repository already exists",
)

TOTAL_COUNT = "X-Total-Count"
PAGINATED: dict[int | str, dict[str, Any]] = {
    200: {
        "headers": {
            TOTAL_COUNT: {
                "description": "How many items match the query, on all pages",
                "schema": {"type": "integer"},
            }
        }
    }
}
# IDs are positive and stored as INTEGER; any other number names nothing.
ID = Annotated[int, Path(ge=1, le=MAX_INTEGER)]
Limit = Annotated[int, Query(ge=1, le=500, description="Items per page")]
Offset = Annotated[int, Query(ge=0, le=MAX_INTEGER, description="Items to skip")]
Filter = Annotated[str | None, Query(min_length=1, max_length=500)]


def _page(
    db: Session, statement: Select, response: Response, limit: int, offset: int
) -> list[Any]:
    """One page of ``statement``; the X-Total-Count header counts every page."""
    total = db.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery())
    )
    response.headers[TOTAL_COUNT] = str(total)
    return list(db.scalars(statement.limit(limit).offset(offset)))


def _project_or_404(project_id: int, db: Session) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise APIError(404, "project_not_found", "Project not found")
    return project


def _rule_or_404(project_id: int, rule_id: int, db: Session) -> Rule:
    _project_or_404(project_id, db)
    rule = db.get(Rule, rule_id)
    # A rule is only reachable through its own project.
    if rule is None or rule.project_id != project_id:
        raise APIError(404, "rule_not_found", "Rule not found")
    return rule


def _project_rules(project_id: int, db: Session) -> list[Rule]:
    statement = select(Rule).where(Rule.project_id == project_id).order_by(Rule.id)
    return list(db.scalars(statement))


def _key_from_name(name: str) -> str:
    key = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-._")[:64]
    return key or "rule"


def _validated_rule(data: dict[str, Any]) -> RuleConfig:
    try:
        return RULES.validate_python(data)
    except ValidationError as exc:
        errors = [
            {**error, "loc": ("body", *error["loc"])}
            for error in exc.errors(include_url=False)
        ]
        raise RequestValidationError(errors) from exc


def _too_many_rules() -> APIError:
    # As many as one configuration file holds, so that an export imports.
    return APIError(409, "too_many_rules", f"A project has at most {MAX_RULES} rules")


def _commit(db: Session, conflict: tuple[str, str]) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise APIError(409, *conflict) from exc


@router.post(
    "/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED
)
def create_project(payload: ProjectCreate, db: DatabaseSession) -> Project:
    project = Project(**payload.model_dump())
    db.add(project)
    _commit(db, PROJECT_CONFLICT)
    db.refresh(project)
    return project


@router.get("/projects", response_model=list[ProjectResponse], responses=PAGINATED)
def list_projects(
    db: DatabaseSession, response: Response, limit: Limit = 100, offset: Offset = 0
) -> list[Project]:
    return _page(db, select(Project).order_by(Project.id), response, limit, offset)


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: ID, db: DatabaseSession) -> Project:
    return _project_or_404(project_id, db)


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: ID, payload: ProjectUpdate, db: DatabaseSession
) -> Project:
    project = _project_or_404(project_id, db)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    _commit(db, PROJECT_CONFLICT)
    db.refresh(project)
    return project


@router.delete(
    "/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    description=(
        "Deletes the project together with its rules and its entire analysis "
        "history (runs and violations). This cannot be undone."
    ),
)
def delete_project(project_id: ID, db: DatabaseSession) -> None:
    db.delete(_project_or_404(project_id, db))
    db.commit()


@router.post(
    "/projects/{project_id}/rules",
    response_model=RuleResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_rule(project_id: ID, payload: RuleCreate, db: DatabaseSession) -> Rule:
    _project_or_404(project_id, db)
    rules = db.scalar(select(func.count()).where(Rule.project_id == project_id))
    if rules >= MAX_RULES:
        raise _too_many_rules()
    data = payload.model_dump()
    if data["key"] is None and data["name"] is None:
        raise RequestValidationError(
            [
                {
                    "type": "missing",
                    "loc": ("body", "name"),
                    "msg": "Give the rule a name or a key",
                    "input": None,
                }
            ]
        )
    data["key"] = data["key"] or _key_from_name(data["name"])
    rule = Rule(project_id=project_id, **rule_columns(_validated_rule(data)))
    db.add(rule)
    _commit(db, RULE_CONFLICT)
    db.refresh(rule)
    return rule


@router.get(
    "/projects/{project_id}/rules",
    response_model=list[RuleResponse],
    responses=PAGINATED,
)
def list_rules(
    project_id: ID,
    db: DatabaseSession,
    response: Response,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[Rule]:
    _project_or_404(project_id, db)
    statement = select(Rule).where(Rule.project_id == project_id).order_by(Rule.id)
    return _page(db, statement, response, limit, offset)


@router.get("/projects/{project_id}/rules/{rule_id}", response_model=RuleResponse)
def get_rule(project_id: ID, rule_id: ID, db: DatabaseSession) -> Rule:
    return _rule_or_404(project_id, rule_id, db)


@router.patch("/projects/{project_id}/rules/{rule_id}", response_model=RuleResponse)
def update_rule(
    project_id: ID, rule_id: ID, payload: RuleUpdate, db: DatabaseSession
) -> Rule:
    rule = _rule_or_404(project_id, rule_id, db)
    merged = {**rule_data(rule), **payload.model_dump(exclude_unset=True)}
    for column, value in rule_columns(_validated_rule(merged)).items():
        setattr(rule, column, value)
    _commit(db, RULE_CONFLICT)
    db.refresh(rule)
    return rule


@router.delete(
    "/projects/{project_id}/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_rule(project_id: ID, rule_id: ID, db: DatabaseSession) -> None:
    # Past violations keep their rule_name snapshot; rule_id becomes NULL.
    db.delete(_rule_or_404(project_id, rule_id, db))
    db.commit()


@router.get(
    "/projects/{project_id}/config",
    response_class=Response,
    responses={200: {"content": {"application/toml": {}}}},
    description="The project's settings and rules as an invariant.toml file.",
)
def export_project_config(project_id: ID, db: DatabaseSession) -> Response:
    project = _project_or_404(project_id, db)
    return Response(
        content=export_config(project, _project_rules(project_id, db)),
        media_type="application/toml",
    )


@router.post(
    "/projects/{project_id}/config/import",
    response_model=ConfigImportPlan,
    dependencies=[RateLimited],
)
def import_project_config(
    project_id: ID, payload: ConfigImportRequest, db: DatabaseSession
) -> ConfigImportPlan:
    """Apply an invariant.toml to the project, or preview it with dry_run.

    Rules are matched by key. ``merge`` keeps rules the file does not mention;
    ``replace`` deletes them. Everything is applied in one transaction.
    """
    project = _project_or_404(project_id, db)
    rules = _project_rules(project_id, db)
    try:
        config = parse_config(payload.content, source=SOURCE)
        plan = plan_import(project, rules, config, payload.mode)
    except ConfigError as exc:
        raise APIError(
            422,
            "invalid_config",
            "The configuration cannot be imported",
            fields=[
                {"location": location, "message": message}
                for location, message in exc.problems
            ],
        ) from exc
    if len(rules) - len(plan.deleted) + len(plan.created) > MAX_RULES:
        raise _too_many_rules()
    if not payload.dry_run:
        apply_import(db, project, plan)
        _commit(db, RULE_CONFLICT)
    return ConfigImportPlan(
        mode=payload.mode,
        dry_run=payload.dry_run,
        applied=not payload.dry_run,
        project_changes=[
            SettingChange(field=name, before=before, after=after)
            for name, before, after in plan.project_changes
        ],
        created=[rule.key for rule in plan.created],
        updated=[rule.key for rule, _config in plan.updated],
        deleted=[rule.key for rule in plan.deleted],
        unchanged=[rule.key for rule in plan.unchanged],
    )


@router.post(
    "/projects/{project_id}/analyze",
    response_model=AnalysisRunResponse,
    dependencies=[RateLimited],
)
def analyze_project(
    project_id: ID, payload: AnalysisRequest, db: DatabaseSession, request: Request
) -> AnalysisRun:
    project = _project_or_404(project_id, db)
    with request.app.state.analysis_slots.slot():
        return persist_analysis(db, project, payload.files, source="direct")


@router.get(
    "/projects/{project_id}/analysis-runs",
    response_model=list[AnalysisRunSummaryResponse],
    responses=PAGINATED,
)
def list_analysis_runs(
    project_id: ID,
    db: DatabaseSession,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Offset = 0,
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
    outcome: RunOutcome | None = None,
    source: Literal["direct", "github_pr", "github_webhook"] | None = None,
    pull_number: Annotated[int | None, Query(ge=1, le=MAX_INTEGER)] = None,
) -> list[AnalysisRun]:
    """The project's runs, newest first, optionally filtered."""
    _project_or_404(project_id, db)
    statement = select(AnalysisRun).where(AnalysisRun.project_id == project_id)
    for column, value in (
        (AnalysisRun.status, run_status),
        (AnalysisRun.outcome, outcome),
        (AnalysisRun.source, source),
        (AnalysisRun.pull_number, pull_number),
    ):
        if value is not None:
            statement = statement.where(column == value)
    statement = statement.order_by(AnalysisRun.id.desc())
    return _page(db, statement, response, limit, offset)


@router.get("/projects/{project_id}/stats", response_model=ProjectStatsResponse)
def project_stats(project_id: ID, db: DatabaseSession) -> dict[str, Any]:
    """Counts over all of the project's runs.

    The pass rate only counts runs with a verdict (pass or fail); how many runs
    have none (incomplete, not applicable, failed or unfinished) is reported
    next to it rather than hidden in the denominator.
    """
    _project_or_404(project_id, db)
    in_project = AnalysisRun.project_id == project_id
    by_status = dict.fromkeys(("queued", "running", "completed", "failed"), 0)
    by_status.update(
        db.execute(
            select(AnalysisRun.status, func.count())
            .where(in_project)
            .group_by(AnalysisRun.status)
        )
        .tuples()
        .all()
    )
    by_outcome = dict.fromkeys(("pass", "fail", "incomplete", "not_applicable"), 0)
    by_outcome.update(
        db.execute(
            select(AnalysisRun.outcome, func.count())
            .where(in_project, AnalysisRun.outcome.is_not(None))
            .group_by(AnalysisRun.outcome)
        )
        .tuples()
        .all()
    )
    totals = db.execute(
        select(
            func.coalesce(func.sum(AnalysisRun.error_count), 0),
            func.coalesce(func.sum(AnalysisRun.warning_count), 0),
            func.coalesce(func.sum(AnalysisRun.analysis_error_count), 0),
            func.count(AnalysisRun.duration_ms),
            func.avg(AnalysisRun.duration_ms),
            func.max(AnalysisRun.duration_ms),
        ).where(in_project)
    ).one()
    errors, warnings, analysis_errors, timed, average, longest = totals
    runs = sum(by_status.values())
    decided = by_outcome["pass"] + by_outcome["fail"]
    latest = db.scalars(
        select(AnalysisRun).where(in_project).order_by(AnalysisRun.id.desc()).limit(10)
    )
    return {
        "project_id": project_id,
        "runs": runs,
        "by_status": by_status,
        "by_outcome": by_outcome,
        "pass_rate": {
            "passed": by_outcome["pass"],
            "decided": decided,
            "without_verdict": runs - decided,
            "rate": by_outcome["pass"] / decided if decided else None,
        },
        "diagnostics": {
            "errors": errors,
            "warnings": warnings,
            "analysis_errors": analysis_errors,
        },
        "duration": {
            "runs": timed,
            "average_ms": float(average) if average is not None else None,
            "max_ms": longest,
        },
        "latest": list(latest),
    }


@router.get("/analysis-runs/{analysis_run_id}", response_model=AnalysisRunResponse)
def get_analysis_run(analysis_run_id: ID, db: DatabaseSession) -> AnalysisRun:
    return _run_or_404(analysis_run_id, db)


@router.get(
    "/analysis-runs/{analysis_run_id}/diagnostics",
    response_model=list[PersistedViolationResponse],
    responses=PAGINATED,
)
def list_diagnostics(
    analysis_run_id: ID,
    db: DatabaseSession,
    response: Response,
    limit: Limit = 100,
    offset: Offset = 0,
    kind: Literal["violation", "error"] | None = None,
    severity: Literal["error", "warning"] | None = None,
    code: Filter = None,
    rule_key: Filter = None,
    path: Filter = None,
) -> list[AnalysisViolation]:
    """A run's diagnostics in the engine's order: path, line, column, rule key."""
    if db.get(AnalysisRun, analysis_run_id) is None:
        raise APIError(404, "analysis_run_not_found", "Analysis run not found")
    statement = select(AnalysisViolation).where(
        AnalysisViolation.analysis_run_id == analysis_run_id
    )
    for column, value in (
        (AnalysisViolation.kind, kind),
        (AnalysisViolation.severity, severity),
        (AnalysisViolation.code, code),
        (AnalysisViolation.rule_key, rule_key),
        (AnalysisViolation.path, path),
    ):
        if value is not None:
            statement = statement.where(column == value)
    statement = statement.order_by(AnalysisViolation.id)
    return _page(db, statement, response, limit, offset)


@router.get(
    "/analysis-runs/{analysis_run_id}/result",
    response_model=AnalysisResultDocument,
    summary="Export a run's result as JSON",
)
def get_analysis_result(analysis_run_id: ID, db: DatabaseSession) -> dict[str, Any]:
    """The result in the engine's format (``schema_version`` 1), as the CLI writes it.

    Source code is never stored, so it is not part of the result.
    """
    return result_document(_run_or_404(analysis_run_id, db))


def _run_or_404(analysis_run_id: int, db: Session) -> AnalysisRun:
    run = load_analysis_run(db, analysis_run_id)
    if run is None:
        raise APIError(404, "analysis_run_not_found", "Analysis run not found")
    return run
