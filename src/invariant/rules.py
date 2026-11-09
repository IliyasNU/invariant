"""Rule checkers: each turns one parsed file into diagnostics for one rule."""

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from functools import cached_property, partial

from invariant.calls import ResolvedCall, resolve_calls
from invariant.config import (
    ForbiddenCallRule,
    ForbiddenImportRule,
    MaxFileLinesRule,
    NoRelativeImportRule,
    RuleConfig,
)
from invariant.paths import absolute_module, matches_any
from invariant.result import Diagnostic

# The line breaks Python's tokenizer recognises; str.splitlines() knows more.
LINE_BREAK = re.compile(r"\r\n|\r|\n")


@dataclass(frozen=True)
class ParsedFile:
    path: str
    module: str
    package: tuple[str, ...]
    source: str
    tree: ast.Module

    @cached_property
    def lines(self) -> list[str]:
        return LINE_BREAK.split(self.source)

    @cached_property
    def calls(self) -> list[ResolvedCall]:
        # Resolved once for all the forbidden_call rules checking this file.
        return resolve_calls(self.tree, self.package)

    @cached_property
    def imports(self) -> tuple[ast.Import | ast.ImportFrom, ...]:
        # ast.walk's breadth-first order decides which findings are listed when
        # the caller sets a limit. Share that same traversal across rules.
        return tuple(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        )

    @cached_property
    def line_count(self) -> int:
        return physical_lines(self.source)

    def column(self, node: ast.stmt | ast.expr) -> int:
        """The 1-based character column of a node; ``ast`` counts UTF-8 bytes."""
        text = self.lines[node.lineno - 1] if node.lineno <= len(self.lines) else ""
        if text.isascii():
            return node.col_offset + 1
        return (
            len(text.encode("utf-8")[: node.col_offset].decode("utf-8", "ignore")) + 1
        )


def physical_lines(source: str) -> int:
    """Count lines as an editor does: a final line break adds no extra line."""
    if not source:
        return 0
    breaks = len(LINE_BREAK.findall(source))
    return breaks if source.endswith(("\n", "\r")) else breaks + 1


def module_matches(module: str, source_pattern: str) -> bool:
    # "app.domain.*" also covers the package itself (app/domain/__init__.py).
    return fnmatchcase(module, source_pattern) or (
        source_pattern.endswith(".*") and fnmatchcase(module, source_pattern[:-2])
    )


def rule_applies(rule: RuleConfig, module: str, path: str) -> bool:
    return module_matches(module, rule.source_pattern) and not matches_any(
        path, rule.exclude_paths
    )


def _violation(
    rule: RuleConfig,
    file: ParsedFile,
    node: ast.stmt | ast.expr,
    message: str,
    **data: object,
) -> Diagnostic:
    return Diagnostic(
        kind="violation",
        code=rule.rule_type,
        severity=rule.severity,
        message=message,
        path=file.path,
        line=node.lineno,
        column=file.column(node),
        rule_key=rule.key,
        rule_name=rule.display_name,
        data=data,
    )


class PrefixMatcher:
    """Match dotted ancestors, keeping the first configured matching prefix.

    Checking an import depends on its number of dotted components rather than
    how many forbidden prefixes the rule lists. Prepared once per analysis,
    this index cannot retain an earlier analysis's mutable configuration.
    """

    def __init__(self, prefixes: list[str]) -> None:
        self.positions = {prefix: index for index, prefix in enumerate(prefixes)}

    def __call__(self, imported: str) -> str | None:
        candidate = imported
        first = len(self.positions)
        matched = None
        while candidate:
            position = self.positions.get(candidate)
            if position is not None and position < first:
                first, matched = position, candidate
            boundary = candidate.rfind(".")
            if boundary < 0:
                break
            candidate = candidate[:boundary]
        return matched


@dataclass
class CheckResult:
    """Listed diagnostics and the total, including findings past the limit."""

    diagnostics: list[Diagnostic] = field(default_factory=list)
    total: int = 0

    def record(self, limit: int | None) -> bool:
        """Count a violation; return whether to build and list its diagnostic."""
        self.total += 1
        return limit is None or len(self.diagnostics) < limit


