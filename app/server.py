"""Serve the API: ``python -m app.server``.

Logs are configured before uvicorn starts, so its own records come out in
the same format; Invariant logs each request itself, with its ID and
duration, in place of uvicorn's access log. Settings that would leave the API
open stop it with exit code 2. SIGTERM finishes the requests in progress,
then stops.
"""

import logging

import uvicorn

from app.config import Settings
from app.logs import configure_logging
from app.security import insecurity

logger = logging.getLogger("app.server")


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format, settings.secrets())
    # Checked again when the application starts; here it ends the process
    # with one clear line instead of a traceback.
    reason = insecurity(settings)
    if reason is not None:
        logger.critical(reason, extra={"event": "insecure_configuration"})
        raise SystemExit(2)
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
