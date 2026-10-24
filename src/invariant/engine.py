"""The analysis engine shared by the API, the GitHub integration and the CLI.

It never executes or imports the code it analyzes: files are only parsed with
:mod:`ast`.
"""

import ast
import warnings
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from invariant.config import ProjectSettings, RuleConfig
from invariant.paths import (
    ModuleLocation,
    PathError,
    SourceRoots,
    matches_any,
    normalize_path,
)
from invariant.result import AnalysisResult, Diagnostic, FileCounts, Outcome
from invariant.rules import CHECKERS, ParsedFile, rule_applies

UnreadableReason = Literal[
    "too_large", "undecodable", "invalid_path", "symlink", "unreadable"
]

_UNREADABLE_MESSAGES: dict[str, str] = {
    "too_large": "The file is larger than the analysis size limit",
    "undecodable": "The file is not valid UTF-8",
    "invalid_path": "The path cannot name a file in the repository",
    "symlink": "The file is a symbolic link, which is not followed",
    "unreadable": "The file could not be read",
}


@dataclass(frozen=True)
class Source:
    """A file to analyze. Its content is used exactly as given."""

    path: str
    content: str


@dataclass(frozen=True)
class Unreadable:
    """A file the caller could not load; it makes the result incomplete."""

    path: str
    reason: UnreadableReason
    detail: str | None = None


def _error(
    code: str, message: str, path: str | None, **position: int | None
) -> Diagnostic:
    return Diagnostic(
        kind="error",
        code=code,
        severity="error",
        message=message,
        path=path,
        **position,
    )


def _parse(
    path: str, location: ModuleLocation, content: str
) -> ParsedFile | Diagnostic:
    try:
        with warnings.catch_warnings():
            # Warnings about the analyzed code (e.g. invalid escapes) are not ours.
            warnings.simplefilter("ignore")
            tree = ast.parse(content, filename=path)
    except SyntaxError as exc:
        return _error("syntax_error", exc.msg, path, line=exc.lineno, column=exc.offset)
    except MemoryError, RecursionError:
        # CPython reports deeply nested source this way instead of SyntaxError.
        return _error("too_complex", "Python source is too complex to parse", path)
    return ParsedFile(
        path=path,
        module=location.module,
        package=location.package,
        source=content,
        tree=tree,
    )


def analyze(
    sources: Iterable[Source],
    rules: Sequence[RuleConfig],
    settings: ProjectSettings | None = None,
    *,
    unreadable: Iterable[Unreadable] = (),
    ignored: int = 0,
    errors: Iterable[Diagnostic] = (),
    fail_on: Literal["error", "warning"] | None = None,
) -> AnalysisResult:
    """Check ``sources`` against ``rules``.

    ``unreadable`` lists files the caller could not load, ``ignored`` counts
    removed or non-Python files, and ``errors`` carries problems found before
    analysis (such as a truncated file list). ``fail_on`` overrides the
    project's threshold.
    """
    settings = settings or ProjectSettings()
    threshold = fail_on or settings.fail_on
    roots = SourceRoots(settings.source_roots)
    enabled = [rule for rule in rules if rule.enabled]
    diagnostics = list(errors)
    sources = list(sources)
    unreadable = list(unreadable)
    eligible = analyzed = excluded = skipped = evaluations = 0

    def is_excluded(path: str) -> bool:
        return roots.locate(path) is None or matches_any(path, settings.exclude_paths)

    for item in unreadable:
        try:
            path = normalize_path(item.path)
        except PathError:
            path = None
        if path is not None and is_excluded(path):
            excluded += 1
            continue
        eligible += 1
        skipped += 1
        message = _UNREADABLE_MESSAGES[item.reason]
        if item.detail:
            message = f"{message}: {item.detail}"
        # An invalid name is shown as given: normalized, "a\\b.py" would read
        # as a different file, a/b.py.
        shown = item.path if item.reason == "invalid_path" else path or item.path
        diagnostics.append(_error(item.reason, message, shown))

    seen: set[str] = set()
    for source in sources:
        try:
            path = normalize_path(source.path)
        except PathError as exc:
            eligible += 1
            skipped += 1
            diagnostics.append(
                _error(
                    "invalid_path", f"The path cannot be analyzed: {exc}", source.path
                )
            )
            continue
        if path in seen:
            raise ValueError(f"the path '{path}' is given more than once")
        seen.add(path)
        location = roots.locate(path)
        if location is None or matches_any(path, settings.exclude_paths):
            excluded += 1
            continue
        eligible += 1
        parsed = _parse(path, location, source.content)
        if isinstance(parsed, Diagnostic):
            skipped += 1
            diagnostics.append(parsed)
            continue
        analyzed += 1
        for rule in enabled:
            if rule_applies(rule, location.module, path):
                evaluations += 1
                diagnostics.extend(CHECKERS[rule.rule_type](rule, parsed))

    diagnostics.sort(key=Diagnostic.sort_key)
    blocking = [
        item
        for item in diagnostics
        if item.kind == "violation"
        and (item.severity == "error" or threshold == "warning")
    ]
    if blocking:
        outcome = Outcome.FAIL
    elif any(item.kind == "error" for item in diagnostics):
        outcome = Outcome.INCOMPLETE
    elif evaluations == 0:
        outcome = Outcome.NOT_APPLICABLE
    else:
        outcome = Outcome.PASS

    return AnalysisResult(
        outcome=outcome,
        fail_on=threshold,
        files=FileCounts(
            total=len(sources) + len(unreadable) + ignored,
            eligible=eligible,
            analyzed=analyzed,
            excluded=excluded,
            skipped=skipped,
            ignored=ignored,
        ),
        rules_total=len(rules),
        rules_enabled=len(enabled),
        evaluations=evaluations,
        diagnostics=tuple(diagnostics),
    )
