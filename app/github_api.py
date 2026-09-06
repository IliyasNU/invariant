import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analysis_service import analyze_github_pull_request
from app.config import Settings
from app.database import get_db
from app.github import GitHubClient, get_github_client, verify_webhook_signature
from app.models import AnalysisRun, Project
from app.schemas import AnalysisRunResponse, GitHubAnalysisRequest, WebhookResponse

router = APIRouter()
DatabaseSession = Annotated[Session, Depends(get_db)]
GitHubDependency = Annotated[GitHubClient, Depends(get_github_client)]


async def raw_request_body(request: Request) -> bytes:
    return await request.body()


RawBody = Annotated[bytes, Depends(raw_request_body)]


def _project_or_404(project_id: int, db: Session) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post(
    "/projects/{project_id}/analyze/github", response_model=AnalysisRunResponse
)
def analyze_github_project(
    project_id: int,
    payload: GitHubAnalysisRequest,
    db: DatabaseSession,
    github: GitHubDependency,
) -> AnalysisRun:
    project = _project_or_404(project_id, db)
    return analyze_github_pull_request(
        db,
        project,
        payload.pull_number,
        github,
        source="github_pr",
    )


@router.post(
    "/integrations/github/webhook",
    response_model=WebhookResponse,
    responses={202: {"model": WebhookResponse}},
)
def github_webhook(
    body: RawBody,
    db: DatabaseSession,
    github: GitHubDependency,
    github_event: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
    github_delivery: Annotated[str | None, Header(alias="X-GitHub-Delivery")] = None,
    github_signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
) -> WebhookResponse | JSONResponse:
    settings = Settings()
    if not settings.github_webhook_secret:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "github_webhook_not_configured",
                "message": "GITHUB_WEBHOOK_SECRET is not configured",
            },
        )
    if not verify_webhook_signature(
        body, github_signature, settings.github_webhook_secret
    ):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "invalid_github_signature",
                "message": "GitHub webhook signature is invalid",
            },
        )

    if github_event != "pull_request":
        response = WebhookResponse(status="ignored", detail="Unsupported event")
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED, content=response.model_dump()
        )

    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_webhook_payload", "message": "Invalid JSON"},
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_webhook_payload",
                "message": "Webhook payload must be an object",
            },
        )

    action = payload.get("action")
    if action not in {"opened", "synchronize", "reopened"}:
        response = WebhookResponse(
            status="ignored", detail="Pull request action is not analyzed"
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED, content=response.model_dump()
        )

    if not github_delivery or len(github_delivery) > 100:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "missing_github_delivery",
                "message": "A valid X-GitHub-Delivery header is required",
            },
        )

    existing = db.scalar(
        select(AnalysisRun).where(AnalysisRun.github_delivery_id == github_delivery)
    )
    if existing is not None:
        return WebhookResponse(status="duplicate", analysis_run_id=existing.id)

    try:
        owner = payload["repository"]["owner"]["login"].lower()
        repo = payload["repository"]["name"].lower()
        pull_number = payload["pull_request"]["number"]
    except (AttributeError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_webhook_payload",
                "message": "Repository or pull request metadata is missing",
            },
        ) from exc
    if (
        not isinstance(owner, str)
        or not isinstance(repo, str)
        or not owner
        or not repo
        or not isinstance(pull_number, int)
        or isinstance(pull_number, bool)
        or pull_number < 1
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_webhook_payload",
                "message": "Repository or pull request metadata is invalid",
            },
        )

    project = db.scalar(
        select(Project).where(
            Project.github_owner == owner,
            Project.github_repo == repo,
        )
    )
    if project is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "github_project_not_found",
                "message": "No Invariant project is configured for this repository",
            },
        )

    try:
        run = analyze_github_pull_request(
            db,
            project,
            pull_number,
            github,
            source="github_webhook",
            github_delivery_id=github_delivery,
        )
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(AnalysisRun).where(AnalysisRun.github_delivery_id == github_delivery)
        )
        if existing is not None:
            return WebhookResponse(status="duplicate", analysis_run_id=existing.id)
        raise

    return WebhookResponse(status="analyzed", analysis_run_id=run.id)
