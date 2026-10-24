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
- An **AnalysisRun** records its source (`direct`, `github_pr`, or
  `github_webhook`), status, pass/fail result, repository/PR metadata, counts,
  and creation time.
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

Direct analysis now returns and persists an analysis run while preserving the
v0.1 summary and violation fields:

```json
{
  "id": 42,
  "project_id": 1,
  "source": "direct",
  "status": "completed",
  "passed": false,
  "pull_number": null,
  "repository_owner": "example-org",
  "repository_name": "payments",
  "files_analyzed": 1,
  "rules_evaluated": 1,
  "violation_count": 1,
  "created_at": "2026-09-06T08:00:00Z",
  "violations": [{
    "id": 73,
    "analysis_run_id": 42,
    "rule_id": 1,
    "rule_name": "domain isolation",
    "path": "app/domain/payment.py",
    "line": 1,
    "column": 1,
    "imported_module": "app.infrastructure.db",
    "message": "app.domain.payment must not import app.infrastructure (matched app.infrastructure.db)"
  }]
}
```

Violations carry `kind`, `code` (the rule type), `severity`, `rule_key` and
type-specific `data` in addition to the fields above. `passed` is false only
when a violation reaches the project's `fail_on` severity: warnings are
reported but pass by default.

Malformed Python returns a structured `422 invalid_python` response and does not
create a completed analysis run. File paths must be relative, end in `.py`, and
contain no `..` segment and no control characters. Project and rule names may
not contain control characters either; descriptions may contain tabs and line
breaks.

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

A run means something only if every changed Python file was checked. In two
cases Invariant therefore returns a structured `422` instead of a run, and posts
no PR comment:

| Code | When | Extra fields |
| --- | --- | --- |
| `analysis_incomplete` | A changed `.py` file is over 1 MB (`too_large`), is not UTF-8 (`undecodable`) or has a path the API rejects (`invalid_path`); or GitHub listed fewer files than the PR changes | `skipped` (`path`, `reason`), `file_list_truncated` |
| `not_applicable` | The PR changes no Python files, or no enabled rule applies to the changed ones (for example, all are excluded) | `ignored_files` |

v0.2 does not store these outcomes. Persisted `incomplete` and
`not_applicable` results are planned for v1.0.

GitHub 404s, rate limits, authorization failures, malformed responses, and
network failures are translated to structured API errors without exposing
credentials or upstream payloads.

### Analysis history

```bash
curl http://127.0.0.1:8000/projects/1/analysis-runs
curl "http://127.0.0.1:8000/projects/1/analysis-runs?limit=20&offset=20"
curl http://127.0.0.1:8000/analysis-runs/42
```

The project endpoint returns newest-first summaries, 50 per page by default
(`limit` 1–200, `offset` ≥ 0). The global run endpoint
returns the summary plus persisted violations. Missing projects or runs return
404.

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

A pull request without Python files returns `202` with status
`not_applicable`. An incomplete analysis returns the same
`422 analysis_incomplete` as the manual endpoint. The payload fields Invariant
uses are type-checked before use: a signed request with, for example,
`"action": []` or a string pull request number gets
`400 invalid_webhook_payload`; an unsigned request still gets `401`.

For relevant events, repository owner/name selects the configured project. The
`X-GitHub-Delivery` value is persisted with a unique constraint, so retrying the
same delivery returns the existing run instead of creating a duplicate. Manual
and webhook-triggered PR analyses use the same code path.

### Optional PR feedback

Set both values below to opt in:

```dotenv
GITHUB_TOKEN=github-token-with-repository-comment-permission
GITHUB_POST_PR_COMMENT=true
```

Invariant posts either `Invariant: PASS` or a concise violation list, and only
for a stored run: incomplete and not-applicable analyses post nothing. A comment
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
  is reported as `analysis_incomplete`, never analyzed partially, and that
  outcome is not stored in the history.
- Direct analysis of a project without rules, or of files that no rule
  covers, still produces `passed: true`; such runs are not yet told apart from
  a real pass (GitHub analysis refuses them as `not_applicable`).
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
