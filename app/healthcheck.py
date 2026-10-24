"""Container health checks: ``python -m app.healthcheck api|worker``.

Only the standard library is used, so the image's own Python runs the check
without the application's environment.

- ``api``: ready (``GET /ready`` on this machine answers 200).
- ``worker``: the worker's heartbeat file was touched within the lease, plus
  a minute. An idle worker touches it every poll; a busy one after each run,
  which the job time keeps within the lease. A hung worker stops touching it.

The exit code is 0 when healthy, 1 otherwise.
"""

import os
import sys
import time
import urllib.request

DEFAULT_HEARTBEAT = "/tmp/invariant-worker-heartbeat"


def api_ready() -> bool:
    port = os.environ.get("INVARIANT_PORT", "8000")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=5):
            return True
    except OSError:  # refused, timed out, or an error status such as 503
        return False


def worker_alive(now: float | None = None) -> bool:
    path = os.environ.get("INVARIANT_WORKER_HEARTBEAT", DEFAULT_HEARTBEAT)
    limit = float(os.environ.get("WORKER_LEASE_SECONDS", "600")) + 60
    try:
        age = (now or time.time()) - os.path.getmtime(path)
    except OSError:
        return False
    return age <= limit


def main(argv: list[str]) -> int:
    checks = {"api": api_ready, "worker": worker_alive}
    if len(argv) != 1 or argv[0] not in checks:
        print("usage: python -m app.healthcheck api|worker", file=sys.stderr)
        return 2
    return 0 if checks[argv[0]]() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
