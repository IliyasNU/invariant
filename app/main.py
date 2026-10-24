from typing import Any

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api import router
from app.database import engine
from app.errors import APIError, RequestContextMiddleware, install_error_handlers
from app.github_api import router as github_router
from app.schemas import ErrorResponse
from invariant import __version__

app = FastAPI(
    title="Invariant",
    # One version for the package, the API and the CLI.
    version=__version__,
    description="Store architecture rules and analyze Python changes or GitHub PRs.",
)
app.include_router(router)
app.include_router(github_router)
install_error_handlers(app)
app.add_middleware(RequestContextMiddleware)


def openapi() -> dict[str, Any]:
    """The schema, with validation errors in Invariant's own error format."""
    if app.openapi_schema is None:
        schema = FastAPI.openapi(app)
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        definition = ErrorResponse.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        components.update(definition.pop("$defs", {}))
        # Every 422 response refers to HTTPValidationError.
        components["HTTPValidationError"] = definition
        components.pop("ValidationError", None)
    return app.openapi_schema


app.openapi = openapi


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/db-health")
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
