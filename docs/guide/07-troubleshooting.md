# 7. Common errors and debugging

Each entry gives the message you see, why it happens, and the fix. The
messages were reproduced for this guide unless marked otherwise.

## Starting things

### The API exits at once with exit code 2

```text
CRITICAL app.server Set INVARIANT_API_KEY to protect the API (generate one with: python -c 'import secrets; print(secrets.token_urlsafe(32))'), or INVARIANT_LOCAL_ONLY=true if only this machine can reach it event=insecure_configuration
```

No key and no local-only mode. The API refuses to start open by mistake.
Set `INVARIANT_API_KEY` in `.env` (or `INVARIANT_LOCAL_ONLY=true` for a
machine-local API). With `uv run fastapi dev` or `fastapi run` the same
check shows as a traceback ending in
`app.security.InsecureConfiguration: …` and `Application startup failed`.

```text
CRITICAL app.server INVARIANT_API_KEY must be at least 32 characters; …
```

The key is too short. Generate one with the command shown.

`INVARIANT_CORS_ORIGINS` containing `*` is refused the same way.

### `No module named 'app'`

```text
.venv/bin/python3: Error while finding module specification for 'app.server' (ModuleNotFoundError: No module named 'app')
```

You ran `uv run python -m app.server` (or `app.worker`, `alembic`) outside
the repository root. `app/` is not an installed package; Python finds it
because the current directory is the checkout. `cd` to the root. The same
mistake also means `.env` is not read, since it is looked up in the current
directory.

### Settings in `.env` are ignored

- You are not in the repository root (see above).
- The same variable is set in your shell; the shell wins. `env | grep -E 'INVARIANT|GITHUB|POSTGRES'`.
- In Docker, `compose.yaml` passes only the variables it lists; after editing
  `.env`, recreate the containers: `docker compose up -d`.
- On WSL, a `.env` saved by a Windows editor can have CRLF line endings, and
  the values then end in an invisible `\r`. Check with `cat -A .env | head`
  (`^M$` at line ends) and fix with `sed -i 's/\r$//' .env`. (Not
  reproduced.)

### `address already in use`

```text
ERROR uvicorn.error [Errno 98] error while attempting to bind on address ('127.0.0.1', 8000): address already in use
```

Another process has the port: often a second API you forgot, or the Compose
`app` service. `ss -ltnp | grep 8000` shows it. Stop it, or use another port
(`INVARIANT_PORT=8001`, or `APP_PORT` for Compose). The same applies to
PostgreSQL's host port 5433 (`POSTGRES_PORT`).

### `Connection refused` to port 5433

```text
psycopg.OperationalError: connection failed: connection to server at "127.0.0.1", port 5433 failed: Connection refused
```

The database container is not running, or not yet healthy.
`docker compose up -d db`, then `docker compose ps` until it says
`(healthy)`. If you changed `POSTGRES_PORT`, `.env` and the running
container must agree.

### Docker: `permission denied … docker.sock` or `cannot connect to the Docker daemon`

Your user is not in the `docker` group (`sudo usermod -aG docker "$USER"`,
then a new terminal), or the daemon is not running (Docker Desktop not
started; `sudo service docker start` with Docker Engine in WSL). See
[chapter 2](02-install-windows-wsl.md). (Not reproduced.)

## Requests

### `/ready` answers `503 not_migrated`

```json
{"error":{"code":"not_migrated","message":"The database schema is not this version's: run alembic upgrade head","request_id":"…"}}
```

The code is newer (or older) than the database schema, typically after
pulling a branch with a new migration. Run `uv run alembic upgrade head`.
`/health` still says `ok`: the process runs, it is just not ready.
`503 database_unavailable` means the database does not answer at all.

### `401 unauthorized`

Missing header, a typo in it, or a different key than the API was started
with. The header is exactly `Authorization: Bearer <key>`. After 30 failures
a minute from one address the answer becomes `429 too_many_failed_keys`.

### `422 validation_error`

The body does not match the schema. `fields` says where:

```json
{"error":{"code":"validation_error","message":"The request is not valid","request_id":"…","fields":[{"location":"body.1","message":"JSON decode error","type":"json_invalid"}]}}
```

`body.1` with `json_invalid` is broken JSON, often shell quoting: use single
quotes around `-d '…'`, or build the body with `python3 -c 'import json; …'`
or `jq`. Other common ones: a path with `..` or not ending in `.py`, a
`max_lines` of 0, an unknown field (fields are strict, so a typo is an
error, not ignored).

### `413 request_too_large`

The body is over `INVARIANT_MAX_BODY_BYTES` (16 MiB). Split the analysis, or
use the CLI for whole repositories.

### `429 too_many_requests` or `too_many_concurrent_analyses`

