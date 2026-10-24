from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api import router
from app.config import Settings
from app.database import engine
from app.errors import (
    REQUEST_ID_HEADER,
    APIError,
    RequestContextMiddleware,
    install_error_handlers,
)
from app.github_api import router as github_router
from app.schemas import ErrorResponse
from app.security import check_security
from invariant import __version__

system = APIRouter()


@system.get("/health")
def health():
    return {"status": "ok"}


@system.get("/db-health")
def db_health():
    try:
        with engine.connect() as connection:
            result = connection.execute(text("SELECT 1")).scalar_one()
    except SQLAlchemyError as exc:
        raise APIError(503, "database_unavailable", "Database unavailable") from exc

    return {
        "database": "ok",
        "result": result,
    }


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # A configuration that would leave the API open stops it here.
    check_security(application.state.settings)
    yield


def _openapi(application: FastAPI) -> dict[str, Any]:
    """The schema, with validation errors in Invariant's own error format."""
    if application.openapi_schema is None:
        schema = FastAPI.openapi(application)
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        definition = ErrorResponse.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        components.update(definition.pop("$defs", {}))
        # Every 422 response refers to HTTPValidationError.
        components["HTTPValidationError"] = definition
        components.pop("ValidationError", None)
    return application.openapi_schema


def create_app(settings: Settings | None = None) -> FastAPI:
    """The API, protected and configured by ``settings`` (the environment's)."""
    application = FastAPI(
        title="Invariant",
        # One version for the package, the API and the CLI.
        version=__version__,
        description=(
            "Store architecture rules and analyze Python changes or GitHub PRs."
        ),
        lifespan=lifespan,
    )
    application.state.settings = settings or Settings()
    application.include_router(system)
    application.include_router(router)
    application.include_router(github_router)
    install_error_handlers(application)
    origins = application.state.settings.cors_origin_list
    if origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER],
            expose_headers=[REQUEST_ID_HEADER, "X-Total-Count"],
            max_age=600,
        )
    # Outermost, so that every response, a CORS preflight too, has an ID.
    application.add_middleware(RequestContextMiddleware)
    application.openapi = lambda: _openapi(application)
    return application


app = create_app()
