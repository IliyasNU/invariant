"""Serve the API: ``python -m app.server``.

Logs are configured before uvicorn starts, so its own records come out in
the same format; Invariant logs each request itself, with its ID and
duration, in place of uvicorn's access log. SIGTERM finishes the requests in
progress, then stops.
"""

import uvicorn

from app.config import Settings
from app.logs import configure_logging


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format, settings.secrets())
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
