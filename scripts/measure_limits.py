"""Measure the largest direct analysis the API accepts, and one byte more.

    DATABASE_URL=postgresql+psycopg://... uv run python scripts/measure_limits.py

Builds a request just under INVARIANT_MAX_BODY_BYTES (16 MiB) of 500 Python
files, sends it to a project with one rule of each type, and prints the time
it took and how much the process grew; then sends a body one byte over the
limit, which must be refused with 413. ``--trace`` also reports the peak of
Python's own allocations, at the cost of a much slower run.

The database must be migrated (``uv run alembic upgrade head``); the script
creates a project in it.
"""

import json
import os
import resource
import sys
import time
import tracemalloc
from pathlib import Path

KEY = "measure-" + "k" * 32
os.environ["INVARIANT_API_KEY"] = KEY
os.environ["INVARIANT_RATE_LIMIT_PER_MINUTE"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.main import app

LIMIT = 16 * 1024 * 1024
RULES = [
    {"name": "No infrastructure", "forbidden_imports": ["app.infrastructure"]},
    {"key": "no-eval", "rule_type": "forbidden_call", "forbidden_calls": ["eval"]},
    {"key": "absolute", "rule_type": "no_relative_import"},
    {"key": "size", "rule_type": "max_file_lines", "max_lines": 2000},
]


def request_body() -> bytes:
    # Real code: the GitHub client three times over, cut at a blank line.
    source = (Path(__file__).resolve().parents[1] / "app" / "github.py").read_text()
    content = (source * 3)[:31_000]
    content = content[: content.rfind("\n\n")] + "\n"
    files = [{"path": f"app/domain/m{n:03}.py", "content": content} for n in range(500)]
    return json.dumps({"files": files}).encode()


def main() -> None:
    trace = "--trace" in sys.argv
    body = request_body()
    assert len(body) <= LIMIT
    print(f"request: 500 files, {len(body) / 2**20:.1f} MiB of {LIMIT / 2**20:.0f}")
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    with TestClient(app, headers=headers) as client:
        name = f"Measure {time.time():.0f}"
        project = client.post("/projects", json={"name": name}).json()["id"]
        for rule in RULES:
            client.post(f"/projects/{project}/rules", json=rule).raise_for_status()
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if trace:
            tracemalloc.start()
        started = time.perf_counter()
        response = client.post(f"/projects/{project}/analyze", content=body)
        elapsed = time.perf_counter() - started
        after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        run = response.json()
        print(
            f"answer: {response.status_code} {run['outcome']}, "
            f"{run['files']['analyzed']} files analyzed in {elapsed:.1f} s; "
            f"the process grew from {before / 1024:.0f} to {after / 1024:.0f} MiB"
        )
        if trace:
            peak = tracemalloc.get_traced_memory()[1]
            print(f"peak of Python's allocations: {peak / 2**20:.0f} MiB")
        over = client.post(
            f"/projects/{project}/analyze",
            content=body + b" " * (LIMIT - len(body) + 1),
        )
        print(
            f"one byte over the limit: {over.status_code} {over.json()['error']['code']}"
        )


if __name__ == "__main__":
    main()
