# Configuration and rules

Invariant checks Python code against architecture rules with one engine, the
`invariant` package in `src/invariant`. The API and the GitHub integration
already run on it, and the upcoming `invariant check` command reads the file
format described here. One schema validates the file, API payloads and stored
rules, so a rule means the same thing everywhere.

The API stores the same settings and rules, and can export a project as this
file or import one (see the README). The `invariant check` command that reads
it locally comes with the CLI.

## The configuration file

Put an `invariant.toml` in the repository root, or a `[tool.invariant]` table
in `pyproject.toml` with the same content. If both exist, Invariant refuses to
guess and asks you to choose one explicitly.

```toml
version = 1

[project]
source_roots = ["src"]
exclude_paths = ["tests/", "**/migrations/*.py"]
fail_on = "error"

[[rules]]
key = "domain-isolation"
name = "Domain must not depend on infrastructure"
rule_type = "forbidden_import"
source_pattern = "app.domain.*"
forbidden_imports = ["app.infrastructure", "fastapi"]
exclude_paths = ["src/app/domain/legacy/**"]

[[rules]]
key = "no-shell"
name = "Domain code must not run commands"
rule_type = "forbidden_call"
source_pattern = "app.domain.*"
forbidden_calls = ["os.system", "subprocess", "builtins.eval"]

[[rules]]
key = "absolute-imports"
rule_type = "no_relative_import"
severity = "warning"

[[rules]]
key = "small-files"
rule_type = "max_file_lines"
max_lines = 400
```

`version` is required and must be `1`. Unknown fields are errors, so a
misspelt field is never silently ignored. Every problem is reported with its
location:

```text
invariant.toml: rules[0].max_lines: Input should be greater than or equal to 1
invariant.toml: rules[0].severty: Extra inputs are not permitted
invariant.toml: line 4, column 1: Unclosed array
```

## Project settings

| Field | Default | Meaning |
| --- | --- | --- |
| `source_roots` | `["."]` | Directories whose contents map to import names (see below) |
| `exclude_paths` | `[]` | Path globs of files that no rule checks |
| `fail_on` | `"error"` | Lowest severity that fails the check: `"error"` or `"warning"` |

## Rules

Every rule has these fields:

| Field | Default | Meaning |
| --- | --- | --- |
| `key` | required | Stable identifier, unique in the project: 1–64 of `a-z`, `0-9`, `.`, `_`, `-` |
| `name` | the key | Display name, at most 120 characters |
| `rule_type` | required | One of the types below |
| `enabled` | `true` | A disabled rule is not evaluated at all |
| `severity` | `"error"` | `"error"` or `"warning"`; see `fail_on` |
| `source_pattern` | `"*"` | Module glob selecting the modules the rule applies to |
| `exclude_paths` | `[]` | Path globs of files this rule skips |

| `rule_type` | Extra fields | Reports |
| --- | --- | --- |
| `forbidden_import` | `forbidden_imports`: 1–100 module names | An import of a listed module or any of its submodules |
| `forbidden_call` | `forbidden_calls`: 1–100 dotted names | A call of a listed function, or of anything inside a listed module |
| `no_relative_import` | none | Any `from .x import y`, `from ..x import y` or `from . import x` |
| `max_file_lines` | `max_lines`: a positive integer | A file with more physical lines than the limit |

`forbidden_import` checks `import a.b`, `import a.b as c`, `from a import b`
(as `a` and as the possible submodule `a.b`) and relative imports resolved
against the file's package. `app.infrastructure` matches itself and
`app.infrastructure.db`, but not `app.infrastructure_extra`. A `from` import
whose module is itself forbidden is reported once per statement.

`forbidden_call` reports a call when the name it is made through can be traced
to an import or a builtin. `import os as operating; operating.system()` and
`from os import system as sh; sh()` both call `os.system`, `eval()` calls
`builtins.eval`, and a decorator used without parentheses (`@functools.cache`)
calls `functools.cache`. As with imports, an entry covers everything inside
it: `subprocess` forbids `subprocess.run` and `subprocess.Popen`, but not
`subprocessx.run`. Names are looked up with Python's scoping rules, so a
parameter, assignment, loop or `with` target, `except ... as` name, class
attribute, type parameter, `global` or `nonlocal` rebinding that shadows an
import leaves the call unresolved, and an unresolved call is never reported.
So does a name bound to different modules on different paths
(`try: import ujson as json` / `except ImportError: import json`) and any name
a `from x import *` could have provided.

