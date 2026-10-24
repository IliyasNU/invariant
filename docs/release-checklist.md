# v1.0.0 release checklist

The conditions the v1.0 specification sets for releasing v1.0.0, each with
the evidence that it holds. Test names are `file::function` under `tests/`;
CI jobs are those of `.github/workflows/ci.yml`.

- [x] **Every required ID is tied to code and a check, with no unproven
  "done".** All 59 rows of [`v1.0-traceability.md`](v1.0-traceability.md),
  R1–R5 included, name their implementation and the tests or CI job that
  prove them. Rows proven by CI alone (R5, O01, O03–O06) were proven by the
  `docker` job's first green run, on #13.
- [x] **R1–R3 are closed, and a `src/` layout works through explicit
  configuration.** Rows R1–R4: for example
  `test_github::test_incomplete_github_analysis_records_no_pass`,
  `test_api::test_long_diagnostics_are_stored_and_read_back` (it fails on
  PostgreSQL before the fix), and
  `test_engine::test_src_layout_resolves_modules_under_the_source_root`.
- [x] **The command line, the API and GitHub give the same diagnostics for
  the same input.**
  `test_parity::test_the_cli_the_api_and_github_report_the_same` runs one
  configuration and one set of files, with a finding of each rule type, a
  warning and a broken file, through `invariant check`, a direct analysis and
  the worker, and compares the results.
- [x] **An incomplete or not-applicable check cannot be mistaken for a full
  pass.**
  - Rows H01 and H02: `passed` is `null` without a verdict, and the counters
    say what was not analyzed.
  - The pull request comment says `INCOMPLETE` or `NOT APPLICABLE`, and
    `invariant check` exits 2 for them.
  - Tests: `test_runs::test_nothing_checked_is_not_applicable_rather_than_a_pass`,
    `test_feedback::test_every_outcome_has_its_own_headline`,
    `test_cli_process::test_exit_codes`.
- [x] **Projects and rules are fully managed, and history keeps its
  snapshots.**
  - Rows B01–B07, H03 and H05.
  - Tests: `test_rules_api` (all), `test_config_api` (all),
    `test_runs::test_editing_or_deleting_a_rule_does_not_rewrite_history`.
- [x] **Webhooks and jobs withstand redelivery, concurrency and restarts.**
  - Rows G02 and G03.
  - On PostgreSQL: `test_worker::test_concurrent_identical_deliveries_queue_one_job`
    and `::test_concurrent_workers_never_run_the_same_job`, plus
    `test_feedback::test_concurrent_publishers_leave_the_newest_run_on_the_comment`.
  - In CI's `docker` job, a delivery stays queued through a stopped worker
    and a restart of the API.
- [x] **The API is protected, submitted code is never run, and secrets stay
  out of results and logs.**
  - Rows S01, S02, S05, A08 and O02.
  - Tests: `test_security::test_every_business_route_requires_the_key`,
    `::test_no_secret_reaches_the_database`,
    `test_engine::test_analyzed_code_is_never_executed`,
    `test_logs::test_secrets_are_redacted_from_the_message_the_fields_and_the_traceback`.
- [x] **PostgreSQL migrations, preserved v0.2 data, Docker and a clean
  install of the command line are verified.**
  - `test_migrations::test_v02_data_survives_the_upgrade_and_the_api_runs_on_it`.
  - CI applies, checks and rolls back the migrations on PostgreSQL 16, 17
    and 18.
  - The `docker` job builds the image and runs the Compose stack, a restore
    of a backup included.
  - The `cli` job installs the wheel into a clean environment and runs the
    command-line tests against it.
- [x] **The README covers the quick start, a real demo, the configuration
  of the four rules, the API and its key, the webhook, the worker, the
  command line and CI, backup and restore, troubleshooting and the
  limitations.** `test_release::test_the_readme_covers_what_the_release_checklist_asks`
  checks the sections. The demo's commands and their output were run as
  written.
- [x] **One version in the package metadata, the API, the command line, the
  User-Agent and the release notes; a changelog and an upgrade guide from
  v0.2.**
  - `test_release::test_one_version_in_the_package_the_api_the_cli_and_github`
    and `::test_the_changelog_opens_with_this_version_and_its_upgrade_guide`.
  - [`CHANGELOG.md`](../CHANGELOG.md) and
    [`upgrade-from-v0.2.md`](upgrade-from-v0.2.md).
- [ ] **CI is green on the head that passes the final review, with no
  blocking finding left.** CI runs on every push of the release pull
  request; the final review, and the decision to merge and tag `v1.0.0`,
  are the owner's.
