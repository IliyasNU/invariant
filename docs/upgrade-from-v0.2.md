# Upgrading from v0.2 to v1.0

v1.0 keeps every project, rule and run that v0.2 stored, and adds to them. It
also changes how the API is used: it needs a key, it answers errors in one
format, and GitHub analyses run in a separate worker. Upgrade in this order.

## 1. Back up the database

```bash
docker compose exec -T db pg_dump --username invariant --format custom invariant > before-v1.dump
```

The migrations are tested on v0.2 data (`tests/test_migrations.py`), but a
backup is what lets you go back. Restoring it is described in the README,
under "Backup and restore".

## 2. Give the API a key

Set a key of at least 32 characters in `.env` (or your secret store), and
give it to every client of the API:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

```dotenv
INVARIANT_API_KEY=the-generated-key
```

Without a key, the API refuses to start. Only a deployment that no other
machine can reach may run without one, with `INVARIANT_LOCAL_ONLY=true`.
`GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET` and `GITHUB_POST_PR_COMMENT` mean
what they meant. Set `INVARIANT_PUBLIC_URL` if comments should link to their
runs; every other new setting has a default (README, "Environment
variables").

## 3. Migrate

With Compose, the `migrate` service applies the migrations before the API
and the worker start:

```bash
docker compose up --build
```

Without Compose, run `uv run alembic upgrade head` once, before starting the
new version. What the migrations do to existing data:

| Migration | Existing data |
| --- | --- |
| `20261024_0003` | Violation messages and module names become unbounded text. |
| `20261024_0004` | Each rule gets a key made from its name, unique in its project, and becomes a `forbidden_import` rule with its modules as parameters. Projects get source roots `["."]`, no exclusions and `fail_on = "error"`. Violations become diagnostics of kind `violation`. |
| `20261024_0005` | Runs, all completed with a verdict in v0.2, get the outcome `pass` or `fail`. Counters and timing that v0.2 did not record stay null. |
| `20261024_0006` | Runs get `attempts = 1`; nothing is queued. |
| `20261024_0007` | A table for pull request comments; existing runs have no comment state and are never published. |

Each migration says, in its own file, what a downgrade removes. Going back to
v0.2 means restoring the backup from step 1.

## 4. Run the worker

GitHub analyses, whether manual or from the webhook, now run in a worker:

```bash
uv run python -m app.worker    # or the Compose service "worker"
```

Without a worker they wait in the queue, and nothing is lost: a worker
started later runs them.

## 5. Update the clients

| v0.2 | v1.0 |
| --- | --- |
| No authentication | `Authorization: Bearer <key>` on every route but `/health`, `/ready`, `/db-health`, the documentation and the webhook |
| Errors as `{"detail": ...}` | `{"error": {"code", "message", "request_id", "fields"?}}`; read `error.code` |
| `POST /projects/{id}/analyze/github` answered `200` with the finished run | `202` with a run in status `queued`; poll `GET /analysis-runs/{id}` until it is `completed` or `failed` |
| The webhook answered `analyzed` or `not_applicable` | `queued`, `duplicate` or `ignored`; the outcome is in the run |
| `passed` was `true` or `false` | `true` only for `pass`, `false` only for `fail`, otherwise `null`; read `status` and `outcome` |
| A file that did not parse: `422 invalid_python` | The run is `incomplete`, with an `error` diagnostic for the file |
| `GET /projects` and `GET /projects/{id}/rules` returned everything | 100 items by default, at most 500; the `X-Total-Count` header gives the total; page with `limit` and `offset` |
| Runs: 50 by default, at most 200 | 100 by default, at most 500 |

Request bodies of v0.2 keep working: a rule created with a `name` and
`forbidden_imports` is a `forbidden_import` rule, with a key made from the
name. Responses keep the v0.2 fields (`passed`, `files_analyzed`,
`rules_evaluated`, `violation_count`) alongside the new ones.

## 6. Pull request comments

With comments enabled, each pull request now gets one comment, edited after
every run, instead of a comment per run. The first v1.0 run on a pull request
that v0.2 commented on writes a new comment, because v0.2's comments were not
remembered. The old comments stay, and later runs edit only the new one.

## 7. Optionally, the command line

`invariant check` runs the same analysis without the service. The
configuration of a project can be exported for it:

```bash
curl -H "Authorization: Bearer $INVARIANT_API_KEY" \
  http://127.0.0.1:8000/projects/1/config -o invariant.toml
invariant check .
```

See `docs/cli.md` and `examples/github-actions.yml`.
