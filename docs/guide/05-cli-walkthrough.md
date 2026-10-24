# 5. CLI walkthrough

The `invariant` command runs the same engine as the API, offline. It needs
no API, database, token or network, which makes it the right tool for a
developer machine and for CI. The full reference is [cli.md](../cli.md).

## Run it

From the checkout, through uv:

```bash
uv run invariant --help
uv run invariant --version      # invariant 0.3.0.dev0
```

Or installed on its own, as a CI job does. The wheel contains only the
engine and the CLI, with pydantic as the one dependency:

```bash
pip install "invariant @ git+https://github.com/IliyasNU/invariant@main"
```

> **Pending v1.0 (stage 5e):** install a release tag instead of `main` once
> `v1.0.0` exists.

To see for yourself that the wheel is standalone, build it and install it
into a fresh environment:

```bash
uv build --wheel -o /tmp/inv-dist
uv venv -p 3.14 /tmp/inv-venv
uv pip install -p /tmp/inv-venv/bin/python /tmp/inv-dist/*.whl
/tmp/inv-venv/bin/invariant --version                # invariant 0.3.0.dev0
/tmp/inv-venv/bin/python -c "import fastapi"         # ModuleNotFoundError: the server is not in it
/tmp/inv-venv/bin/invariant check examples/demo      # PASS
```

In the rest of this chapter, `invariant` means either the installed command
or `uv run --project ~/code/invariant invariant` (the path of your checkout)
when you are in another directory.

## Check the demo project

`examples/demo` is a tiny shop with a domain and an infrastructure package
and an `invariant.toml` with four rules.

```bash
uv run invariant check examples/demo
```

```text
PASS
Files: 5 analyzed, 0 not analyzed, 0 excluded, 1 ignored (6 in total)
Rules: 4 of 4 enabled, applied 14 times
Configuration: examples/demo/invariant.toml
```

`ignored` is the non-Python file (`invariant.toml` itself). The exit code is
`0`:

```bash
echo $?   # 0
```

## Write a configuration for a new project

`invariant init` writes a commented starter file. For a project with a `src`
directory it chooses `source_roots = ["src"]` and an example rule for the
first package it finds:

```bash
mkdir /tmp/myshop && cp -r examples/demo/src /tmp/myshop/ && cd /tmp/myshop
invariant init
```

```text
Wrote invariant.toml with an example rule for the package 'shop'.
Edit the rule to match your code, then run: invariant check .
```

```toml
version = 1

[project]
# Directories whose contents map to import names, e.g. ["src"] for a src layout.
source_roots = ["src"]
# Path globs of files that no rule checks, e.g. ["tests/", "**/migrations/*.py"].
exclude_paths = []
# The lowest severity that fails the check: "error" or "warning".
fail_on = "error"

# An example rule: change the module names to match your project.
[[rules]]
key = "domain-isolation"
name = "Domain code must not import infrastructure"
rule_type = "forbidden_import"
source_pattern = "shop.domain.*"
forbidden_imports = ["shop.infrastructure"]
```

Running `init` again refuses to overwrite it (exit code `2`) unless you pass
`--force`. Every field is explained in [configuration.md](../configuration.md).

## Make it fail

Put the demo in a Git repository, then break the rule on a branch:

```bash
cp -r examples/demo /tmp/demo && cd /tmp/demo
git init -q -b main && git add . && git commit -qm init
git checkout -qb feature
printf 'from shop.infrastructure.db import connect\n' >> src/shop/domain/order.py
git commit -qam "bad import"
invariant check .
```

```text
src/shop/domain/order.py:11:1: error [domain-isolation] shop.domain.order must not import shop.infrastructure (matched shop.infrastructure.db)

FAIL: 1 violation at or above error
…
```

Exit code `1`.

## Check only what a branch changed

This is what a pull request check does:

```bash
invariant check . --base main --head HEAD
```

```text
src/shop/domain/order.py:11:1: error [domain-isolation] shop.domain.order must not import shop.infrastructure (matched shop.infrastructure.db)

FAIL: 1 violation at or above error
Files: 1 analyzed, 0 not analyzed, 0 excluded, 0 ignored (1 in total)
Rules: 4 of 4 enabled, applied 4 times
Configuration: invariant.toml at c8405527beec
Changes: c8405527beec since its merge base with c79d1a50095e
```

Only the one changed file was analyzed. In diff mode the files **and the
configuration** come from the `HEAD` commit, not from your working tree:
uncommitted edits are not checked. In GitHub Actions, check out with
`fetch-depth: 0`, or the base commit is missing.

## A file that cannot be analyzed

```bash
printf 'def (:\n' > src/shop/domain/broken.py
invariant check .
```

```text
src/shop/domain/broken.py:1:5: not analyzed [syntax_error] invalid syntax
src/shop/domain/order.py:11:1: error [domain-isolation] shop.domain.order must not import shop.infrastructure (matched shop.infrastructure.db)

FAIL: 1 violation at or above error
Files: 5 analyzed, 1 not analyzed, 0 excluded, 1 ignored (7 in total)
```

Still `FAIL`, because a real violation was found. Remove that violation and
the same run says `INCOMPLETE` with exit code `2`: one file was not seen, so
there is no verdict. To check without the broken file on purpose:

```bash
invariant check . --exclude 'src/shop/domain/broken.py'
# Files: 5 analyzed, 0 not analyzed, 1 excluded, 1 ignored (7 in total)
```

## Exit codes

| Code | When |
| --- | --- |
| `0` | `pass`, or `not_applicable` (use `--strict` to make that `2`) |
| `1` | `fail` |
| `2` | `incomplete`, an unusable configuration or command line, a Git error |

A CI job only needs the exit code.

## JSON for programs

```bash
invariant check . --format json
```

Standard output holds exactly one JSON document, the same format as the
API's `GET /analysis-runs/{id}/result`; messages go to standard error.

## In CI

Copy [`examples/github-actions.yml`](../../examples/github-actions.yml) to
`.github/workflows/` in the project to check. It checks the changed files of
each pull request and the whole project on pushes to `main`. This
repository's own CI runs those commands against the installed wheel, and
checks its own engine with its root `invariant.toml`.

Next: [GitHub webhooks](06-github-webhooks.md).
