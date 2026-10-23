"""Rule checkers: each turns one parsed file into diagnostics for one rule."""

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import cached_property

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


def _forbidden_match(imported: str, forbidden_imports: list[str]) -> str | None:
    for forbidden in forbidden_imports:
        if imported == forbidden or imported.startswith(f"{forbidden}."):
            return forbidden
    return None


def check_forbidden_import(
    rule: ForbiddenImportRule, file: ParsedFile
) -> list[Diagnostic]:
    found: list[Diagnostic] = []

    def report(node: ast.stmt, imported: str, forbidden: str) -> None:
        found.append(
            _violation(
                rule,
                file,
                node,
                f"{file.module} must not import {forbidden} (matched {imported})",
                imported_module=imported,
                forbidden=forbidden,
            )
        )

    for node in ast.walk(file.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if forbidden := _forbidden_match(alias.name, rule.forbidden_imports):
                    report(node, alias.name, forbidden)
        elif isinstance(node, ast.ImportFrom):
            module = absolute_module(file.package, node.level, node.module)
            if module is None:
                # Unresolvable, so it is kept relative and matches no absolute rule.
                module = "." * node.level + (node.module or "")
            if forbidden := _forbidden_match(module, rule.forbidden_imports):
                report(node, module, forbidden)
                continue
            # ``from package import name`` may load the submodule package.name.
            for alias in node.names:
                if alias.name == "*":
                    continue
                imported = f"{module}.{alias.name}"
                if forbidden := _forbidden_match(imported, rule.forbidden_imports):
                    report(node, imported, forbidden)
    return found


def check_forbidden_call(rule: ForbiddenCallRule, file: ParsedFile) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for call in file.calls:
        if forbidden := _forbidden_match(call.target, rule.forbidden_calls):
            found.append(
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
    rule: NoRelativeImportRule, file: ParsedFile
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for node in ast.walk(file.tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            spelled = "." * node.level + (node.module or "")
            resolved = absolute_module(file.package, node.level, node.module)
            found.append(
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


def check_max_file_lines(rule: MaxFileLinesRule, file: ParsedFile) -> list[Diagnostic]:
    lines = physical_lines(file.source)
    if lines <= rule.max_lines:
        return []
    return [
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
    ]


CHECKERS: dict[str, Callable[..., list[Diagnostic]]] = {
    "forbidden_import": check_forbidden_import,
    "forbidden_call": check_forbidden_call,
    "no_relative_import": check_no_relative_import,
    "max_file_lines": check_max_file_lines,
}
