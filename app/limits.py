"""Limits that keep one client from exhausting the API.

- **Request bodies** larger than ``INVARIANT_MAX_BODY_BYTES`` (16 MiB) are
  refused with ``413 request_too_large`` before they are read whole: a
  declared ``Content-Length`` over the limit at once, a streamed body where
  it passes the limit.
- **Expensive requests** (direct analyses, GitHub analyses, configuration
  imports) are limited to ``INVARIANT_RATE_LIMIT_PER_MINUTE`` (60) per
  client, then answered ``429 too_many_requests`` with ``Retry-After``.
- **Failed API keys** are limited to ``INVARIANT_AUTH_FAILURES_PER_MINUTE``
  (30) per client, then answered 429, which makes guessing slower still.
- **Direct analyses** run at most ``INVARIANT_MAX_CONCURRENT_ANALYSES`` (2)
  at a time; a request that finds no slot within a few seconds is answered
  ``429 too_many_concurrent_analyses``.

Rate limits count per API process and client address; 0 turns one off.
"""

import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from fastapi import Request
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import APIError, error_response

SLOT_WAIT_SECONDS = 5.0


class RateLimiter:
    """``per_minute`` requests a minute per key, in bursts of as many."""

    def __init__(
        self, per_minute: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.per_minute = per_minute
        self.clock = clock
        self._lock = threading.Lock()
        # key -> (tokens left, when they were counted)
        self._buckets: dict[str, tuple[float, float]] = {}

    def wait(self, key: str) -> float | None:
        """Take a token for ``key``; None if granted, else seconds to wait."""
        if self.per_minute <= 0:
            return None
        rate = self.per_minute / 60
        now = self.clock()
        with self._lock:
            tokens, then = self._buckets.get(key, (float(self.per_minute), now))
            tokens = min(float(self.per_minute), tokens + (now - then) * rate)
            if tokens < 1:
                self._buckets[key] = (tokens, now)
                return (1 - tokens) / rate
            self._buckets[key] = (tokens - 1, now)
            if len(self._buckets) > 10_000:
                self._forget_full(now, rate)
        return None

    def _forget_full(self, now: float, rate: float) -> None:
        # A full bucket is the same as no bucket.
        self._buckets = {
            key: (tokens, then)
            for key, (tokens, then) in self._buckets.items()
            if tokens + (now - then) * rate < self.per_minute
        }


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def too_many(retry_after: float, code: str, message: str) -> APIError:
    return APIError(
        429, code, message, headers={"Retry-After": str(math.ceil(retry_after))}
    )


def rate_limited(request: Request) -> None:
    """A dependency of the expensive routes."""
    wait = request.app.state.rate_limiter.wait(client_address(request))
    if wait is not None:
        raise too_many(
            wait, "too_many_requests", "Too many requests; retry after a while"
        )


class AnalysisSlots:
    """At most ``size`` direct analyses at once."""

    def __init__(self, size: int, wait: float = SLOT_WAIT_SECONDS) -> None:
        self._semaphore = threading.BoundedSemaphore(size)
        self.wait = wait

    @contextmanager
    def slot(self) -> Iterator[None]:
        if not self._semaphore.acquire(timeout=self.wait):
            raise too_many(
                self.wait,
                "too_many_concurrent_analyses",
                "Other analyses are running; retry after a while",
            )
        try:
            yield
        finally:
            self._semaphore.release()


class BodyLimitMiddleware:
    """Refuses request bodies over ``max_bytes`` before they are read whole."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    def _message(self) -> str:
        return f"The request body is larger than {self.max_bytes} bytes"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            response = error_response(413, "request_too_large", self._message())
            await response(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # FastAPI passes an HTTPException from reading the body on.
                    raise HTTPException(413, self._message())
            return message

        await self.app(scope, limited_receive, send)
