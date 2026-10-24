# Invariant

Invariant is a backend developer tool for enforcing project-specific software
architecture rules. A team records boundaries such as “domain code must not
import infrastructure,” then submits source changes or a GitHub pull request.
Invariant returns deterministic, machine-readable violations and keeps an audit
history of every completed analysis. The `invariant` command runs the same
checks offline, on a developer machine or in CI.

Version 0.2 is intentionally focused: it analyzes Python imports with the
standard-library AST. It does **not** claim semantic or AI-based architecture
understanding, and the human developer remains the final authority.

## Architecture

```mermaid
flowchart LR
    Direct[Direct API request] --> Pipeline[Shared analysis workflow]
    Manual[Manual GitHub PR request] --> GitHub[GitHub REST client]
    Webhook[Signed GitHub webhook] --> GitHub
    GitHub --> Files[Changed Python files at PR head]
    Files --> Pipeline
    Pipeline --> AST[Deterministic Python AST analyzer]
    Rules[(Project rules)] --> AST
    AST --> Runs[(Analysis runs and violations)]
    Runs --> History[History API]
    Runs -. optional .-> Comment[GitHub PR comment]
```

- **FastAPI** exposes projects, rules, analysis, history, health, and webhook APIs.
- **SQLAlchemy 2 + PostgreSQL 18** persist project configuration, rules, runs,
  and individual violations.
- **Alembic** migrates v0.1 data forward without resetting the database.
- **httpx** contains all GitHub REST networking in `app/github.py`.
- The shared analysis workflow in `app/analysis_service.py` is independent of
  whether source files came from an HTTP body, GitHub, or a future client.
- **`src/invariant`** is the analysis engine: configuration schema, source
  roots, rule types and results. The service runs it through `app/analysis.py`,
  and the upcoming CLI will run the same code. Its configuration format and
  semantics are described in [`docs/configuration.md`](docs/configuration.md).
- The core analyzer never executes submitted code.

## Data model

- A **Project** has a unique name, optional paired `github_owner` /
  `github_repo` configuration, and analysis settings: `source_roots`,
  `exclude_paths` and `fail_on`. Existing v0.1 projects remain valid with the
  GitHub fields null and the default settings.
- A **Rule** belongs to one project and has a stable `key`, a unique `name`, a
  `rule_type` (`forbidden_import`, `forbidden_call`, `no_relative_import`,
  `max_file_lines`),
  `enabled`, `severity`, `source_pattern`, `exclude_paths` and the fields of
  its type.
