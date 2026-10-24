"""Build the image, run the Compose stack and check that it works.

    python3 scripts/compose_smoke.py

Run it from anywhere, with Docker running; CI's ``docker`` job does. It uses a
Compose project of its own, ``invariant-smoke``, removed at the end with its
volume, and only the standard library.

1. The image builds; the migrations run once, before the API and the worker,
   which become healthy and run as an unprivileged user.
2. The API requires its key, is ready, and analyzes code.
3. A signed webhook is queued, analyzed by the worker against a stand-in
   GitHub (``scripts/github_stub.py``), and commented on.
4. A delivery made while the worker is stopped waits in the queue, through a
   restart of the API, until the worker runs it; the stopped worker exited 0,
   and the comment was edited, not repeated.
5. The whole stack goes down and up again, and keeps its data.
6. A backup taken with pg_dump is restored into a new, empty database, and
   the API serves the same projects, rules and history from it.
"""

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "invariant-smoke"
API = "http://127.0.0.1:8000"
STUB = "http://127.0.0.1:8081"
HEAD = "1" * 40
KEY = secrets.token_urlsafe(32)
WEBHOOK_SECRET = secrets.token_urlsafe(24)
SETTINGS = {
    "INVARIANT_API_KEY": KEY,
    "GITHUB_WEBHOOK_SECRET": WEBHOOK_SECRET,
    "GITHUB_TOKEN": "stub-token",
    "GITHUB_POST_PR_COMMENT": "true",
    "GITHUB_API_URL": "http://github-stub:8080",
    "INVARIANT_PUBLIC_URL": API,
    "WORKER_POLL_SECONDS": "1",
}
VIOLATION = "import app.infrastructure.db\n"


class Failure(Exception):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