def check_forbidden_import(
    rule: ForbiddenImportRule,
    file: ParsedFile,
    limit: int | None = None,
    *,
    matcher: PrefixMatcher,
) -> CheckResult:
    found = CheckResult()

    def report(node: ast.stmt, imported: str, forbidden: str) -> None:
        if not found.record(limit):
            return
        found.diagnostics.append(
            _violation(
                rule,
                file,
                node,
                f"{file.module} must not import {forbidden} (matched {imported})",
                imported_module=imported,
                forbidden=forbidden,
            )
        )

    for node in file.imports:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if forbidden := matcher(alias.name):
                    report(node, alias.name, forbidden)
        elif isinstance(node, ast.ImportFrom):
            module = absolute_module(file.package, node.level, node.module)
            if module is None:
                # Unresolvable, so it is kept relative and matches no absolute rule.
                module = "." * node.level + (node.module or "")
            if forbidden := matcher(module):
                report(node, module, forbidden)
                continue
            # ``from package import name`` may load the submodule package.name.
            for alias in node.names:
                if alias.name == "*":
                    continue
                imported = f"{module}.{alias.name}"
                if forbidden := matcher(imported):
                    report(node, imported, forbidden)
    return found


def check_forbidden_call(
    rule: ForbiddenCallRule,
    file: ParsedFile,
    limit: int | None = None,
    *,
    matcher: PrefixMatcher,
) -> CheckResult:
    found = CheckResult()
    for call in file.calls:
        if forbidden := matcher(call.target):
            if not found.record(limit):
                continue
            found.diagnostics.append(
                _violation(
                    rule,
                    file,
                    call.node,
                    f"{file.module} must not call {forbidden} (called {call.target})",
                    call=call.target,
                    forbidden=forbidden,
                )
            )
    return found


def check_no_relative_import(
    rule: NoRelativeImportRule, file: ParsedFile, limit: int | None = None
) -> CheckResult:
    found = CheckResult()
    for node in file.imports:
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            if not found.record(limit):
                continue
            spelled = "." * node.level + (node.module or "")
            resolved = absolute_module(file.package, node.level, node.module)
            found.diagnostics.append(
                _violation(
                    rule,
                    file,
                    node,
                    f"{file.module} uses the relative import '{spelled}'; "
                    "import it absolutely",
                    relative_module=spelled,
                    resolved_module=resolved,
                )
            )
    return found


def check_max_file_lines(
    rule: MaxFileLinesRule, file: ParsedFile, limit: int | None = None
) -> CheckResult:
    found = CheckResult()
    lines = file.line_count
    if lines > rule.max_lines and found.record(limit):
        found.diagnostics.append(
            Diagnostic(
                kind="violation",
                code=rule.rule_type,
                severity=rule.severity,
                message=f"{file.path} has {lines} lines; the limit is {rule.max_lines}",
                path=file.path,
                # The first line past the limit.
                line=rule.max_lines + 1,
                rule_key=rule.key,
                rule_name=rule.display_name,
                data={"lines": lines, "max_lines": rule.max_lines},
            )
        )
    return found


CHECKERS: dict[str, Callable[..., CheckResult]] = {
    "forbidden_import": check_forbidden_import,
    "forbidden_call": check_forbidden_call,
    "no_relative_import": check_no_relative_import,
    "max_file_lines": check_max_file_lines,
}


def prepare_checker(
    rule: RuleConfig,
) -> Callable[[ParsedFile, int | None], CheckResult]:
    """Prepare a rule once, then reuse its checker for every applicable file."""
    checker = CHECKERS[rule.rule_type]
    if isinstance(rule, ForbiddenImportRule):
        return partial(checker, rule, matcher=PrefixMatcher(rule.forbidden_imports))
    if isinstance(rule, ForbiddenCallRule):
        return partial(checker, rule, matcher=PrefixMatcher(rule.forbidden_calls))
    return partial(checker, rule)