- An **AnalysisRun** is one analysis attempt: its source (`direct`,
  `github_pr` or `github_webhook`), its status and outcome (see
  [Results](#results)), file, rule and diagnostic counts, timing, the engine
  version, a snapshot and hash of the configuration it checked, and for GitHub
  the repository, pull request, base and head commits and delivery ID. Source
  code is never stored.
- An **AnalysisViolation** is a diagnostic: its kind, code and severity, a
  snapshot of the rule's key and name, the location, and data for the rule
  type. History stays readable when rules change or are deleted.

## How rules work

The rule types, their fields, source roots, the two kinds of pattern and the
meaning of results are described in
[`docs/configuration.md`](docs/configuration.md). In short, for each submitted
`.py` file Invariant derives a module name from its path relative to its source
root:

```text
app/domain/payment.py -> app.domain.payment         (source root ".")
src/app/domain/payment.py -> app.domain.payment     (source root "src")
```

A rule applies when that module matches its `source_pattern` and the file is
not excluded. `forbidden_import` checks `import` and `from ... import ...`
statements, including relative ones resolved against the file's package, and
`from package import name` as the possible submodule `package.name`.
`forbidden_call` reports calls such as `os.system()` or `eval()` that can be
traced to an import or a builtin, through aliases, and leaves alone a local
name that shadows them.

Example rule:

```json
{
  "name": "domain isolation",
  "rule_type": "forbidden_import",
  "source_pattern": "app.domain.*",
  "forbidden_imports": ["app.infrastructure", "fastapi"]
}
```

`from app.infrastructure.db import repository` inside
`app/domain/payment.py` produces a structured violation with the rule, path,
line, column, imported module, and explanation. Results are sorted by path,
line, column and rule key, so repeated analysis is deterministic.

## Command line

The `invariant` command checks a project without the API, a database, a token
or a network connection:

```bash
pip install "invariant @ git+https://github.com/IliyasNU/invariant@main"
invariant init                                     # write a starter invariant.toml
invariant check .                                  # every Python file in the project
invariant check . --base origin/main --head HEAD   # only what the branch changed
invariant check . --format json
```

It exits with `0` when no violation reaches the threshold, `1` for violations,
and `2` for an unusable configuration, a technical error, or a check that could
not see every file. The options, which files are read, the Git diff mode and a
tested GitHub Actions workflow are described in [`docs/cli.md`](docs/cli.md).

The installed package holds only the engine and the command, with pydantic as
its one dependency. The service's own dependencies form the `server`
dependency group, which `uv sync` installs by default.

## Local development

Requirements:

- [uv](https://docs.astral.sh/uv/)
- Docker with Compose

```bash
cp .env.example .env
uv sync --dev
docker compose up -d db
uv run alembic upgrade head
uv run fastapi dev app/main.py
```

The API is available at <http://127.0.0.1:8000>, with interactive documentation
at <http://127.0.0.1:8000/docs>.

PostgreSQL listens on `5432` inside its container and maps to host port `5433`
by default, avoiding a common conflict with host PostgreSQL installations. Set
`POSTGRES_PORT` in `.env` if 5433 is occupied.

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | unset | Complete SQLAlchemy URL; overrides all `POSTGRES_*` values |
| `POSTGRES_DB` | `invariant` | Database name |
| `POSTGRES_USER` | `invariant` | Database user |
| `POSTGRES_PASSWORD` | `invariant` | Local development password |
| `POSTGRES_HOST` | `localhost` | Database host for local app execution |
| `POSTGRES_PORT` | `5433` | Database port for local app execution / host mapping |
| `GITHUB_TOKEN` | unset | Optional for public repositories; required for private repositories and PR comments |
| `GITHUB_WEBHOOK_SECRET` | unset | Shared secret used to verify GitHub webhook signatures |
| `GITHUB_POST_PR_COMMENT` | `false` | Post concise PASS/violation comments when a token is also present |
| `GITHUB_API_URL` | `https://api.github.com` | Override for GitHub Enterprise Server or tests |
| `APP_PORT` | `8000` | Host port used by the Compose app service |

Never commit `.env`. It is ignored, while `.env.example` contains placeholders
and local-only example credentials.

## Docker full stack

Build the application image, start PostgreSQL, migrate, and run FastAPI:

```bash
cp .env.example .env
docker compose up --build
```

The app container waits for a healthy database, runs `alembic upgrade head`,
and connects to PostgreSQL through Compose service DNS at `db:5432`. The image
runs as an unprivileged `invariant` user.

Both published ports bind to `127.0.0.1` only, because the API has no
authentication and the database uses local example credentials. Put a reverse
proxy or tunnel in front of the app to receive GitHub webhooks.

Useful commands:

```bash
docker compose ps
docker compose logs app db
docker compose down
```

`docker compose down` preserves the named PostgreSQL volume. Do not add `-v` if
you want to keep local data.

## Migrations

```bash
uv run alembic upgrade head
uv run alembic current
uv run alembic check
```

The v0.2 migration adds nullable GitHub fields to existing projects, then creates
analysis run and violation tables. It does not delete or rewrite v0.1 project or
rule data.

Migration `20261024_0003` changes the violation `imported_module` and `message`
columns to `TEXT`, because a valid import can exceed the previous 500- and
1,000-character limits. Downgrading fails rather than truncating if a longer
value is stored.

## API

### Projects and rules

```bash
curl -X POST http://127.0.0.1:8000/projects \
  -H "Content-Type: application/json" \
  -d '{
    "name": "payments",
    "description": "Payments service",
    "github_owner": "example-org",
    "github_repo": "payments"
  }'

curl http://127.0.0.1:8000/projects
curl http://127.0.0.1:8000/projects/1
```

Lists are paginated: `GET /projects` and `GET /projects/{id}/rules` return
100 items by default (`limit` 1–500, `offset` ≥ 0), and every list response
carries the total number of matching items in an `X-Total-Count` header.

`PATCH /projects/{id}` changes only the fields it sends: `name`,
`description`, the GitHub pair (set both, or clear both with null),
`source_roots`, `exclude_paths` and `fail_on`. Associate an existing v0.1
project with GitHub:

```bash
curl -X PATCH http://127.0.0.1:8000/projects/1 \
  -H "Content-Type: application/json" \
  -d '{"github_owner":"example-org","github_repo":"payments"}'
```

`DELETE /projects/{id}` returns `204` and deletes the project **together with
its rules and its entire analysis history**.

Rules of every type share `key`, `name`, `enabled`, `severity`,
`source_pattern` and `exclude_paths`. `rule_type` defaults to
`forbidden_import` and `key` to a slug of the name, so v0.1/v0.2 request
bodies keep working:

```bash
curl -X POST http://127.0.0.1:8000/projects/1/rules \
  -H "Content-Type: application/json" \
  -d '{
    "name":"domain isolation",
    "source_pattern":"app.domain.*",
    "forbidden_imports":["app.infrastructure"]
  }'

curl -X POST http://127.0.0.1:8000/projects/1/rules \
  -H "Content-Type: application/json" \
  -d '{"key":"small-files","rule_type":"max_file_lines","max_lines":400,"severity":"warning"}'

curl http://127.0.0.1:8000/projects/1/rules
curl http://127.0.0.1:8000/projects/1/rules/1
curl -X PATCH http://127.0.0.1:8000/projects/1/rules/1 \
  -H "Content-Type: application/json" -d '{"enabled":false}'
curl -X DELETE http://127.0.0.1:8000/projects/1/rules/1
```

Keys and names are unique within a project (`409` otherwise). A rule is
reachable only through its own project, so another project's rule ID returns
`404`. `PATCH` validates the merged rule again and cannot change `rule_type`.
Deleting a rule returns `204`; past violations keep the rule key and name they
were recorded with, and their `rule_id` becomes null.

### Configuration export and import

A project's settings and rules can be exported as an
[`invariant.toml`](docs/configuration.md) file and imported back:

```bash
curl http://127.0.0.1:8000/projects/1/config -o invariant.toml

curl -X POST http://127.0.0.1:8000/projects/1/config/import \
  -H "Content-Type: application/json" \
  -d "$(jq -n --rawfile content invariant.toml '{content: $content, dry_run: true}')"
```

Import matches rules by `key` and answers with the plan: `created`, `updated`,
`deleted`, `unchanged` and `project_changes`. With `dry_run: true` nothing is
changed. `mode` is `merge` (default: rules missing from the file stay) or
`replace` (they are deleted). Everything is applied in one transaction. An
invalid file returns `422 invalid_config` with each problem and its location,
for example `rules[0].max_lines`.

### Direct code analysis

```bash
curl -X POST http://127.0.0.1:8000/projects/1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "files":[{
      "path":"app/domain/payment.py",
      "content":"from app.infrastructure.db import repository\n"
    }]
  }'
```

Direct analysis records a run and returns it. Abridged:

```json
{
  "id": 42,
  "project_id": 1,
  "source": "direct",
  "status": "completed",
  "outcome": "fail",
  "passed": false,
  "fail_on": "error",
  "files": {"total": 1, "eligible": 1, "analyzed": 1, "excluded": 0, "skipped": 0, "ignored": 0},
  "rules": {"total": 1, "enabled": 1, "evaluations": 1},
  "summary": {"errors": 1, "warnings": 0, "analysis_errors": 0},
  "error": null,
  "started_at": "2026-10-24T08:00:00.035517Z",
  "finished_at": "2026-10-24T08:00:00.037113Z",
  "duration_ms": 2,
  "engine_version": "0.3.0.dev0",
  "config_hash": "854dd572b1b82b530ac58bcead403f192029ad166ca0cfaaf8d941fe7911f97a",
  "files_analyzed": 1,
  "rules_evaluated": 1,
  "violation_count": 1,
  "config": {"version": 1, "project": {"source_roots": ["."], "exclude_paths": [], "fail_on": "error"}, "rules": ["..."]},
  "violations": [{
    "id": 73,
    "kind": "violation",
    "code": "forbidden_import",
    "severity": "error",
    "rule_id": 1,
    "rule_key": "domain-isolation",
    "rule_name": "Domain isolation",
    "path": "app/domain/payment.py",
    "line": 1,
    "column": 1,
    "imported_module": "app.infrastructure.db",
    "message": "app.domain.payment must not import app.infrastructure (matched app.infrastructure.db)",
    "data": {"imported_module": "app.infrastructure.db", "forbidden": "app.infrastructure"}
  }]
}
```

`violations` holds every diagnostic: rule violations, and `error` entries for
files that could not be analyzed. A file with a syntax error, or one too
deeply nested to parse, no longer rejects the request: the other files are
still checked, and the run is `fail` if one of them violates a rule and
`incomplete` otherwise. File paths must be relative, end in `.py`, and contain
no `..` segment and no control characters. Project and rule names may not
contain control characters either; descriptions may contain tabs and line
breaks.

### Results

A run's `status` says whether the analysis ran; its `outcome` says what a
completed run found:

| `status` | `outcome` | `passed` | Meaning |
| --- | --- | --- | --- |
| `completed` | `pass` | `true` | Every applicable check ran; no violation reaches `fail_on` |
| `completed` | `fail` | `false` | A violation reaches `fail_on` |
| `completed` | `incomplete` | `null` | A file could not be analyzed, so there is no verdict |
| `completed` | `not_applicable` | `null` | Nothing was checked: no eligible file, or no enabled rule applies |
| `failed` | `null` | `null` | The analysis could not run; `error` has a safe `code` and `message` |
| `queued`, `running` | `null` | `null` | Reserved for background analysis |

The database enforces these combinations. Warnings never fail the default
`fail_on = "error"`. `files`, `rules` and `summary` count what was checked
(`files.total = eligible + excluded + ignored`, `eligible = analyzed +
skipped`); runs recorded by v0.2 keep their verdict, with the counters v0.2
did not record set to `null`. The flat `files_analyzed`, `rules_evaluated`
and `violation_count` fields are kept for v0.2 clients. `config` is the
project settings and rules as they were at analysis time, so editing or
deleting a rule never rewrites an earlier result.

Changes from v0.2, starting with version 0.3.0.dev0: `passed` can be `null`; a file that
cannot be parsed makes the run `incomplete` instead of a `422 invalid_python`;
incomplete, not-applicable and failed GitHub analyses are stored instead of
refused; a project without applicable rules no longer passes.

### Manual GitHub pull request analysis
### Manual GitHub pull request analysis

The project must have GitHub owner/repository configuration:

```bash
curl -X POST http://127.0.0.1:8000/projects/1/analyze/github \
  -H "Content-Type: application/json" \
  -d '{"pull_number":12}'
```

Invariant fetches PR metadata and the changed-file list through GitHub's REST
API, downloads each changed Python file at the PR head SHA, runs the shared AST
analyzer, persists the result, and returns the full run. Removed and non-Python
files are outside the analysis.

Every analysis is recorded and returned as a run. If a changed `.py` file is
over 1 MB (`too_large`), not UTF-8 (`undecodable`) or has a path the API
rejects (`invalid_path`), or GitHub listed fewer files than the pull request
changes (`file_list_truncated`), the run is `incomplete` and lists each of
these as an `error` diagnostic. A pull request that changes no Python file, or
none that an enabled rule applies to, is `not_applicable`. Neither posts a PR
comment.

When GitHub cannot be used (404, rate limit, authorization failure, malformed
response, network failure), the run is recorded as `failed` with the error's
code, and the request returns the matching status with the run's ID, never
credentials or upstream payloads:

```json
{"detail": {"code": "github_rate_limited", "message": "GitHub API rate limit exceeded", "analysis_run_id": 43}}
```

### Analysis history
### Analysis history

```bash
curl http://127.0.0.1:8000/projects/1/analysis-runs
curl "http://127.0.0.1:8000/projects/1/analysis-runs?limit=20&offset=20"
curl "http://127.0.0.1:8000/projects/1/analysis-runs?outcome=fail&source=github_webhook"
curl http://127.0.0.1:8000/analysis-runs/42
curl "http://127.0.0.1:8000/analysis-runs/42/diagnostics?kind=violation&severity=error&limit=50"
curl http://127.0.0.1:8000/projects/1/stats
```

The project endpoint returns newest-first summaries, 50 per page by default
(`limit` 1–200, `offset` ≥ 0), filtered by any of `status`, `outcome`,
`source` and `pull_number`. The global run endpoint returns the summary plus
the configuration snapshot and every diagnostic. For large runs,
`GET /analysis-runs/{id}/diagnostics` pages through them in the engine's order
(path, line, column, rule key), 100 per page by default (`limit` 1–500),
filtered by any of `kind`, `severity`, `code`, `rule_key` and `path`. These
lists also carry `X-Total-Count`. Missing projects or runs return 404.

`GET /projects/{id}/stats` summarizes all of a project's runs:

```json
{
  "project_id": 1,
  "runs": 6,
  "by_status": {"queued": 0, "running": 0, "completed": 5, "failed": 1},
  "by_outcome": {"pass": 2, "fail": 1, "incomplete": 1, "not_applicable": 1},
  "pass_rate": {"passed": 2, "decided": 3, "without_verdict": 3, "rate": 0.6667},
  "diagnostics": {"errors": 1, "warnings": 1, "analysis_errors": 1},
  "duration": {"runs": 5, "average_ms": 2.4, "max_ms": 4},
  "latest": ["the ten newest run summaries"]
}
```

The pass rate is `passed / decided` over the runs with a verdict, pass or
fail. Incomplete, not-applicable, failed and unfinished runs have none, so
they are counted in `without_verdict` instead of silently lowering the rate.

`GET /analysis-runs/{id}/result` exports a run in the engine's own result
format (`schema_version` 1), the document `invariant check --format json`
prints, with a `source` (where the files came from, with commits) and a `run`
(status, error, timing, configuration hash) section. A client can read it
without knowing the rest of the API.

### Errors

Request validation errors return `422`. `503 Database unavailable` means
PostgreSQL could not be reached; any other database failure returns
`500 storage_error` without SQL or driver details and is logged on the server.

## GitHub webhook

Configure a strong random `GITHUB_WEBHOOK_SECRET`, then add a GitHub repository
webhook targeting:

```text
POST https://your-invariant-host/integrations/github/webhook
Content type: application/json
Events: Pull requests
```

Invariant validates `X-Hub-Signature-256` with HMAC-SHA256 and constant-time
comparison before parsing or acting on the payload. It analyzes pull request
actions `opened`, `synchronize`, and `reopened`; other events/actions return a
clean `202 ignored` response.

An analyzed delivery returns `200` with `{"status": "analyzed",
"analysis_run_id": ..., "outcome": ...}`, including for incomplete and
not-applicable results. A GitHub failure records a failed run and returns the
same error as the manual endpoint. The payload fields Invariant uses are
type-checked before use: a signed request with, for example, `"action": []`
or a string pull request number gets `400 invalid_webhook_payload`; an
unsigned request still gets `401`.

For relevant events, repository owner/name selects the configured project. The
`X-GitHub-Delivery` value is persisted with a unique constraint, so retrying the
same delivery returns the existing run (`"status": "duplicate"`) instead of
creating a duplicate, even when that run failed; analyze the pull request
again with the manual endpoint. Manual and webhook-triggered PR analyses use
the same code path.

### Optional PR feedback

Set both values below to opt in:

```dotenv
GITHUB_TOKEN=github-token-with-repository-comment-permission
GITHUB_POST_PR_COMMENT=true
```

Invariant posts either `Invariant: PASS` or a concise violation list, and only
for a verdict: incomplete, not-applicable and failed analyses post nothing. A comment
failure is logged without secrets and does not roll back or fail the completed
analysis. GitHub Check Runs are intentionally out of scope for v0.2.

## Testing and quality checks

External GitHub calls are mocked; tests require no token and do not mutate a
GitHub repository. SQLite (with foreign keys enabled) provides fast per-test
isolation. Set `INVARIANT_TEST_DATABASE_URL` to run the same suite against
PostgreSQL; the tests drop and recreate the schema, so use a disposable
database.

```bash
uv sync --dev
uv run pytest
INVARIANT_TEST_DATABASE_URL=postgresql+psycopg://invariant:invariant@localhost:5433/invariant_test \
  uv run pytest
uv run ruff check app examples migrations src tests
uv run ruff format --check app examples migrations src tests
uv run python -m compileall -q app migrations src tests
uv run invariant check .
```

GitHub Actions (`.github/workflows/ci.yml`) runs ruff, applies and checks the
migrations against PostgreSQL 18, and runs the tests on SQLite and PostgreSQL
for every pull request and every push to `main`. Its `cli` job builds the
wheel, installs it into a clean environment and runs the command-line tests
and the GitHub Actions example against that installation, then checks this
repository with its own `invariant.toml`.

Real local verification:

```bash
docker compose up -d db
uv run alembic upgrade head
uv run alembic current
uv run alembic check
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Security and limitations

- No GitHub token or webhook secret is stored in PostgreSQL.
- Webhook signatures are checked against the exact raw body before JSON parsing.
- Invariant never executes submitted or downloaded code.
- GitHub content is limited to changed, non-deleted `.py` files that decode as
  UTF-8 and are at most 1 MB. GitHub exposes at most 3,000 changed files for one
  pull request through this REST endpoint. A pull request beyond these limits
  is recorded as `incomplete`, never as a pass.
- History keeps results, diagnostics, counts, configuration snapshots and
  commit IDs, but never source code. There is no retention policy yet: runs
  are kept until their project is deleted.
- A failed webhook analysis is not retried, and redelivering the same
  delivery returns the failed run; the background worker planned for v1.0
  retries.
- Columns count Unicode characters, 1-based. Runs stored before this change
  recorded UTF-8 byte offsets, which differ only after non-ASCII text on the
  same line.
- Analysis is synchronous.
- Dynamic imports (`importlib.import_module`, `__import__`) are not detected.
- The service has no authentication, organizations, authorization model, web UI,
  background queue, or deployment hardening yet. Do not expose it publicly
  without an appropriate trusted network boundary.
- Repository-wide unchanged files are not analyzed; v0.2 evaluates changed PR
  files only.
- CI checks the code, the migrations and the tests on SQLite and PostgreSQL,
  but does not build or smoke-test the Docker image yet.

## Roadmap

The v1.0 requirements and what this branch already covers, with the tests
that prove it, are tracked in
[`docs/v1.0-traceability.md`](docs/v1.0-traceability.md).

Potential v0.3+ work belongs here rather than in the v0.2 implementation:

- GitHub App authentication and GitHub Checks.
- Semantic / LLM-assisted architecture rules with human review.
- Repository-wide dependency graphs and additional deterministic rule types.
- Multi-language analysis.
- Authentication, organizations, and authorization.
- Web UI and production deployment guidance.
