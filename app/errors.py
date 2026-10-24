"""One error format for every response, and the request ID that ties it to the logs.

Every error is ``{"error": {"code": ..., "message": ..., "request_id": ...}}``.
The code is stable and says what kind of error it is: a request Invariant
cannot accept (``validation_error`` and other 4xx codes), a GitHub problem
(``github_*``), a storage problem (``database_unavailable``,
``storage_error``) or a failure of Invariant itself (``internal_error``).
The message is Invariant's own and never carries SQL, a DSN, a token or a
traceback; those stay in the server's log, under the same request ID. A
validation error also lists the ``fields`` it found wrong.

Each response carries the ``X-Request-ID`` header: the client's own, when it
sent a usable one, or a new one.
"""

import logging
import re
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import (
    DisconnectionError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.analysis_service import ProjectGitHubNotConfigured
from app.github import GitHubAPIError
from app.logs import REQUEST_ID

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_USABLE_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    413: "request_too_large",
    415: "unsupported_media_type",
    429: "too_many_requests",
}


class APIError(Exception):
    """An error response: its status, code and message, and the fields at fault."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        fields: list[dict[str, str]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.fields = fields
        self.headers = headers


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    fields: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": REQUEST_ID.get(),
    }
    if fields is not None:
        error["fields"] = fields
    return JSONResponse(
        status_code=status_code, content={"error": error}, headers=headers
    )


def _location(parts: tuple[Any, ...] | list[Any]) -> str:
    return ".".join(str(part) for part in parts)


def _api_error(_request: Request, exc: APIError) -> JSONResponse:
    return error_response(
        exc.status_code, exc.code, exc.message, fields=exc.fields, headers=exc.headers
    )


def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    # Raised by the framework itself: an unknown path, a wrong method.
    message = exc.detail if isinstance(exc.detail, str) else "The request failed"
    code = _HTTP_CODES.get(exc.status_code, "http_error")
    return error_response(exc.status_code, code, message, headers=exc.headers)


def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # The rejected input is not echoed: it may be large, or a secret.
    fields = [
        {
            "location": _location(error.get("loc", ())),
            "message": str(error.get("msg", "")),
            "type": str(error.get("type", "")),
        }
        for error in exc.errors()
    ]
    return error_response(
        422, "validation_error", "The request is not valid", fields=fields
    )


def _github_error(_request: Request, exc: GitHubAPIError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message)


def _github_not_configured(
    _request: Request, _exc: ProjectGitHubNotConfigured
) -> JSONResponse:
    return error_response(
        409,
        "github_not_configured",
        "Project has no GitHub owner/repository configuration",
    )


def _database_error(_request: Request, exc: SQLAlchemyError) -> JSONResponse:
    if isinstance(
        exc, OperationalError | InterfaceError | DisconnectionError | PoolTimeoutError
    ):
        logger.warning("The database is unavailable", exc_info=exc)
        return error_response(503, "database_unavailable", "Database unavailable")
    # The database is up but rejected the statement: a bug, not an outage.
    logger.error("The database rejected a statement", exc_info=exc)
    return error_response(500, "storage_error", "The database rejected the operation")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(APIError, _api_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(GitHubAPIError, _github_error)
    app.add_exception_handler(ProjectGitHubNotConfigured, _github_not_configured)
    app.add_exception_handler(SQLAlchemyError, _database_error)


class RequestContextMiddleware:
    """Gives each request an ID, logs it, and answers what nothing else caught.

    An unexpected exception becomes a ``500 internal_error`` response; its
    traceback is logged with the request ID and never sent to the client.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        offered = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = offered if _USABLE_ID.fullmatch(offered) else uuid.uuid4().hex
        token = REQUEST_ID.set(request_id)
        started = time.perf_counter()
        status = 500
        response_started = False

        async def send_with_id(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            logger.exception("The request failed unexpectedly")
            if response_started:
                raise
            response = error_response(
                500, "internal_error", "The server failed unexpectedly"
            )
            await response(scope, receive, send_with_id)
        finally:
            logger.info(
                "%s %s %s",
                scope["method"],
                scope["path"],
                status,
                extra={
                    "event": "request",
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                },
            )
            REQUEST_ID.reset(token)
