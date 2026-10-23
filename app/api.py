import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analysis import rule_columns, rule_data
from app.analysis_service import load_analysis_run, persist_analysis
from app.database import get_db
from app.models import AnalysisRun, Project, Rule
from app.schemas import (
    AnalysisRequest,
    AnalysisRunResponse,
    AnalysisRunSummaryResponse,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    RuleCreate,
    RuleResponse,
    RuleUpdate,
)
from invariant.config import RULES, RuleConfig

router = APIRouter()
DatabaseSession = Annotated[Session, Depends(get_db)]
RULE_CONFLICT = "A rule with this key or name already exists in the project"


def _project_or_404(project_id: int, db: Session) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _rule_or_404(project_id: int, rule_id: int, db: Session) -> Rule:
    _project_or_404(project_id, db)
    rule = db.get(Rule, rule_id)
    # A rule is only reachable through its own project.
    if rule is None or rule.project_id != project_id:
        raise HTTPException(status_code=404, detail="Rule not found")
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


def _commit(db: Session, conflict: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=conflict) from exc


@router.post(
    "/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED
)
def create_project(payload: ProjectCreate, db: DatabaseSession) -> Project:
    project = Project(**payload.model_dump())
    db.add(project)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A project with this name or GitHub repository already exists",
        ) from exc
    db.refresh(project)
    return project


@router.get("/projects", response_model=list[ProjectResponse])
def list_projects(db: DatabaseSession) -> list[Project]:
    return list(db.scalars(select(Project).order_by(Project.id)))


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: int, db: DatabaseSession) -> Project:
    return _project_or_404(project_id, db)


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: int, payload: ProjectUpdate, db: DatabaseSession
) -> Project:
    project = _project_or_404(project_id, db)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    _commit(db, "A project with this name or GitHub repository already exists")
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
def delete_project(project_id: int, db: DatabaseSession) -> None:
    db.delete(_project_or_404(project_id, db))
    db.commit()


@router.post(
    "/projects/{project_id}/rules",
    response_model=RuleResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_rule(project_id: int, payload: RuleCreate, db: DatabaseSession) -> Rule:
    _project_or_404(project_id, db)
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


@router.get("/projects/{project_id}/rules", response_model=list[RuleResponse])
def list_rules(project_id: int, db: DatabaseSession) -> list[Rule]:
    _project_or_404(project_id, db)
    return _project_rules(project_id, db)


@router.get("/projects/{project_id}/rules/{rule_id}", response_model=RuleResponse)
def get_rule(project_id: int, rule_id: int, db: DatabaseSession) -> Rule:
    return _rule_or_404(project_id, rule_id, db)


@router.patch("/projects/{project_id}/rules/{rule_id}", response_model=RuleResponse)
def update_rule(
    project_id: int, rule_id: int, payload: RuleUpdate, db: DatabaseSession
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
def delete_rule(project_id: int, rule_id: int, db: DatabaseSession) -> None:
    # Past violations keep their rule_name snapshot; rule_id becomes NULL.
    db.delete(_rule_or_404(project_id, rule_id, db))
    db.commit()


@router.post("/projects/{project_id}/analyze", response_model=AnalysisRunResponse)
def analyze_project(
    project_id: int, payload: AnalysisRequest, db: DatabaseSession
) -> AnalysisRun:
    project = _project_or_404(project_id, db)
    return persist_analysis(db, project, payload.files, source="direct")


@router.get(
    "/projects/{project_id}/analysis-runs",
    response_model=list[AnalysisRunSummaryResponse],
)
def list_analysis_runs(
    project_id: int,
    db: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AnalysisRun]:
    _project_or_404(project_id, db)
    statement = (
        select(AnalysisRun)
        .where(AnalysisRun.project_id == project_id)
        .order_by(AnalysisRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(statement))


@router.get("/analysis-runs/{analysis_run_id}", response_model=AnalysisRunResponse)
def get_analysis_run(analysis_run_id: int, db: DatabaseSession) -> AnalysisRun:
    run = load_analysis_run(db, analysis_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Analysis run not found")
    return run
