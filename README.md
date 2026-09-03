# Invariant

Invariant is a small backend service for recording project-specific architecture
rules and checking proposed Python source changes against them. The v0.1 API lets
you create projects, attach forbidden-import rules, and receive precise,
machine-readable violations before architectural boundaries erode.

## Architecture

- **FastAPI** exposes health, project, rule, and analysis endpoints.
- **SQLAlchemy 2** provides the persistence model and session lifecycle.
- **Alembic** owns schema migrations.
- **PostgreSQL 18** runs locally through Docker Compose.
- The analyzer uses Python's built-in `ast` module. It does not execute submitted
  source code.

`Project` is the aggregate root. Each `Rule` belongs to one project and stores a
source-module glob plus one or more forbidden module prefixes.

## Local setup

Requirements: [uv](https://docs.astral.sh/uv/) and Docker with Compose.

```bash
cp .env.example .env
uv sync --dev
docker compose up -d db
uv run alembic upgrade head
uv run fastapi dev app/main.py
```

The API is available at <http://127.0.0.1:8000>, with interactive documentation
at <http://127.0.0.1:8000/docs>.

Configuration is read from `DATABASE_URL` when it is set. Otherwise the app
builds the URL from `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`,
`POSTGRES_HOST`, and `POSTGRES_PORT`. Compose and `.env.example` use host port
`5433` by default because a host PostgreSQL installation commonly occupies
`5432`; PostgreSQL still listens on `5432` inside the container. Change
`POSTGRES_PORT` in `.env` if 5433 is also occupied.

Useful database commands:

```bash
docker compose ps
docker compose logs db
uv run alembic current
uv run alembic downgrade base
uv run alembic upgrade head
```

Never commit `.env`; it is ignored. `.env.example` contains development-only
example credentials and should be copied before use.

## API examples

Create and list projects:

```bash
curl -X POST http://127.0.0.1:8000/projects \
  -H "Content-Type: application/json" \
  -d '{"name":"payments","description":"Payments service"}'

curl http://127.0.0.1:8000/projects
curl http://127.0.0.1:8000/projects/1
```

Add and list rules:

```bash
curl -X POST http://127.0.0.1:8000/projects/1/rules \
  -H "Content-Type: application/json" \
  -d '{
    "name":"domain isolation",
    "rule_type":"forbidden_import",
    "source_pattern":"app.domain.*",
    "forbidden_imports":["app.infrastructure","fastapi"]
  }'

curl http://127.0.0.1:8000/projects/1/rules
```

Analyze source files:

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

Example violation:

```json
{
  "project_id": 1,
  "files_analyzed": 1,
  "rules_evaluated": 1,
  "violations": [{
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

## How analysis works

For every submitted `.py` file, Invariant converts its relative path to a module
name (`app/domain/payment.py` becomes `app.domain.payment`). A rule applies when
that name matches its `source_pattern` glob. The AST walker collects `import` and
`from ... import ...` statements, then reports an import when it equals a
forbidden prefix or is one of its submodules. Results are sorted by file, line,
column, and rule ID, so the same input and rules always produce the same output.

Malformed Python is rejected with a structured `422 invalid_python` response.
Input paths must be relative Python paths, duplicate names return `409`, and
unknown projects return `404`.

## Tests

```bash
uv run pytest
```

The API tests use an isolated in-memory SQLite database for speed. Running the
Alembic migration and `/db-health` against Compose verifies the PostgreSQL path.

## Roadmap

- Analyze Git diffs and repository checkouts directly.
- Add more rule types, including dependency direction and module ownership.
- Add rule update/delete endpoints and project lifecycle operations.
- Add CI integrations and stable machine-readable report formats.
- Add authentication and team ownership only when multi-user deployment needs it.