`max_file_lines` counts lines as an editor does: an empty file has 0 lines, a
final line break adds none, and `\n`, `\r\n` and `\r` all end a line. The
violation points at the first line past the limit.

## Module names and source roots

A file's module name is its path relative to its source root, without `.py`,
with `/` replaced by `.`; a package's `__init__.py` is the package itself. With
the default root `.`, `src/app/domain/a.py` is `src.app.domain.a`; with
`source_roots = ["src"]` it is `app.domain.a`, and its relative imports resolve
inside `app`.

Roots must not overlap (`["src", "src/app"]` or `[".", "src"]` is an error), so
every file belongs to at most one root. Python files outside all roots are not
checked and are counted as excluded.

Paths are repository-relative. Backslashes count as separators, `./` and
duplicate slashes are removed, and absolute paths, drive letters, `..` and
control characters are rejected.

## Two kinds of pattern

- **Module globs** (`source_pattern`) match dotted module names with
  `fnmatch`: `*` also matches dots, so `app.domain.*` covers every module below
  `app.domain`, and a pattern ending in `.*` also covers the package itself.
- **Path globs** (`exclude_paths`) match repository-relative paths: `*` and `?`
  stay inside one path segment, `**` spans directories, and a trailing `/`
  means the whole directory (`tests/` is `tests/**`). `**/migrations/*.py`
  matches `migrations/x.py` and `a/b/migrations/x.py`.

## Results

An analysis has exactly one outcome, decided in this order:

| Outcome | When |
| --- | --- |
| `fail` | At least one violation at or above `fail_on` |
| `incomplete` | Some eligible file could not be analyzed (syntax error, too complex, unreadable, a truncated file list) |
| `not_applicable` | Nothing was checked: no eligible files, no enabled rules, or no rule pattern covering an analyzed file |
| `pass` | Every applicable check ran without a blocking violation |

`passed` is `true` only for `pass`, `false` only for `fail`, and `null`
otherwise. Warnings never fail the default `fail_on = "error"` threshold but are
always reported.

Diagnostics are sorted by path, line, column and rule key.

Each diagnostic is either a `violation` of a rule or an `error` that kept a
file from being analyzed. Both carry `code` (the rule type, or a reason such as
`syntax_error`), `severity`, `message`, `path`, `line` and `column`; violations
also carry the rule's `key` and `name` as they were at analysis time, and
`data` with facts for the rule type (`imported_module`, `lines`, ...). A
broken file never disappears from the result: it is listed with its reason
while the other files are still checked.

Lines and columns are 1-based. Columns count Unicode characters, so text such
as `"é"` earlier on the line does not shift them.

File counts add up: `total = eligible + excluded + ignored` and
`eligible = analyzed + skipped`, where `ignored` are removed or non-Python files
and `skipped` are eligible files that could not be analyzed.

## What static analysis cannot see

Invariant parses files with Python's `ast` module and never executes or imports
the code it checks. That also bounds what it can know:

- Dynamic imports (`importlib.import_module(name)`, `__import__`) and code in
  `exec` or `eval` strings are not seen.
- `from x import *` is checked as an import of `x`, but the names it brings in
  are unknown.
- Every import statement counts, including ones inside functions, `try`
  blocks or `if TYPE_CHECKING:`.
- There is no type inference: a module object passed around under another name
  is not followed.
- A relative import that climbs above the top-level package cannot be
  resolved and is not reported by `forbidden_import`.
- `forbidden_call` follows only the name a call is made through: a function
  stored in a variable first (`run = os.system`), passed as a value
  (`map(os.system, commands)`), looked up with `getattr`, or reached through
  an object (`self.shell.run()`) is not traced back to its definition.

An unresolved case is never reported as proven safe: such imports and calls
are simply outside what a rule can match.
