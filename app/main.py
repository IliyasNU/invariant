import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import (
    DisconnectionError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.analysis_service import AnalysisFailed, ProjectGitHubNotConfigured
from app.api import router
from app.database import engine
from app.github import GitHubAPIError
from app.github_api import router as github_router
from invariant import __version__

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Invariant",
    # One version for the package, the API and the CLI.
    version=__version__,
    description="Store architecture rules and analyze Python changes or GitHub PRs.",
)
app.include_router(router)
app.include_router(github_router)


@app.exception_handler(AnalysisFailed)
def analysis_failed_handler(_request: Request, exc: AnalysisFailed) -> JSONResponse:
    # The upstream status and code, plus the failed run that records them.
    return JSONResponse(
        status_code=exc.error.status_code,
        content={
            "detail": {
                "code": exc.error.code,
                "message": exc.error.message,
                "analysis_run_id": exc.run.id,
            }
        },
    )


@app.exception_handler(GitHubAPIError)
def github_api_error_handler(_request: Request, exc: GitHubAPIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(ProjectGitHubNotConfigured)
def github_not_configured_handler(
    _request: Request, _exc: ProjectGitHubNotConfigured
) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": {
                "code": "github_not_configured",
                "message": "Project has no GitHub owner/repository configuration",
            }
        },
    )


@app.exception_handler(SQLAlchemyError)
def database_error_handler(_request: Request, exc: SQLAlchemyError) -> JSONResponse:
    if isinstance(
        exc, OperationalError | InterfaceError | DisconnectionError | PoolTimeoutError
    ):
        return JSONResponse(
            status_code=503,
            content={"detail": "Database unavailable"},
        )
    # The database is up but rejected the statement: a bug, not an outage.
    logger.error("Database rejected a statement", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "code": "storage_error",
                "message": "The database rejected the operation",
            }
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/db-health")
def db_health():
    try:
        with engine.connect() as connection:
            result = connection.execute(text("SELECT 1")).scalar_one()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    return {
        "database": "ok",
        "result": result,
    }
