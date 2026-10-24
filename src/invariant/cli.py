"""The ``invariant`` command: check a project offline, or start its configuration.

Exit codes: 0 when no violation reaches the threshold (or nothing was
checked, which the output says), 1 for violations that do, and 2 for an
unusable configuration or input, a technical error, or an incomplete check.
"""

import argparse
import json
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from invariant import __version__
from invariant.config import (
    CONFIG_FILE,
    PYPROJECT_FILE,
    ConfigError,
    InvariantConfig,
    ProjectSettings,
    choose_config,
    discover_config,
    load_config,
)
from invariant.engine import analyze
from invariant.files import collect_directory
from invariant.git import (
    GitError,
    changed_files,
    committed_config,
    ensure_repository,
    resolve_commit,
)
from invariant.paths import PathError, validate_path_glob
from invariant.result import AnalysisResult, Diagnostic, Outcome

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_ERROR = 2

DOCS = "https://github.com/IliyasNU/invariant/blob/main/docs/configuration.md"

STARTER = """\
# Invariant: architecture rules for this project. All fields are explained in
# {docs}
version = 1

[project]
# Directories whose contents map to import names, e.g. ["src"] for a src layout.
source_roots = ["{root}"]
# Path globs of files that no rule checks, e.g. ["tests/", "**/migrations/*.py"].
exclude_paths = []
# The lowest severity that fails the check: "error" or "warning".
fail_on = "error"

# An example rule: change the module names to match your project.
[[rules]]
key = "domain-isolation"
name = "Domain code must not import infrastructure"
rule_type = "forbidden_import"
source_pattern = "{package}.domain.*"
forbidden_imports = ["{package}.infrastructure"]
"""


