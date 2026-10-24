# Invariant for new developers

This guide is for a junior Python developer who opens this repository for the
first time. It explains what Invariant does, how its parts fit together, how
to run it on your own machine, and how to learn the code by changing it.

The [README](../../README.md), [configuration.md](../configuration.md) and
[cli.md](../cli.md) are the reference: every setting, route and limit is
listed there. This guide is the path through them. Where the two disagree,
the reference and the code win; please fix the guide.

## Reading order

| # | Chapter | You will be able to |
| --- | --- | --- |
| 1 | [Architecture](01-architecture.md) | Name every component, and follow one request and one GitHub run through the code |
| 2 | [Install on Windows 11 with WSL](02-install-windows-wsl.md) | Get a working checkout, Python 3.14 and uv inside Ubuntu |
| 3 | [Docker Compose and PostgreSQL](03-docker-postgres.md) | Start the database, migrate it, inspect it, run the full stack |
| 4 | [API walkthrough](04-api-walkthrough.md) | Create a project and rules, analyze code, read the history |
| 5 | [CLI walkthrough](05-cli-walkthrough.md) | Check a project offline and in CI, read exit codes |
| 6 | [GitHub webhooks](06-github-webhooks.md) | Send a signed webhook to a local stack and watch the worker comment |
| 7 | [Common errors and debugging](07-troubleshooting.md) | Recognise the usual failures and find the cause |
| 8 | [Study guide](08-study-guide.md) | Work through the code in a week, with exercises |
| 9 | [Production deployment](09-production-deployment.md) | Deploy the stack behind HTTPS with the right secrets |

On Linux or macOS, skip chapter 2 and install [uv](https://docs.astral.sh/uv/)
and Docker the usual way; every later command is the same.

## What was verified

Every command in chapters 3 to 7 was run, and its output pasted from that
run, on 24 October 2026, against commit `7207c4f` (branch
`claude/v1-operations`, stage 5d of the v1.0 plan), on Ubuntu 24.04 with uv
0.11, Python 3.14 fetched by uv, Docker Engine 29 and the `postgres:18` image:

- `uv sync --locked --dev`, `alembic upgrade head`, `alembic check`;
- the API with an API key: projects, rules, direct analysis (pass, fail,
  incomplete, not applicable), history, diagnostics, stats, result export,
  configuration export and import, and the error responses shown;
- the CLI: `init`, `check` on the demo project, JSON output, Git diff mode,
  `--exclude`, exit codes 0, 1 and 2, and the built wheel in a clean
  virtual environment without the server's dependencies;
- a signed webhook and a manual pull request analysis, run by the worker
  against `scripts/github_stub.py` (a stand-in GitHub), with the pull
  request comment it wrote and then updated; a repeated delivery; `SIGTERM`
  on the worker;
- `pg_dump` and `pg_restore` into a new database, `app.retention --dry-run`;
- the test suite: 438 passed and 4 skipped on SQLite, 442 passed on
  PostgreSQL 18.

Not verified by hand here:

- **Building the Docker image** (`docker compose up --build`): the sandbox
  that wrote this guide could not download the `ghcr.io/astral-sh/uv` base
  image. CI's `docker` job builds the image and smoke-tests the whole stack on
  every change, and it passed on `7207c4f`
  ([run 40072970223](https://github.com/IliyasNU/invariant/actions/runs/40072970223)).
- **Windows 11 and WSL** (chapter 2): the Ubuntu commands are the ones above;
  the Windows-side steps follow Microsoft's and Docker's documentation and
  were not executed.
- **The reverse proxy and TLS** in chapter 9 are an example configuration,
  not tested.

## Still changing

The v1.0 plan ([v1.0-traceability.md](../v1.0-traceability.md)) is in its
last stage. Sections marked like this may change before the release:

> **Pending v1.0 (stage 5e):** the version number (`0.3.0.dev0` today), the
> changelog and the upgrade guide.

Error codes, limits, the API key and logs (stages 5a to 5c) are implemented
on this branch. Their tables live in the [README](../../README.md) only, so that
there is one place to update; this guide links there instead of copying
them.
