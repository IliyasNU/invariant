# 3. Docker Compose and PostgreSQL

Invariant stores everything in PostgreSQL (16, 17 or 18; Compose uses 18).
There are two ways to run it locally:

- **Database in Docker, code on your machine** — for development. You edit
  a file and `fastapi dev` reloads it.
- **The full stack in Docker** — the database, a one-off migration, the API
  and the worker, built from the `Dockerfile`, as in production.

## `compose.yaml` in one picture

```text
db (postgres:18)  ──healthy──▶  migrate (alembic upgrade head, runs once)
                                   │ completed successfully
                         ┌─────────┴─────────┐
                         ▼                   ▼
                app (python -m app.server)   worker (python -m app.worker)
                127.0.0.1:8000               no port
```

- `db` publishes PostgreSQL's `5432` on host port **5433**
  (`POSTGRES_PORT`), so it does not collide with a PostgreSQL already
  installed on your machine. Its data lives in the named volume
  `postgres_data`.
- `migrate`, `app` and `worker` use one image, `invariant`, built from the
  `Dockerfile`, and run as the unprivileged user `invariant`. Inside the
  Compose network they reach the database at `db:5432`.
- Both published ports bind to `127.0.0.1` only. Nothing is reachable from
  other machines.

## Settings: `.env`

```bash
cp .env.example .env
```

`.env` is read by Docker Compose (for the containers) and by
`app/config.py` when you run the code with `uv run` **from the repository
root**: pydantic-settings looks for `.env` in the current directory. A
variable set in your shell wins over `.env`. `.env` is in `.gitignore`;
never commit it.

The API refuses to start without a key (see
[API key and exposure](../../README.md#api-key-and-exposure)). For local work
choose one:

```bash
# A real key, as in production (recommended: you learn to send it)
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
# put the output after INVARIANT_API_KEY= in .env

# Or no key, for an API only this machine can reach
echo INVARIANT_LOCAL_ONLY=true >> .env
```

Once a key is set, it is required even with `INVARIANT_LOCAL_ONLY=true`.

## Way 1: database in Docker, code on your machine

```bash
docker compose up -d db
docker compose ps                 # wait for "(healthy)"
uv run alembic upgrade head       # create or update the tables
uv run alembic current            # 20261024_0007 (head)
```

Run the API, with reload on every change:

```bash
uv run fastapi dev app/main.py
```

or as the container runs it, with structured logs (`INVARIANT_LOG_FORMAT=text`
makes them readable in a terminal):

```bash
INVARIANT_LOG_FORMAT=text uv run python -m app.server
```

And, in a second terminal, the worker, needed only for GitHub analyses:

```bash
INVARIANT_LOG_FORMAT=text uv run python -m app.worker
```

Check that it is up:

```bash
curl http://127.0.0.1:8000/health    # {"status":"ok"}         the process runs
curl http://127.0.0.1:8000/ready     # {"status":"ready"}      the DB answers and is migrated
```

Open <http://127.0.0.1:8000/docs> for the interactive API documentation.
To call protected routes from there, click **Authorize** and paste the key.

## Way 2: the full stack

```bash
docker compose up -d --build
docker compose ps                    # app and worker become (healthy)
docker compose logs -f app worker    # Ctrl+C stops following, not the stack
```

`docker compose ps` shows `migrate` as exited with code 0: it ran the
migrations and stopped, which is correct. `app` and `worker` start only
after it succeeded, so several copies never race to change the schema.

> **Verification:** CI's `docker` job builds this image and runs
> `scripts/compose_smoke.py` against the stack on every change (signed
> webhook, worker, comment, restart, backup and restore). The image build
> itself was not repeated by hand for this guide; see
> [What was verified](README.md#what-was-verified).

Stop and start again:

```bash
docker compose stop       # the worker finishes its current run first (up to 2 minutes)
docker compose up -d
docker compose down       # removes the containers, keeps the data volume
```

`docker compose down --volumes` (or `-v`) **deletes the database**. Use it
only when you mean to start from nothing.

## Looking inside PostgreSQL

```bash
docker compose exec db psql -U invariant
```

```text
invariant=# \dt
                   List of tables
 Schema |         Name          | Type  |   Owner
--------+-----------------------+-------+-----------
 public | alembic_version       | table | invariant
 public | analysis_runs         | table | invariant
 public | analysis_violations   | table | invariant
 public | projects              | table | invariant
 public | pull_request_comments | table | invariant
 public | rules                 | table | invariant
```

Useful queries while you learn:

```sql
SELECT id, name, github_owner, github_repo, source_roots FROM projects;
SELECT id, project_id, key, rule_type, enabled, severity FROM rules;
SELECT id, source, status, outcome, attempts, next_attempt_at, locked_by
  FROM analysis_runs ORDER BY id DESC LIMIT 10;
SELECT * FROM alembic_version;
```

Read, but do not change rows by hand while the worker runs: the queue
columns (`status`, `locked_by`, `lease_expires_at`) are its locks.

A desktop client (DBeaver, pgAdmin, DataGrip) connects to `localhost:5433`,
database, user and password `invariant`, as in `.env`.

## Migrations

Migrations are in `migrations/versions/`, named
`YYYYMMDD_NNNN_slug.py`. The application never creates tables itself.

```bash
uv run alembic upgrade head      # apply everything
uv run alembic current           # where the database is
uv run alembic history           # all migrations
uv run alembic check             # the models and the migrations agree
uv run alembic downgrade -1      # undo the last one (development only)
```

When you change `app/models.py`, write a migration:

```bash
uv run alembic revision --autogenerate -m "add rule description"
```

Read the generated file before you trust it, rename it to the
`YYYYMMDD_NNNN_slug.py` pattern, make the downgrade work, and make sure
existing rows survive: the project's migrations never drop or rewrite user
data, and a downgrade that would lose data fails instead. Then
`uv run alembic upgrade head && uv run alembic check`. CI applies, checks and
rolls back every migration on PostgreSQL 16, 17 and 18.

## Tests against PostgreSQL

`uv run pytest` uses an in-memory SQLite database. To run the same tests on
PostgreSQL, create a **separate, disposable** database: the tests drop and
recreate the schema.

```bash
docker compose exec -T db psql -U invariant -c "CREATE DATABASE invariant_test"
INVARIANT_TEST_DATABASE_URL=postgresql+psycopg://invariant:invariant@localhost:5433/invariant_test \
  uv run pytest
```

Four tests are skipped on SQLite because they need PostgreSQL (row locks, concurrent inserts, the real migrations);
on PostgreSQL none is skipped.

## Backup and restore

```bash
docker compose exec -T db pg_dump --username invariant --format custom invariant > invariant.dump
```

Restore into an empty database, for example a new one to look at:

```bash
docker compose exec -T db psql -U invariant -c "CREATE DATABASE restored"
docker compose exec -T db pg_restore --username invariant --dbname restored --no-owner < invariant.dump
```

The [README](../../README.md#backup-and-restore) shows the full restore of
the main database. The dump holds no secrets: they live only in the
environment.

Next: [API walkthrough](04-api-walkthrough.md).