class UsageError(Exception):
    """A command line or input that cannot be used."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="invariant",
        description="Check Python code against architecture rules without running it.",
        epilog=(
            "Exit codes: 0 no violation reaches the threshold (or nothing was "
            "checked), 1 violations, 2 an error or an incomplete check."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"invariant {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    init = commands.add_parser(
        "init",
        help=f"write a starter {CONFIG_FILE}",
        description=f"Write a starter {CONFIG_FILE} with an example rule.",
    )
    init.add_argument(
        "path",
        nargs="?",
        default=Path("."),
        type=Path,
        metavar="PATH",
        help="project directory (default: the current directory)",
    )
    init.add_argument(
        "--force", action="store_true", help=f"overwrite an existing {CONFIG_FILE}"
    )

    check = commands.add_parser(
        "check",
        help="check a project",
        description=(
            "Check the Python files under PATH, or with --base only the files a "
            "Git commit range changed."
        ),
    )
    check.add_argument(
        "path",
        nargs="?",
        default=Path("."),
        type=Path,
        metavar="PATH",
        help=(
            "project root; paths in the configuration are relative to it "
            "(default: the current directory)"
        ),
    )
    check.add_argument(
        "--config",
        type=Path,
        metavar="FILE",
        help=(
            f"configuration file (default: {CONFIG_FILE}, or [tool.invariant] in "
            f"{PYPROJECT_FILE}, in PATH)"
        ),
    )
    check.add_argument(
        "--base",
        metavar="REV",
        help=(
            "check only the files changed since the merge base with REV, as a "
            "pull request shows them"
        ),
    )
    check.add_argument(
        "--head",
        metavar="REV",
        help=(
            "with --base: the commit whose files and configuration are checked "
            "(default: HEAD); the working tree is not read"
        ),
    )
    check.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )
    check.add_argument(
        "--fail-on",
        choices=("error", "warning"),
        help="lowest severity that fails the check (default: from the configuration)",
    )
    check.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="also skip files matching this path glob; may be repeated",
    )
    check.add_argument(
        "--strict",
        action="store_true",
        help="exit with 2 when nothing was checked (not_applicable)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # A file name the terminal cannot encode must not crash the report.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            return _init(args.path, force=args.force)
        return _check(args)
    except (UsageError, ConfigError, GitError) as exc:
        for line in str(exc).splitlines() or [type(exc).__name__]:
            print(f"invariant: {_printable(line)}", file=sys.stderr)
        return EXIT_ERROR
    # A crash is a technical error (2), never "violations found" (1), which an
    # uncaught exception would report.
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        print("invariant: internal error", file=sys.stderr)
        return EXIT_ERROR


# init ---------------------------------------------------------------------


def _starter_values(root: Path) -> tuple[str, str]:
    source_root = "src" if (root / "src").is_dir() else "."
    try:
        children = sorted((root / source_root).iterdir())
    except OSError:
        children = []
    packages = [
        child.name
        for child in children
        if child.name.isascii()
        and child.name.isidentifier()
        and child.name not in {"test", "tests"}
        and (child / "__init__.py").is_file()
    ]
    return source_root, packages[0] if packages else "app"


def _init(root: Path, *, force: bool) -> int:
    if not root.is_dir():
        raise UsageError(f"{root} is not a directory")
    target = root / CONFIG_FILE
    try:
        pyproject = (root / PYPROJECT_FILE).read_bytes()
    except OSError:
        pyproject = None
    if choose_config(False, pyproject, where=str(root)) == PYPROJECT_FILE:
        raise UsageError(
            f"{root / PYPROJECT_FILE} already has a [tool.invariant] table; "
            "edit it instead"
        )
    source_root, package = _starter_values(root)
    text = STARTER.format(docs=DOCS, root=source_root, package=package)
    try:
        with open(target, "w" if force else "x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        raise UsageError(
            f"{target} already exists; pass --force to overwrite it"
        ) from None
    except OSError as exc:
        raise UsageError(f"cannot write {target}: {exc.strerror}") from exc
    print(f"Wrote {target} with an example rule for the package '{package}'.")
    print(f"Edit the rule to match your code, then run: invariant check {root}")
    return EXIT_OK


# check --------------------------------------------------------------------


def _check(args: argparse.Namespace) -> int:
    root: Path = args.path
    if not root.is_dir():
        raise UsageError(f"{root} is not a directory")
    if args.head is not None and args.base is None:
        raise UsageError("--head needs --base")
    base = head = None
    if args.base is not None:
        ensure_repository(root)
        base = resolve_commit(root, args.base)
        head = resolve_commit(root, args.head or "HEAD")
    config, config_source = _configuration(args.config, root, head)
    settings = _with_exclusions(config.project, args.exclude)
    if base is not None and head is not None:
        files = changed_files(root, base, head)
    else:
        files = collect_directory(root)
    result = analyze(
        files.sources,
        config.rules,
        settings,
        unreadable=files.unreadable,
        ignored=files.ignored,
        fail_on=args.fail_on,
    )
    source = {
        "mode": "directory" if base is None else "git_diff",
        "config": config_source,
        "base": base,
        "head": head,
    }
    if args.format == "json":
        _write_json(result, source)
    else:
        _write_text(result, source, sys.stdout, strict=args.strict)
    return exit_code(result.outcome, strict=args.strict)


def exit_code(outcome: Outcome, *, strict: bool = False) -> int:
    if outcome is Outcome.FAIL:
        return EXIT_VIOLATIONS
    if outcome is Outcome.INCOMPLETE:
        return EXIT_ERROR
    if outcome is Outcome.NOT_APPLICABLE and strict:
        return EXIT_ERROR
    return EXIT_OK


def _configuration(
    path: Path | None, root: Path, head: str | None
) -> tuple[InvariantConfig, str]:
    if path is not None:
        return load_config(path), str(path)
    if head is not None:
        found = committed_config(root, head)
        where = f"{root} at {head[:12]}"
    else:
        discovered = discover_config(root)
        found = (load_config(discovered), str(discovered)) if discovered else None
        where = str(root)
    if found is None:
        raise UsageError(
            f"no {CONFIG_FILE} or [tool.invariant] in {PYPROJECT_FILE} in {where}; "
            "create one with 'invariant init' or pass --config"
        )
    return found


def _with_exclusions(project: ProjectSettings, patterns: list[str]) -> ProjectSettings:
    if not patterns:
        return project
    extra = []
    for pattern in patterns:
        try:
            extra.append(validate_path_glob(pattern))
        except PathError as exc:
            raise UsageError(f"--exclude {pattern!r}: {exc}") from exc
    return project.model_copy(
        update={"exclude_paths": [*project.exclude_paths, *extra]}
    )


# Output -------------------------------------------------------------------


def _printable(text: str) -> str:
    """Escape control and formatting characters a file name could smuggle in."""
    if text.isprintable():
        return text
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in text)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _describe(item: Diagnostic) -> str:
    where = ":".join(
        str(part) for part in (item.path, item.line, item.column) if part is not None
    )
    if item.kind == "violation":
        label = f"{item.severity} [{item.rule_key}]"
    else:
        label = f"not analyzed [{item.code}]"
    return _printable(f"{where or 'invariant'}: {label} {item.message}")


def _verdict(result: AnalysisResult, *, strict: bool) -> str:
    violations = result.violations
    warnings = sum(item.severity == "warning" for item in violations)
    blocking = sum(
        item.severity == "error" or result.fail_on == "warning" for item in violations
    )
    if result.outcome is Outcome.FAIL:
        line = f"FAIL: {_plural(blocking, 'violation')} at or above {result.fail_on}"
        if len(violations) > blocking:
            line += f", {_plural(len(violations) - blocking, 'warning')}"
        return line
    if result.outcome is Outcome.INCOMPLETE:
        not_analyzed = _plural(result.files.skipped, "file")
        line = (
            f"INCOMPLETE: {not_analyzed} could not be analyzed, so there is no verdict"
        )
        if violations:
            line += f"; {_plural(len(violations), 'violation')} found anyway"
        return line
    if result.outcome is Outcome.NOT_APPLICABLE:
        if result.files.eligible == 0:
            reason = "there is no Python file to check"
        elif result.rules_enabled == 0:
            reason = "no rule is enabled"
        else:
            reason = "no enabled rule applies to the checked files"
        suffix = " (--strict: an error)" if strict else ""
        return f"NOT APPLICABLE: {reason}{suffix}"
    if warnings:
        return f"PASS with {_plural(warnings, 'warning')}"
    return "PASS"


def _write_text(
    result: AnalysisResult, source: dict[str, Any], out: TextIO, *, strict: bool
) -> None:
    out.writelines(_describe(item) + "\n" for item in result.diagnostics)
    if result.diagnostics:
        out.write("\n")
    files = result.files
    out.write(_verdict(result, strict=strict) + "\n")
    out.write(
        f"Files: {files.analyzed} analyzed, {files.skipped} not analyzed, "
        f"{files.excluded} excluded, {files.ignored} ignored "
        f"({files.total} in total)\n"
    )
    out.write(
        f"Rules: {result.rules_enabled} of {result.rules_total} enabled, "
        f"applied {_plural(result.evaluations, 'time')}\n"
    )
    out.write(_printable(f"Configuration: {source['config']}") + "\n")
    if source["base"] is not None:
        out.write(
            f"Changes: {source['head'][:12]} since its merge base with "
            f"{source['base'][:12]}\n"
        )


def _write_json(result: AnalysisResult, source: dict[str, Any]) -> None:
    document = {**result.to_dict(), "source": source}
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    sys.stdout.flush()
    # JSON is UTF-8 whatever the terminal's encoding is.
    sys.stdout.buffer.write(text.encode("utf-8", "backslashreplace"))
    sys.stdout.buffer.flush()
