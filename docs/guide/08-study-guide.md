# 8. Study guide

A plan for your first week or two, about two hours a session. Each session
has something to read, something to run, and exercises. Do the exercises on
a local branch (`git checkout -b study/<topic>`); they are for learning, not
for merging.

Before you start, finish chapters 2 to 4, so `uv run pytest` passes and the
API answers.

Rules for every exercise:

- **Write the test first**, watch it fail, then make it pass.
- Run `uv run pytest`, `uv run ruff check app examples migrations scripts src tests`
  and `uv run ruff format --check app examples migrations scripts src tests`
  before you call it done. That is what CI does.
- If an exercise needs a change to the API, the database or the CLI, update
  the README too: the repository treats docs as part of the change.

## Session 1: the engine from the outside

**Read:** [configuration.md](../configuration.md) in full;
`src/invariant/result.py`.

**Run:** the [CLI walkthrough](05-cli-walkthrough.md) on `examples/demo`.

**Exercises**

1. Without running anything, predict the module name of
   `src/shop/domain/order.py` with `source_roots = ["."]`, then with
   `["src"]`. Check with a direct analysis and a rule whose `source_pattern`
   is the name you predicted.
2. Predict the `outcome` and `passed` of an analysis whose only file has a
   syntax error and also a forbidden import in another file. Then of one
   with only the syntax error. Check both with the CLI (the counts and exit
   codes are in chapter 5).
3. Explain in two sentences why `incomplete` and `not_applicable` have
   `passed: null` instead of `true`. What could go wrong in a CI pipeline if
   they were `true`?

## Session 2: inside the engine

**Read:** `src/invariant/engine.py` (`analyze`), `src/invariant/rules.py`,
`src/invariant/paths.py`. Then the tests that pin them:
`tests/test_engine.py`, `tests/test_engine_paths.py`.