def call(method, path, body=None, *, key=True, headers=None, base=API):
    """Send a request; returns the status and the decoded JSON body."""
    data = body if isinstance(body, bytes) or body is None else json.dumps(body)
    request = urllib.request.Request(
        base + path,
        data=data.encode() if isinstance(data, str) else data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    if key:
        request.add_header("Authorization", f"Bearer {KEY}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    return status, json.loads(raw) if raw else None


def deliver(delivery: str) -> dict:
    """A signed pull request event for pull request 7 of example/shop."""
    body = json.dumps(
        {
            "action": "synchronize",
            "repository": {"name": "shop", "owner": {"login": "example"}},
            "pull_request": {"number": 7},
        }
    ).encode()
    signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    status, answer = call(
        "POST",
        "/integrations/github/webhook",
        body,
        key=False,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )
    check(status == 202 and answer["status"] == "queued", f"webhook: {answer}")
    return answer


def run(run_id: int) -> dict:
    status, answer = call("GET", f"/analysis-runs/{run_id}")
    check(status == 200, f"run {run_id}: {answer}")
    return answer


def wait_for_run(run_id: int, timeout: float = 90) -> dict:
    """The run once it is finished and its comment is no longer pending."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = run(run_id)
        feedback = (current.get("feedback") or {}).get("status")
        if current["status"] in ("completed", "failed") and feedback != "pending":
            return current
        time.sleep(1)
    raise Failure(f"run {run_id} did not finish: {current}")


def comments() -> list[str]:
    status, answer = call("GET", "/_comments", key=False, base=STUB)
    check(status == 200, f"stub: {status}")
    return answer


def check_api() -> int:
    """The key, readiness and a direct analysis; returns the project's ID."""
    status, _ = call("GET", "/projects", key=False)
    check(status == 401, f"without the key: {status}")
    status, answer = call("GET", "/ready", key=False)
    check((status, answer) == (200, {"status": "ready"}), f"ready: {answer}")
    status, project = call(
        "POST",
        "/projects",
        {"name": "Shop", "github_owner": "example", "github_repo": "shop"},
    )
    check(status == 201, f"project: {project}")
    status, rule = call(
        "POST",
        f"/projects/{project['id']}/rules",
        {
            "name": "Domain isolation",
            "source_pattern": "app.domain.*",
            "forbidden_imports": ["app.infrastructure"],
        },
    )
    check(status == 201, f"rule: {rule}")
    status, analysis = call(
        "POST",
        f"/projects/{project['id']}/analyze",
        {"files": [{"path": "app/domain/orders.py", "content": VIOLATION}]},
    )
    check(status == 200 and analysis["outcome"] == "fail", f"analysis: {analysis}")
    print(f"project {project['id']}: direct analysis {analysis['outcome']}")
    return project["id"]


def check_webhook() -> int:
    """A delivery analyzed by the worker and commented on; returns the run's ID."""
    queued = deliver("smoke-1")
    done = wait_for_run(queued["analysis_run_id"])
    check(
        (done["status"], done["outcome"], done["head_sha"])
        == ("completed", "fail", HEAD),
        f"webhook run: {done}",
    )
    check(done["feedback"]["status"] == "posted", f"feedback: {done['feedback']}")
    [comment] = comments()
    check("**Invariant: FAIL**" in comment and HEAD[:12] in comment, comment)
    print(f"run {done['id']}: {done['outcome']}, comment posted")
    return done["id"]


def snapshot(project_id: int) -> dict:
    """What a restore must bring back."""
    projects = call("GET", "/projects")[1]
    rules = call("GET", f"/projects/{project_id}/rules")[1]
    runs = call("GET", f"/projects/{project_id}/analysis-runs")[1]
    diagnostics = {
        item["id"]: call("GET", f"/analysis-runs/{item['id']}/diagnostics")[1]
        for item in runs
    }
    return {
        "projects": [(item["id"], item["name"]) for item in projects],
        "rules": [(item["id"], item["key"]) for item in rules],
        "runs": [(item["id"], item["status"], item["outcome"]) for item in runs],
        "diagnostics": {
            run_id: [(item["path"], item["code"]) for item in items]
            for run_id, items in diagnostics.items()
        },
    }


class Stack:
    """The Compose project, run with the smoke test's own settings."""

    def __init__(self, env_file: Path) -> None:
        self.command = [
            "docker",
            "compose",
            "--project-name",
            PROJECT,
            "--file",
            str(ROOT / "compose.yaml"),
            "--file",
            str(ROOT / "scripts" / "compose.smoke.yaml"),
            "--env-file",
            str(env_file),
        ]
        # The shell's values would take precedence over the env file's.
        self.environment = {k: v for k, v in os.environ.items() if k not in SETTINGS}

    def __call__(self, *args: str, stdin: bytes | None = None, capture=False):
        return subprocess.run(
            [*self.command, *args],
            cwd=ROOT,
            env=self.environment,
            input=stdin,
            capture_output=capture,
            check=True,
        )

    def services(self) -> dict[str, dict]:
        out = self("ps", "--all", "--format", "json", capture=True).stdout
        text = out.decode().strip()
        # One object per line, or, from older Compose versions, one array.
        items = (
            json.loads(text)
            if text.startswith("[")
            else [json.loads(line) for line in text.splitlines() if line.strip()]
        )
        return {item["Service"]: item for item in items}

    def wait_healthy(self, *names: str, timeout: float = 240) -> None:
        deadline = time.monotonic() + timeout
        while True:
            state = self.services()
            health = {name: state.get(name, {}).get("Health") for name in names}
            if all(value == "healthy" for value in health.values()):
                print(f"healthy: {', '.join(names)}", flush=True)
                return
            for name in names:
                if state.get(name, {}).get("State") == "exited":
                    raise Failure(f"{name} exited: {state[name]}")
            if time.monotonic() > deadline:
                raise Failure(f"not healthy after {timeout} s: {health}")
            time.sleep(2)

    def output(self, *args: str) -> str:
        return self(*args, capture=True).stdout.decode().strip()


def main() -> int:
    with tempfile.TemporaryDirectory() as scratch:
        env_file = Path(scratch) / "smoke.env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in SETTINGS.items()))
        stack = Stack(env_file)
        try:
            smoke(stack, Path(scratch))
        except (Failure, subprocess.CalledProcessError) as error:
            print(f"\nFAILED: {error}", file=sys.stderr)
            stack("logs", "--no-color", "--timestamps")
            return 1
        finally:
            stack("down", "--volumes", "--remove-orphans")
    print("\nThe Compose stack works.")
    return 0


def smoke(stack: Stack, scratch: Path) -> None:
    step("1. Build, migrate once, start healthy and unprivileged")
    stack("build")
    stack("up", "--detach")
    stack.wait_healthy("db", "app", "worker")
    migrate = stack.services()["migrate"]
    check(
        (migrate["State"], migrate["ExitCode"]) == ("exited", 0),
        f"migrate: {migrate}",
    )
    current = stack.output("exec", "-T", "app", "alembic", "current")
    check("(head)" in current, f"alembic current: {current}")
    for service in ("app", "worker"):
        uid = stack.output("exec", "-T", service, "id", "-u")
        check(uid != "0", f"{service} runs as root")
    print(f"migrations at head ({current}); app and worker not root")

    step("2. The API: its key, readiness, a direct analysis")
    project_id = check_api()

    step("3. A webhook analyzed by the worker and commented on")
    check_webhook()

    step("4. The queue keeps a delivery through a stopped worker and a restart")
    stack("stop", "worker")
    worker = stack.services()["worker"]
    check(
        (worker["State"], worker["ExitCode"]) == ("exited", 0),
        f"stopped worker: {worker}",
    )
    queued = deliver("smoke-2")["analysis_run_id"]
    stack("restart", "app")
    stack.wait_healthy("app")
    check(run(queued)["status"] == "queued", f"run {queued}: {run(queued)}")
    stack("start", "worker")
    done = wait_for_run(queued)
    check(done["outcome"] == "fail", f"run {queued}: {done}")
    check(len(comments()) == 1, f"comments: {comments()}")
    print(f"run {queued} waited for the worker; the one comment was edited")

    step("5. Down and up again, with the data")
    before = snapshot(project_id)
    stack("down")
    stack("up", "--detach")
    stack.wait_healthy("app", "worker")
    check(snapshot(project_id) == before, "data changed across down and up")
    print(f"{len(before['runs'])} runs, {len(before['rules'])} rule kept")

    step("6. Back up, lose the volume, restore")
    dump = stack(
        "exec",
        "-T",
        "db",
        "pg_dump",
        "--username",
        "invariant",
        "--format",
        "custom",
        "invariant",
        capture=True,
    ).stdout
    check(len(dump) > 1000, f"the dump has {len(dump)} bytes")
    (scratch / "invariant.dump").write_bytes(dump)
    stack("down", "--volumes")
    stack("up", "--detach", "db")
    stack.wait_healthy("db")
    tables = stack.output(
        "exec",
        "-T",
        "db",
        "psql",
        "--username",
        "invariant",
        "--dbname",
        "invariant",
        "--tuples-only",
        "--command",
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'",
    )
    check(tables.strip() == "0", f"the new database has {tables} tables")
    stack(
        "exec",
        "-T",
        "db",
        "pg_restore",
        "--username",
        "invariant",
        "--dbname",
        "invariant",
        "--no-owner",
        stdin=dump,
    )
    stack("up", "--detach")
    stack.wait_healthy("app", "worker")
    restored = snapshot(project_id)
    check(restored == before, f"restored {restored}, expected {before}")
    print(f"restored {len(dump)} bytes: the same projects, rules and history")


if __name__ == "__main__":
    sys.exit(main())
