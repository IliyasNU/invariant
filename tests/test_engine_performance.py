"""Performance contracts without machine-dependent timing assertions."""

import ast

import pytest

import invariant.rules as checkers
from invariant.config import (
    ForbiddenCallRule,
    ForbiddenImportRule,
    MaxFileLinesRule,
    NoRelativeImportRule,
)
from invariant.engine import Source, analyze
from invariant.result import Outcome


def import_rule(key="imports", **fields):
    return ForbiddenImportRule(
        key=key,
        rule_type="forbidden_import",
        forbidden_imports=fields.pop("forbidden_imports", ["os"]),
        **fields,
    )


def test_import_rules_share_one_breadth_first_ast_walk_per_file(monkeypatch):
    original = ast.walk
    walked = []

    def walk(tree):
        walked.append(tree)
        return original(tree)

    monkeypatch.setattr(checkers.ast, "walk", walk)
    sources = [Source(f"app/a{i}.py", "import os\nfrom . import x\n") for i in range(3)]
    rules = [import_rule(f"imports-{i}") for i in range(10)] + [
        NoRelativeImportRule(key=f"relative-{i}", rule_type="no_relative_import")
        for i in range(10)
    ]

    result = analyze(sources, rules)

    assert result.error_count == 60
    assert result.evaluations == 60
    assert len(walked) == len(sources)


def test_line_count_is_shared_by_size_rules_for_one_file(monkeypatch):
    original = checkers.physical_lines
    counted = []

    def count(source):
        counted.append(source)
        return original(source)

    monkeypatch.setattr(checkers, "physical_lines", count)
    sources = [Source("a.py", "x = 1\n" * 5), Source("b.py", "y = 1\r\n" * 8)]
    rules = [
        MaxFileLinesRule(key=f"size-{i}", rule_type="max_file_lines", max_lines=2)
        for i in range(10)
    ]

    assert analyze(sources, rules).error_count == 20
    assert counted == [source.content for source in sources]


@pytest.mark.parametrize("prefixes", [["os", "os.path"], ["os.path", "os"]])
def test_overlapping_prefixes_keep_configuration_order_for_imports_and_calls(prefixes):
    rules = [
        import_rule(forbidden_imports=prefixes),
        ForbiddenCallRule(
            key="calls", rule_type="forbidden_call", forbidden_calls=prefixes
        ),
    ]
    result = analyze(
        [Source("a.py", "import os.path\nos.path.join('a', 'b')\n")], rules
    )

    assert result.error_count == 2
    assert [item.data["forbidden"] for item in result.violations] == [prefixes[0]] * 2


@pytest.mark.parametrize(
    "imported",
    ["os", "os.path", "os.path.join", "os.paths", "os_extra", ".os.path", "a.é.child"],
)
def test_indexed_prefix_matching_keeps_exact_dotted_boundary_semantics(imported):
    prefixes = ["os.path", "a.é", "os", "a", "z"]
    expected = next(
        (
            prefix
            for prefix in prefixes
            if imported == prefix or imported.startswith(prefix + ".")
        ),
        None,
    )

    assert checkers.PrefixMatcher(prefixes)(imported) == expected


def test_matchers_are_prepared_once_per_analysis_not_per_file_and_never_stale(
    monkeypatch,
):
    original = checkers.PrefixMatcher
    prepared = []

    class Matcher(original):
        def __init__(self, prefixes):
            prepared.append(tuple(prefixes))
            super().__init__(prefixes)

    monkeypatch.setattr(checkers, "PrefixMatcher", Matcher)
    rule = import_rule()
    sources = [Source(f"a{i}.py", "import os\n") for i in range(8)]

    assert analyze(sources, [rule]).error_count == 8
    rule.forbidden_imports[0] = "sys"
    assert analyze(sources, [rule]).outcome is Outcome.PASS
    assert prepared == [("os",), ("sys",)]


@pytest.mark.parametrize("limit", [0, 1, 5, 70])
def test_capped_checks_count_all_rule_types_without_allocating_omitted_diagnostics(
    monkeypatch, limit
):
    sources = [
        Source("app/a.py", "import os\n" + "from .x import y\nos.system('x')\n" * 10),
        Source("app/b.py", "import os\n" + "from .x import y\nos.system('x')\n" * 10),
    ]
    rules = [
        import_rule(severity="warning"),
        ForbiddenCallRule(
            key="calls", rule_type="forbidden_call", forbidden_calls=["os.system"]
        ),
        NoRelativeImportRule(
            key="relative", rule_type="no_relative_import", severity="warning"
        ),
        MaxFileLinesRule(key="size", rule_type="max_file_lines", max_lines=2),
    ]
    full = analyze(sources, rules)
    diagnostic = checkers.Diagnostic
    constructed = []
    original_column = checkers.ParsedFile.column
    columns = []

    def create(**fields):
        constructed.append(fields)
        return diagnostic(**fields)

    def column(file, node):
        columns.append(node)
        return original_column(file, node)

    monkeypatch.setattr(checkers, "Diagnostic", create)
    monkeypatch.setattr(checkers.ParsedFile, "column", column)

    result = analyze(sources, rules, max_violations=limit)

    assert result.outcome is full.outcome is Outcome.FAIL
    assert (result.error_count, result.warning_count) == (
        full.error_count,
        full.warning_count,
    )
    assert result.files == full.files
    assert result.evaluations == full.evaluations
    listed = min(limit, len(full.violations))
    assert len(constructed) == len(result.violations) == listed
    assert len(columns) == sum(
        item.code != "max_file_lines" for item in result.violations
    )


def test_capped_imports_keep_breadth_first_selection_and_character_columns():
    # The top-level import is visited before the earlier, nested one. Selection
    # happens before final line-based sorting, exactly as in uncached ast.walk.
    source = Source(
        "a.py",
        "def nested():\n    import os.path\nx = 'é'; import os\n",
    )
    result = analyze([source], [import_rule()], max_violations=1)

    [item] = result.violations
    assert (item.line, item.column, item.data["imported_module"]) == (3, 10, "os")
    assert result.error_count == 2
    assert result.omitted_errors == 1


def test_from_module_match_still_suppresses_alias_duplicates_and_unresolved_relatives():
    sources = [
        Source("app/a.py", "from os.path import join, exists\n"),
        Source("top.py", "from .os.path import join\n"),
    ]
    result = analyze(
        sources, [import_rule(forbidden_imports=["os.path", "os.path.join"])]
    )

    [item] = result.violations
    assert (item.path, item.data["imported_module"]) == ("app/a.py", "os.path")


def test_a_negative_diagnostic_limit_is_rejected():
    with pytest.raises(ValueError, match="max_violations must be nonnegative"):
        analyze([], [], max_violations=-1)


def test_prefix_mutation_cannot_hide_a_later_match():
    rule = import_rule(forbidden_imports=["os", "sys", "json"])
    rule.forbidden_imports[1] = "os"
    result = analyze([Source("a.py", "import json\n")], [rule])
    [item] = result.violations
    assert item.data["forbidden"] == "json"
    assert result.outcome is Outcome.FAIL