**Run:** the engine snippet at the end of
[chapter 7](07-troubleshooting.md#debugging-techniques), with a
`breakpoint()` in `check_forbidden_import`. Step through one import.

**Exercises**

1. `from app import infrastructure` inside `app/domain/x.py`: is it
   reported by a rule forbidding `app.infrastructure`? Find the line in
   `rules.py` that decides, then the test in `tests/test_engine.py` that
   proves it.
2. Add a test that `app.infrastructure_extra` is **not** matched by
   `forbidden_imports = ["app.infrastructure"]`. (It probably passes at once;
   then find the existing test that already covers it.)
3. In `paths.py`, find why `["src", "src/app"]` is refused as source roots.
   Write down what would break if overlapping roots were allowed.

## Session 3: `forbidden_call` and Python scoping

**Read:** `src/invariant/calls.py` and `tests/test_engine_calls.py`. This is
the hardest module in the engine; take your time.

**Exercises**

1. For each line, as the whole of a file that starts with `import os`,
   decide whether a rule forbidding `os.system` reports it, then check with
   a direct analysis:

   ```python
   import os as o; o.system("x")
   from os import system as s; s("x")
   def f(os): os.system("x")
   run = os.system; run("x")
   ```

2. Read the section "What static analysis cannot see" in
   [configuration.md](../configuration.md#what-static-analysis-cannot-see).
   Pick one limitation and write a test that documents it: the call is
   *not* reported.

## Session 4: the API and the database

**Read:** `app/main.py` (`create_app`), `app/api.py`, `app/schemas.py`,
`app/models.py`, `app/analysis_service.py` (`persist_analysis`). Then
`tests/conftest.py` and `tests/test_api.py`.

**Run:** the [API walkthrough](04-api-walkthrough.md), and the queries of
[chapter 3](03-docker-postgres.md#looking-inside-postgresql) after it. Find
your runs and violations in the tables.

**Exercises**

1. Follow `POST /projects/{id}/analyze` from the route to the `INSERT`: list
   every function it passes through (chapter 1 has the outline). Where is
   the transaction committed?
2. Add an `enabled` filter to `GET /projects/{id}/rules`
   (`?enabled=false` lists disabled rules). Write the test in
   `tests/test_rules_api.py` first; keep `X-Total-Count` correct.
3. Delete a rule that has violations in the history. What happens to
   `rule_id`, `rule_key` and `rule_name` of those violations, and why is
   that the right behaviour? Find the `ondelete` that does it in
   `app/models.py`.

## Session 5: migrations

**Read:** `migrations/versions/` in order, especially
`20261024_0003_text_violation_diagnostics.py` (a downgrade that refuses to
lose data) and `tests/test_migrations.py`.

**Exercises**

1. On a throw-away database, run `uv run alembic downgrade base`, then
   `upgrade head`, then `alembic check`.
2. Add a nullable `description` column to rules: model, migration (named
   `YYYYMMDD_NNNN_slug.py`), schema, test. Run the migration up and down on
   PostgreSQL. Why must the column be nullable, or have a server default,
   for existing rows?
3. Run the suite on PostgreSQL (chapter 3). Which tests are skipped on
   SQLite, and why?

## Session 6: security and limits

**Read:** `app/security.py`, `app/limits.py`, `app/errors.py`, `app/logs.py`;
`tests/test_security.py`, `tests/test_limits.py`, `tests/test_errors.py`,
`tests/test_logs.py`; the README sections
[API key and exposure](../../README.md#api-key-and-exposure),
[Errors](../../README.md#errors), [Limits](../../README.md#limits),
[Logs](../../README.md#logs).

**Exercises**

1. Why does `require_api_key` use `hmac.compare_digest` instead of `==`?
   What is a timing attack?
2. Why is the webhook route outside the API key, and what protects it
   instead? Find the test that proves an unsigned webhook is refused.
3. Make the API return a `500` on purpose in a test (raise in a route via
   monkeypatch). Check that the body has `internal_error`, no traceback, and
   that the log line has the traceback and the request ID.

## Session 7: the worker and GitHub

**Read:** `app/worker.py`, `app/github.py`, `app/feedback.py`;
`tests/test_worker.py`, `tests/test_github.py`, `tests/test_github_limits.py`,
`tests/test_feedback.py`.

**Run:** the local webhook walkthrough in [chapter 6](06-github-webhooks.md).

**Exercises**

1. Copy `scripts/github_stub.py` to your scratch directory and make it
   answer `503` to the first two pull request requests. Watch the worker log
   `run_retry_scheduled` with growing delays (set `WORKER_RETRY_SECONDS=2`
   so you do not wait long). Which test in `tests/test_worker.py` proves the
   doubling?
2. Start two workers and queue five runs. Use the logs to show that no run
   is claimed twice. Which SQL clause guarantees it?
3. Explain `stale` in your own words, then read
   `test_a_run_whose_commit_is_no_longer_the_head_is_not_published` and
   `test_an_older_run_never_replaces_a_newer_ones_comment`.
4. Kill a worker with `kill -9` in the middle of a run (make the stub slow
   with `time.sleep`). With a short `WORKER_LEASE_SECONDS`, watch another
   worker log `run_worker_lost` and take the run over.

## Session 8: the CLI and CI

**Read:** `src/invariant/cli.py`, `src/invariant/git.py`,
`src/invariant/files.py`; `tests/test_cli.py`, `tests/test_cli_process.py`;
`.github/workflows/ci.yml`; `examples/github-actions.yml`.

**Exercises**

1. Build the wheel and install it in a clean venv (chapter 5). Why does CI
   run the CLI tests against the installed wheel and not the checkout?
2. Why does `--base` read files from the commit instead of the working tree?
   Construct a case where reading the working tree would give a wrong
   result in CI.
3. Add a `--quiet` option to `invariant check` that prints only the verdict
   line. Tests first; keep JSON output unchanged.

## Capstone: a new rule type

Add a rule type `no_star_import`: report every `from x import *`.

You will touch, and should understand why:

- `src/invariant/config.py`: a `NoStarImportRule` model, added to the
  `RuleConfig` union;
- `src/invariant/rules.py`: a checker, registered in `CHECKERS`;
- `app/analysis.py`: `_RULE_MODELS`;
- `app/schemas.py`: a `NoStarImportRuleCreate` in `RuleCreate`;
- tests in `tests/test_engine.py`, `tests/test_rules_api.py` and
  `tests/test_config_api.py` (export and import of the new type);
- [configuration.md](../configuration.md) and the README's data model.

No migration is needed: rule-specific fields live in the JSON column
`rules.params`. Check that yourself in `app/models.py`. Then run the full
suite on SQLite and PostgreSQL, ruff, and `uv run invariant check .` (does
your checker respect the engine's own `invariant.toml` rules?).

## Checklist: you know the codebase when you can

- [ ] Explain the four outcomes and when each happens.
- [ ] Draw the path of a webhook from GitHub to the PR comment, naming the
      modules.
- [ ] Say what happens when a worker dies mid-run, and when GitHub rate
      limits it.
- [ ] Write a migration that preserves existing data, with a working
      downgrade.
- [ ] Find any request in the logs by its request ID.
- [ ] Add a rule type end to end, with tests and docs.
