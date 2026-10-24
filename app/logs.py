"""Structured logs: one JSON object per line, with the request and run IDs.

Every record carries the ``request_id`` of the HTTP request and the ``run_id``
of the analysis run it belongs to, when there is one, so that a run can be
followed from the request that queued it to the worker that ran it and the
comment it wrote. Fields given as ``extra`` are added as they are.

Secrets are redacted from the whole line, the traceback included: the
configured secrets (``Settings.secrets``), anything shaped like a GitHub
token or a bearer credential, and passwords in URLs.

The entry points (``python -m app.server``, ``python -m app.worker``) call
``configure_logging``; importing the application configures nothing.
"""

import json
import logging
import re
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)
RUN_ID: ContextVar[int | None] = ContextVar("run_id", default=None)

REDACTED = "[redacted]"
_PATTERNS = [
    # GitHub tokens: classic, fine-grained, OAuth, user-to-server, refresh.
    (
        re.compile(r"\b(?:gh[opsur]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
        REDACTED,
    ),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), rf"\1{REDACTED}"),
    # The password of user:password@ in a URL, such as a database URL.
    (re.compile(r"(://[^/:@\s]+:)[^/@\s]+@"), rf"\1{REDACTED}@"),
]
# Attributes every LogRecord has; anything else came in through ``extra``,
# apart from uvicorn's copy of the message with terminal colours.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "color_message",
}


@contextmanager
def run_context(run_id: int) -> Iterator[None]:
    """Tag every record logged inside with ``run_id``."""
    token = RUN_ID.set(run_id)
    try:
        yield
    finally:
        RUN_ID.reset(token)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class ContextFilter(logging.Filter):
    """Stamps each record with the request and run IDs as it is logged.

    A record logged with an explicit ``run_id`` keeps it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for name, variable in (("request_id", REQUEST_ID), ("run_id", RUN_ID)):
            value = variable.get()
            if value is not None and not hasattr(record, name):
                setattr(record, name, value)
        return True


class _Formatter(logging.Formatter):
    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        # Longest first, so that a secret containing another is replaced whole.
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def fields(self, record: logging.LogRecord) -> dict[str, object]:
        fields: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name, value in vars(record).items():
            if name not in _STANDARD and not name.startswith("_"):
                fields[name] = value
        if record.exc_info:
            fields["exception"] = self.formatException(record.exc_info)
        return fields


class JSONFormatter(_Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = json.dumps(self.fields(record), default=str, ensure_ascii=False)
        # The secrets as they appear inside a JSON string.
        escaped = [json.dumps(s, ensure_ascii=False)[1:-1] for s in self.secrets]
        return redact(line, escaped)


class TextFormatter(_Formatter):
    """For a terminal: the time, level, logger and message, then the fields."""

    def format(self, record: logging.LogRecord) -> str:
        fields = self.fields(record)
        head = " ".join(
            str(fields.pop(name)) for name in ("time", "level", "logger", "message")
        )
        exception = fields.pop("exception", None)
        line = " ".join([head, *(f"{name}={value}" for name, value in fields.items())])
        if exception:
            line += "\n" + str(exception)
        return redact(line, self.secrets)


def configure_logging(
    level: str = "INFO", log_format: str = "json", secrets: Iterable[str] = ()
) -> None:
    """Send every log record, uvicorn's too, to stderr in ``log_format``."""
    formatter_class = JSONFormatter if log_format == "json" else TextFormatter
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(ContextFilter())
    handler.setFormatter(formatter_class(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    # httpx logs every request URL at INFO; Invariant logs what matters.
    logging.getLogger("httpx").setLevel(logging.WARNING)
