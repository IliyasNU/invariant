# Command line

The `invariant` command checks a project on a developer machine or in CI. It
runs the same engine as the API and the GitHub integration, works offline and
needs no API, database, token or network.

## Installation

```bash
pip install "invariant @ git+https://github.com/IliyasNU/invariant@main"
```

Pin a release tag or commit instead of `main` for reproducible checks. The
command needs Python 3.14 or newer; the code it checks can target any Python
version that 3.14's parser accepts. The package contains only the engine and
the command, and depends only on pydantic. `python -m invariant` runs the same
command.

## `invariant init [PATH] [--force]`

Writes `PATH/invariant.toml` (default: the current directory) with comments,
`source_roots = ["src"]` when `PATH` has a `src` directory and `["."]`
otherwise, and one example `forbidden_import` rule for the first package it
finds. It refuses to overwrite an existing `invariant.toml` unless `--force`
is given, and refuses when `pyproject.toml` already has a `[tool.invariant]`
table. The format is described in [configuration.md](configuration.md).

## `invariant check [PATH] [options]`

`PATH` is the project root (default: the current directory). The
configuration is looked up there, as `invariant.toml` or `[tool.invariant]`
in `pyproject.toml`, and every path in it is relative to `PATH`.

| Option | Meaning |
| --- | --- |
| `--config FILE` | Use this configuration file instead |
| `--base REV` | Check only the files changed since the merge base with `REV` |
| `--head REV` | With `--base`: the commit to check (default `HEAD`) |
| `--format text\|json` | Output format (default `text`) |
| `--fail-on error\|warning` | Lowest severity that fails the check, instead of the configured `fail_on` |
| `--exclude GLOB` | Also exclude files matching this path glob; may be repeated |
| `--strict` | Treat "nothing was checked" (`not_applicable`) as an error |

### Which files are checked

Without `--base`, every file below `PATH` is considered and read from disk.
Hidden directories (`.git`, `.venv`, `.tox`, ...), directories that hold a
virtual environment (a `pyvenv.cfg`), `__pycache__` and `node_modules` are not
searched. Other files count as ignored, and source roots and exclusions
decide which Python files are analyzed.

With `--base`, only the files changed between the merge base of `BASE` and
`HEAD`, and `HEAD`, are considered, as a pull request shows them
(`git diff BASE...HEAD`). File contents and the configuration come from the
`HEAD` commit, never from the working tree, so uncommitted changes are not
checked; with `--config`, that file is used as given. A removed file counts as
ignored and a renamed file is checked under its new name. Names with spaces or
other unusual characters are read exactly. Revisions are resolved to commit
IDs before use and passed to Git as arguments, never through a shell, and a
revision starting with `-` is refused. A shallow clone that lacks the base
commit is an error that says so; with `actions/checkout`, set
`fetch-depth: 0`.

A Python file is analyzed when it is a regular file of at most 1 MB that
decodes as UTF-8 (a byte order mark is allowed); the GitHub integration uses
the same rules. Any other Python file is listed with its reason and makes the
result incomplete, rather than being skipped quietly:

| Reason | File |
| --- | --- |
| `too_large` | Larger than 1 MB |
| `undecodable` | Not valid UTF-8 |
| `symlink` | A symbolic link: links are never followed, and a linked directory is not entered |
| `invalid_path` | A name that is not valid UTF-8, or contains a backslash or control characters |
| `unreadable` | Not readable, or not a regular file (such as a FIFO) |
| `syntax_error`, `too_complex` | Not parseable Python |

Exclude such a file to get a complete check without it.

### Output

The text format lists one line per diagnostic, sorted by path, line, column
and rule key, then the verdict and the counts:

```text
src/shop/api.py:1:1: warning [absolute] shop.api uses the relative import '.domain'; import it absolutely
src/shop/domain/order.py:1:1: error [domain-isolation] shop.domain.order must not import shop.infrastructure (matched shop.infrastructure)
src/shop/legacy.py: not analyzed [undecodable] The file is not valid UTF-8

FAIL: 1 violation at or above error, 1 warning
Files: 2 analyzed, 1 not analyzed, 0 excluded, 1 ignored (4 in total)
Rules: 2 of 2 enabled, applied 3 times
Configuration: invariant.toml
```

The verdict is `PASS`, `PASS with N warnings`, `FAIL`, `INCOMPLETE` or
`NOT APPLICABLE` with its reason, decided as described in
[configuration.md](configuration.md#results): a blocking violation fails the
check even when another file could not be analyzed, and without one, such a
file leaves no verdict (`INCOMPLETE: 1 file could not be analyzed, so there is
no verdict`). Control and formatting characters in file names and messages are
escaped.

With `--format json`, standard output holds exactly one JSON document: the
result described in [configuration.md](configuration.md#results)
(`schema_version`, `outcome`, `passed`, `fail_on`, `files`, `rules`,
`summary`, `diagnostics`), plus `source` with the `mode` (`directory` or
`git_diff`), the `config` used, and the `base` and `head` commit IDs in diff
mode. Messages go to standard error. When the configuration or the command
line cannot be used, standard output stays empty.

### Exit codes

| Code | When |
| --- | --- |
| `0` | `pass`, or `not_applicable` (nothing was checked; the output says so) |
| `1` | `fail`: a violation at or above the threshold |
| `2` | `incomplete`; `not_applicable` with `--strict`; an unusable configuration or command line; a Git error; an internal error |

## In CI

[`examples/github-actions.yml`](../examples/github-actions.yml) is a complete
GitHub Actions workflow: copy it to `.github/workflows/` in the project. It
checks the files a pull request changes, and the whole project on pushes to
`main`. It needs no secrets, only `fetch-depth: 0` for the diff mode.

The example is tested: this repository's CI installs the built wheel into a
clean environment and runs the workflow's own `invariant check` commands on
the demo project in [`examples/demo`](../examples/demo). The clean commit
passes, and a commit that makes the domain import infrastructure exits with
1 (`tests/test_cli_process.py`). The same CI job checks this repository's
engine with the `invariant.toml` at its root, and the changes of every pull
request.

Other CI systems run the same commands; the exit code decides the job's
status.
