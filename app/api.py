from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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
)

router = APIRouter()
DatabaseSession = Annotated[Session, Depends(get_db)]


def _project_or_404(project_id: int, db: Session) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


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
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A project already uses this GitHub repository",
        ) from exc
    db.refresh(project)
    return project


@router.post(
    "/projects/{project_id}/rules",
    response_model=RuleResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_rule(project_id: int, payload: RuleCreate, db: DatabaseSession) -> Rule:
    _project_or_404(project_id, db)
    rule = Rule(project_id=project_id, **payload.model_dump())
    db.add(rule)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A rule with this name already exists in the project",
        ) from exc
    db.refresh(rule)
    return rule


@router.get("/projects/{project_id}/rules", response_model=list[RuleResponse])
def list_rules(project_id: int, db: DatabaseSession) -> list[Rule]:
    _project_or_404(project_id, db)
    statement = select(Rule).where(Rule.project_id == project_id).order_by(Rule.id)
    return list(db.scalars(statement))


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
def list_analysis_runs(project_id: int, db: DatabaseSession) -> list[AnalysisRun]:
    _project_or_404(project_id, db)
    statement = (
        select(AnalysisRun)
        .where(AnalysisRun.project_id == project_id)
        .order_by(AnalysisRun.id.desc())
    )
    return list(db.scalars(statement))


@router.get("/analysis-runs/{analysis_run_id}", response_model=AnalysisRunResponse)
def get_analysis_run(analysis_run_id: int, db: DatabaseSession) -> AnalysisRun:
    run = load_analysis_run(db, analysis_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Analysis run not found")
    return run
