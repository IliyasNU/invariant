"""Who may use the API: whoever holds the operator's key.

Every route of the business API requires ``Authorization: Bearer <key>``.
The key is compared in constant time and read from that header only, never
from the URL; a missing or wrong key is ``401 unauthorized``. ``/health``,
``/db-health``, the API documentation and the GitHub webhook, which checks
its own HMAC signature, are open.

The API refuses to start without a key, unless ``INVARIANT_LOCAL_ONLY`` says
that it is reachable from this machine only: a forgotten setting must not
leave the API open.
"""

import hmac
import logging
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings
from app.errors import APIError

logger = logging.getLogger(__name__)

MIN_KEY_LENGTH = 32
GENERATE_KEY = "python -c 'import secrets; print(secrets.token_urlsafe(32))'"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_bearer = HTTPBearer(
    auto_error=False, description="The operator's API key, INVARIANT_API_KEY"
)


class InsecureConfiguration(Exception):
    """The settings would leave the API open, or its key guessable."""


def insecurity(settings: Settings) -> str | None:
    """Why the settings would expose the API by mistake, or None."""
    key = settings.api_key.get_secret_value() if settings.api_key else None
    if key is not None and len(key) < MIN_KEY_LENGTH:
        return (
            f"INVARIANT_API_KEY must be at least {MIN_KEY_LENGTH} characters; "
            f"generate one with: {GENERATE_KEY}"
        )
    if key is None and not settings.local_only:
        return (
            "Set INVARIANT_API_KEY to protect the API (generate one with: "
            f"{GENERATE_KEY}), or INVARIANT_LOCAL_ONLY=true if only this "
            "machine can reach it"
        )
    if "*" in settings.cors_origin_list:
        return "INVARIANT_CORS_ORIGINS must list the origins allowed, not '*'"
    return None


def check_security(settings: Settings) -> None:
    """Refuse settings that would expose the API by mistake; warn of risks."""
    reason = insecurity(settings)
    if reason is not None:
        raise InsecureConfiguration(reason)
    if settings.api_key is None:
        logger.warning(
            "The API accepts requests without a key (INVARIANT_LOCAL_ONLY); "
            "keep it reachable from this machine only",
            extra={"event": "local_only"},
        )
    public = urlsplit(settings.public_url or "")
    if public.scheme == "http" and public.hostname not in _LOOPBACK:
        logger.warning(
            "INVARIANT_PUBLIC_URL is not HTTPS; serve the API through a "
            "TLS-terminating proxy",
            extra={"event": "public_url_not_https"},
        )


def require_api_key(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """Let the request through only with the API key, unless local-only."""
    expected = request.app.state.settings.api_key
    if expected is None:
        return
    challenge = {"WWW-Authenticate": "Bearer"}
    if credentials is None:
        raise APIError(
            401,
            "unauthorized",
            "An API key is required: Authorization: Bearer <key>",
            headers=challenge,
        )
    if not hmac.compare_digest(
        credentials.credentials.encode(), expected.get_secret_value().encode()
    ):
        raise APIError(
            401, "unauthorized", "The API key is not valid", headers=challenge
        )


APIKey = Depends(require_api_key)
