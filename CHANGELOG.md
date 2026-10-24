# Changelog

All notable changes to Invariant. From 1.0.0 on, versions follow semantic
versioning: a change that breaks the API, the command line, the configuration
file or the result format comes only with a new major version.

## 1.0.0 (2026-10-24)

The first stable release. The same analysis now runs from the `invariant`
command line, the API and GitHub pull requests, and the self-hosted service
is protected and verified end to end. To upgrade a v0.2 deployment, follow
[`docs/upgrade-from-v0.2.md`](docs/upgrade-from-v0.2.md).

### Added

- **The `invariant` command line.** `invariant init`, and `invariant check`
  for a whole directory or a Git diff (`--base`/`--head`, read from the head
  commit), with text or JSON output, exit codes 0/1/2, `--fail-on`,
  `--exclude` and `--strict`. A tested GitHub Actions example
  (`examples/github-actions.yml`) and a demo project (`examples/demo`). The
  wheel depends on pydantic alone.
- **Rule types `forbidden_call`, `no_relative_import` and
  `max_file_lines`**, beside `forbidden_import`. Every rule has a stable key,
  a severity, an on/off switch and its own exclusions. Projects have source
  roots (a `src/` layout), exclusions and a `fail_on` threshold.
- **`invariant.toml`, with a schema version.** It is read by the command
  line, and exported from and imported into a project by the API, with a dry
  run (`GET /projects/{id}/config`, `POST /projects/{id}/config/import`).
- **Complete run records.**
  - Every run has a `status` (`queued`, `running`, `completed`, `failed`)
    and, once completed, an `outcome`: `pass`, `fail`, `incomplete` or
    `not_applicable`.
  - Each run keeps counters for files, rules and diagnostics, its timing,
    the engine version, and a snapshot and hash of the configuration it
    checked.
  - The JSON export, `GET /analysis-runs/{id}/result`, is the same document
    the command line writes.
- **History.** Pagination with `X-Total-Count` on every list, history
  filters, paginated diagnostics (`GET /analysis-runs/{id}/diagnostics`) and
  project statistics (`GET /projects/{id}/stats`).
- **A worker (`python -m app.worker`) and a durable queue in PostgreSQL.**
  Workers take runs under leases. Transient GitHub errors are retried with
  exponential backoff, never sooner than `Retry-After`, up to an attempt
  limit, and a run whose worker died is picked up again.
- **One pull request comment, kept current.**
  - The comment names the commit and the outcome, lists up to ten findings,
    and says what was checked and what static analysis cannot see.
  - It has its own retries, and its state is the run's `feedback`.
  - A run of an older commit, or one older than the run the comment shows,
    is never published.
- **Reliable GitHub reads.**
  - Each run reads one consistent pull request snapshot.
  - Downloads are streamed, with limits of 1 MB per file, 10 MB per response
    and 50 MB per pull request, and a job time limit (`WORKER_JOB_SECONDS`).
- **Errors and logs.**
  - One error format, carrying the request ID (`X-Request-ID`).
  - JSON logs that follow a run from its delivery to its comment.
- **Protection.**
  - An API key, `INVARIANT_API_KEY`. Without one, the API refuses to start
    unless `INVARIANT_LOCAL_ONLY=true` says that only this machine can
    reach it.
  - Restrictive CORS (`INVARIANT_CORS_ORIGINS`).
  - Limits on bodies (413), on analyses per client (429), on failed keys
    (429), on concurrent analyses (429) and on rules per project (409).
- **Operations.**
  - Readiness at `/ready`, and container health checks for the API and the
    worker.
  - Retention (`INVARIANT_RETENTION_DAYS`, `python -m app.retention`).
  - Documented backup and restore with `pg_dump` and `pg_restore`.
  - `python -m app.server` to serve the API.
- **Verification in CI.**
  - The test suite runs on PostgreSQL 16, 17 and 18.
  - The Docker image is built, and the Compose stack is smoke-tested,
    backup and restore included.
  - The wheel is installed in a clean environment.
  - The command line, the API and the GitHub worker are checked to give the
    same diagnostics.

### Changed

These changes can break a v0.2 client; the
[upgrade guide](docs/upgrade-from-v0.2.md) says what to do about each.

- **Authentication.** Every business route requires
  `Authorization: Bearer <INVARIANT_API_KEY>`. Health, readiness, the
  documentation and the HMAC-signed webhook do not.
- **Errors** are `{"error": {"code", "message", "request_id", "fields"?}}`
  instead of `{"detail": ...}`, with specific codes.
- **GitHub analyses run in the worker.** Both the manual endpoint and the
  webhook answer `202` with a `queued` run, and the result appears in the run
  later. The webhook's statuses are `queued`, `duplicate` and `ignored`.
  Without a worker, GitHub analyses wait.
- **Verdicts.**
  - `passed` is `null` for a run without a verdict.
  - A file that cannot be parsed makes the run `incomplete` instead of
    `422 invalid_python`.
  - Incomplete, not-applicable and failed analyses are stored instead of
    refused.
  - A project without applicable rules no longer passes.
- **Lists are paginated.**
  - Projects and rules, which returned everything, now return 100 items by
    default and at most 500, like runs and diagnostics.
  - Runs were limited to 50 by default (200 at most), now to 100 (500).
- **Comments.** With comments enabled, incomplete and not-applicable runs
  get a comment that says they have no verdict, and each pull request has
  one comment that is edited instead of a new comment per run.
- **Request limits.** Direct analyses over 16 MiB are refused (413), and so
  are more than 60 analyses a minute from one client (429).
- **Packaging.** The image serves the API with `python -m app.server` and
  logs JSON. The service's dependencies moved to the `server` dependency
  group, so install it with `uv sync` rather than `pip install .`.

### Fixed

- An incomplete GitHub analysis reported PASS; a pull request without Python
  files passed instead of being `not_applicable` (review finding R1).
- Long valid diagnostics failed to store on PostgreSQL, and data errors were
  reported as `503 Database unavailable` (R2).
- Malformed input caused `500` instead of a `4xx` (R3).
- A comment that failed to publish because of an unexpected error stopped
  the worker, in a loop of restarts.
- Redirects from renamed repositories were read as file contents.
- Compose did not pass the `WORKER_*` settings from `.env` to the containers.
- The image did not build once the package metadata named the README.

## 0.2.0

The service: projects and `forbidden_import` rules, direct analysis of
submitted files, GitHub pull request analysis by hand or through a signed
webhook, an audit history, and optional pull request comments.