Rate limit (60 analyses and imports a minute per client) or more than two
direct analyses at once. Wait for the `Retry-After` header. In tests the
limit is off (`tests/conftest.py`).

### `409`

`project_conflict` / `rule_conflict`: the name, GitHub repository or rule key
already exists. `github_not_configured`: the project has no
`github_owner`/`github_repo`. `too_many_rules`: 500 per project.

### An analysis says `not_applicable` though the code is wrong

Nothing was checked. Look at `files` and `rules` in the response:

- `excluded` is high: the files are outside every `source_roots` directory
  or match `exclude_paths`. A file at `app/x.py` with `source_roots = ["src"]`
  is excluded.
- No rule's `source_pattern` matches the module name. With
  `source_roots = ["."]`, `src/shop/domain/a.py` is `src.shop.domain.a`, not
  `shop.domain.a`. Fix the root, not the pattern.
- The rules are disabled.

### An analysis says `incomplete`

Some file could not be analyzed; the `error` diagnostics name it and the
reason (`syntax_error`, `too_large`, `undecodable`, …). Fix or exclude the
file. This is by design: an unread file is never a pass.

## GitHub path

### Webhook: `503 github_webhook_not_configured`

`GITHUB_WEBHOOK_SECRET` is not set in the API's environment.

### Webhook: `401 invalid_github_signature`

The secret differs between GitHub and Invariant, or something changed the
body on the way (a proxy that re-encodes JSON). The signature covers the exact
bytes.

### Webhook: `404 github_project_not_found`

No project has this `github_owner`/`github_repo`. Compare with the
repository's URL; case does not matter.

### Runs stay `queued`

No worker is running, or it cannot reach the database. Start
`uv run python -m app.worker`, or `docker compose ps worker`. A run waiting
for a retry has `next_attempt_at` in the future and keeps its last `error`.

### A run is `failed`

Read `error.code`:

| Code | Usually |
| --- | --- |
| `github_not_found` | Wrong owner/repo/pull number, or a private repository without a token that can read it |
| `github_authentication_failed` | Bad or expired `GITHUB_TOKEN`, or missing permissions |
| `github_rate_limited` | Retried until `WORKER_MAX_ATTEMPTS`; use a token for a higher limit |
| `github_unavailable` | Network or GitHub outage, retried first |
| `github_invalid_response` | The answer was not what GitHub's API documents (a wrong `GITHUB_API_URL`?) |
| `github_api_error` | GitHub answered with another error status |
| `worker_lost` | The worker died or took longer than its lease, on every attempt |

### No comment appears

Comments need **both** `GITHUB_TOKEN` and `GITHUB_POST_PR_COMMENT=true` in
the **worker's** environment. Then look at the run's `feedback`: `stale`
means a newer commit was pushed (correct behaviour), `failed` has an
`error`.

## Debugging techniques

**Follow a request ID.** Every response has `X-Request-ID`; every log line
of that request carries it. With Compose:

```bash
docker compose logs app | grep 627653515c724546a47204e172ee4627
```

**Follow a run.** Worker log lines carry `run_id`:

```bash
docker compose logs worker | grep 'run_id=6\|"run_id": 6'
```

**Readable, more detailed logs.** `INVARIANT_LOG_FORMAT=text` and
`INVARIANT_LOG_LEVEL=DEBUG`. Logs never contain source code, request
bodies, the API key or tokens; do not add code that logs them.

**Look at the database.** `docker compose exec db psql -U invariant`, then
the queries in [chapter 3](03-docker-postgres.md#looking-inside-postgresql).

**Reproduce in a test.** The fastest debugger is a failing test:

```bash
uv run pytest tests/test_api.py -x            # stop at the first failure
uv run pytest -k webhook -o addopts="" -v     # by name, verbose
uv run pytest tests/test_worker.py --pdb      # drop into the debugger on failure
```

`tests/conftest.py` sets up an in-memory SQLite database, an API key and a
`TestClient`; GitHub is replaced by mocks, so no test touches the network.

**Step through the engine.** It is plain Python, with no database:

```python
from invariant.config import ProjectSettings, ForbiddenImportRule
from invariant.engine import Source, analyze

result = analyze(
    [Source("src/shop/domain/a.py", "import shop.infrastructure.db\n")],
    [ForbiddenImportRule(key="r", rule_type="forbidden_import",
                         source_pattern="shop.domain.*",
                         forbidden_imports=["shop.infrastructure"])],
    ProjectSettings(source_roots=["src"]),
)
print(result.outcome, [d.message for d in result.diagnostics])
```

Run it with `uv run python`, and put a `breakpoint()` in
`src/invariant/rules.py` to watch a checker work.

Next: [study guide](08-study-guide.md).
