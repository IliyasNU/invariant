from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.analysis import SourceSyntaxError
from app.analysis_service import (
    AnalysisIncomplete,
    AnalysisNotApplicable,
    ProjectGitHubNotConfigured,
)
from app.api import router
from app.database import engine
from app.github import GitHubAPIError
from app.github_api import router as github_router

app = FastAPI(
    title="Invariant",
    version="0.2.0",
    description="Store architecture rules and analyze Python changes or GitHub PRs.",
)
app.include_router(router)
app.include_router(github_router)


@app.exception_handler(SourceSyntaxError)
def source_syntax_error_handler(
    _request: Request, exc: SourceSyntaxError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "invalid_python",
                "path": exc.path,
                "line": exc.line,
                "column": exc.column,
                "message": exc.message,
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


@app.exception_handler(AnalysisIncomplete)
def analysis_incomplete_handler(
    _request: Request, exc: AnalysisIncomplete
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "analysis_incomplete",
                "message": (
                    "Some changed Python files could not be analyzed; "
                    "no result was recorded"
                ),
                "skipped": [
                    {"path": item.path, "reason": item.reason} for item in exc.skipped
                ],
                "file_list_truncated": exc.truncated,
            }
        },
    )


@app.exception_handler(AnalysisNotApplicable)
def analysis_not_applicable_handler(
    _request: Request, exc: AnalysisNotApplicable
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "not_applicable",
                "message": "The pull request changes no Python files to analyze",
                "ignored_files": exc.ignored,
            }
        },
    )


@app.exception_handler(SQLAlchemyError)
def database_error_handler(_request: Request, _exc: SQLAlchemyError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "Database unavailable"},
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
