import random
import subprocess
import sys

import pytest

from invariant.config import (
    ForbiddenImportRule,
    MaxFileLinesRule,
    NoRelativeImportRule,
    ProjectSettings,
)
from invariant.engine import Source, Unreadable, analyze
from invariant.result import Diagnostic, Outcome
from invariant.rules import physical_lines


def forbid(*modules, **fields) -> ForbiddenImportRule:
    fields = {"key": "infra", "source_pattern": "app.domain.*", **fields}
    return ForbiddenImportRule(
        rule_type="forbidden_import", forbidden_imports=list(modules), **fields
    )


def no_relative(**fields) -> NoRelativeImportRule:
    return NoRelativeImportRule(
        rule_type="no_relative_import", key="absolute", **fields
    )


def max_lines(limit: int, **fields) -> MaxFileLinesRule:
    return MaxFileLinesRule(
        rule_type="max_file_lines", key="size", max_lines=limit, **fields
    )


def domain(content: str, path: str = "app/domain/service.py") -> list[Source]:
    return [Source(path=path, content=content)]


def imported(result) -> list[str]:
    return [item.data["imported_module"] for item in result.violations]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("import app.infrastructure\n", ["app.infrastructure"]),
        ("import app.infrastructure.db\n", ["app.infrastructure.db"]),
        ("import app.infrastructure as infra\n", ["app.infrastructure"]),
        ("from app import infrastructure as infra\n", ["app.infrastructure"]),
        ("from app.infrastructure import db as database\n", ["app.infrastructure"]),
        ("from ..infrastructure import db\n", ["app.infrastructure"]),
        ("import app.infrastructure_extra\n", []),
        ("from app import infrastructure_extra\n", []),
        ("import json\n", []),
    ],
)
def test_forbidden_import_matches_the_module_and_its_submodules(content, expected):
    result = analyze(domain(content), [forbid("app.infrastructure")])

    assert imported(result) == expected


def test_forbidden_import_checks_every_alias_of_one_statement():
    result = analyze(
        domain("import json, app.infrastructure, app.infrastructure.db as db\n"),
        [forbid("app.infrastructure")],
    )

    assert imported(result) == ["app.infrastructure", "app.infrastructure.db"]


@pytest.mark.parametrize(
    "content",
    [
        "from .models import Order\n",
        "from ..infrastructure import db\n",
        "from . import x\n",
    ],
)
def test_no_relative_import_reports_every_relative_form(content):
    result = analyze(domain(content), [no_relative()])

    [violation] = result.violations
    assert violation.code == "no_relative_import"
    assert violation.data["relative_module"] == content.split()[1]
    assert result.outcome is Outcome.FAIL


def test_no_relative_import_accepts_absolute_imports_and_respects_exclusions():
    content = "from .models import Order\n"
    assert (
        analyze(domain("from app.domain import models\n"), [no_relative()]).violations
        == ()
    )
    excluded = no_relative(exclude_paths=["app/domain/**"])
    assert analyze(domain(content), [excluded]).violations == ()


@pytest.mark.parametrize(
    ("severity", "outcome"), [("warning", Outcome.PASS), ("error", Outcome.FAIL)]
)
def test_no_relative_import_severity_decides_the_outcome(severity, outcome):
    result = analyze(domain("from . import x\n"), [no_relative(severity=severity)])

    [violation] = result.violations
    assert violation.severity == severity
    assert result.outcome is outcome


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        ("", 0),
        ("\n", 1),
        ("x = 1", 1),
        ("x = 1\n", 1),
        ("x = 1\ny = 2", 2),
        ("x = 1\r\ny = 2\r\n", 2),
        ("x = 1\ry = 2\r", 2),
        ("x = 1\n\n", 2),
        ("x = '\x0c'\n", 1),
    ],
)
def test_physical_lines_count_like_an_editor(source, lines):
    assert physical_lines(source) == lines


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_max_file_lines_allows_n_lines_and_reports_n_plus_one(newline):
    exactly = newline.join(f"x{i} = {i}" for i in range(3)) + newline
    one_more = exactly + f"x3 = 3{newline}"
    rule = max_lines(3, source_pattern="*")

    assert analyze(domain(exactly), [rule]).outcome is Outcome.PASS
    [violation] = analyze(domain(one_more), [rule]).violations
    assert (violation.line, violation.column) == (4, None)
    assert violation.data == {"lines": 4, "max_lines": 3}
    assert "imported_module" not in violation.data


def test_empty_file_has_zero_lines():
    result = analyze(domain(""), [max_lines(1, source_pattern="*")])

    assert result.outcome is Outcome.PASS


def test_source_is_analyzed_verbatim():
    result = analyze(
        domain("\n\n\n  # note\nimport app.infrastructure\n"),
        [forbid("app.infrastructure")],
    )

    assert [item.line for item in result.violations] == [5]


def test_columns_count_characters_not_utf8_bytes():
    result = analyze(
        domain('x = "é"; import app.infrastructure\n'), [forbid("app.infrastructure")]
    )

    [violation] = result.violations
    assert violation.column == 10


def test_diagnostics_carry_rule_snapshot_and_type_data():
    rule = forbid("app.infrastructure", name="Domain isolation", severity="warning")

    [violation] = analyze(domain("import app.infrastructure\n"), [rule]).violations

    assert violation == Diagnostic(
        kind="violation",
        code="forbidden_import",
        severity="warning",
        message=(
            "app.domain.service must not import app.infrastructure "
            "(matched app.infrastructure)"
        ),
        path="app/domain/service.py",
        line=1,
        column=1,
        rule_key="infra",
        rule_name="Domain isolation",
        data={
            "imported_module": "app.infrastructure",
            "forbidden": "app.infrastructure",
        },
    )


def test_disabled_rules_are_not_evaluated():
    result = analyze(
        domain("import app.infrastructure\n"),
        [forbid("app.infrastructure", enabled=False)],
    )

    assert result.violations == ()
    assert (result.rules_total, result.rules_enabled, result.evaluations) == (1, 0, 0)
    assert result.outcome is Outcome.NOT_APPLICABLE


def test_warnings_pass_by_default_and_fail_when_the_threshold_is_warning():
    rule = forbid("app.infrastructure", severity="warning")
    sources = domain("import app.infrastructure\n")

    default = analyze(sources, [rule])
    strict = analyze(sources, [rule], fail_on="warning")
    configured = analyze(sources, [rule], ProjectSettings(fail_on="warning"))

    assert (default.outcome, default.passed, len(default.violations)) == (
        Outcome.PASS,
        True,
        1,
    )
    assert strict.outcome is configured.outcome is Outcome.FAIL
    assert strict.passed is False


def test_src_layout_resolves_modules_under_the_source_root():
    sources = domain("from ..infrastructure import db\n", path="src/app/domain/c.py")
    rule = forbid("app.infrastructure", source_pattern="*")

    flat = analyze(sources, [rule])
    src = analyze(sources, [rule], ProjectSettings(source_roots=["src"]))

    assert flat.outcome is Outcome.PASS  # resolves to src.app.infrastructure
    assert imported(src) == ["app.infrastructure"]


def test_windows_separators_and_package_init_files_map_to_modules():
    settings = ProjectSettings(source_roots=["src"])
    result = analyze(
        [
            Source(
                path="src\\app\\domain\\__init__.py",
                content="import app.infrastructure\n",
            )
        ],
        [forbid("app.infrastructure")],
        settings,
    )

    [violation] = result.violations
    assert violation.path == "src/app/domain/__init__.py"
    assert "app.domain must not import" in violation.message


def test_project_and_rule_exclusions_and_roots_decide_what_is_checked():
    sources = [
        Source("src/app/domain/a.py", "import app.infrastructure\n"),
        Source("src/app/domain/legacy/b.py", "import app.infrastructure\n"),
        Source("src/app/domain/test_c.py", "import app.infrastructure\n"),
        Source("scripts/d.py", "import app.infrastructure\n"),
    ]
    settings = ProjectSettings(source_roots=["src"], exclude_paths=["**/test_*.py"])
    rule = forbid("app.infrastructure", exclude_paths=["src/app/domain/legacy/"])

    result = analyze(sources, [rule], settings, ignored=2)

    assert [item.path for item in result.violations] == ["src/app/domain/a.py"]
    files = result.files
    assert (files.total, files.eligible, files.analyzed) == (6, 2, 2)
    assert (files.excluded, files.skipped, files.ignored) == (2, 0, 2)
    assert result.evaluations == 1


def test_a_broken_file_does_not_hide_the_others():
    sources = [
        Source("app/domain/broken.py", "def broken(:\n"),
        Source("app/domain/deep.py", "x = " + "-" * 200_000 + "1"),
        Source("app/domain/ok.py", "import json\n"),
    ]

    result = analyze(sources, [forbid("app.infrastructure")])

    assert result.outcome is Outcome.INCOMPLETE
    assert result.passed is None
    assert [(item.code, item.path, item.line) for item in result.errors] == [
        ("syntax_error", "app/domain/broken.py", 1),
        ("too_complex", "app/domain/deep.py", None),
    ]
    assert (result.files.analyzed, result.files.skipped) == (1, 2)


def test_a_blocking_violation_fails_even_when_another_file_is_broken():
    sources = [
        Source("app/domain/broken.py", "def broken(:\n"),
        Source("app/domain/bad.py", "import app.infrastructure\n"),
    ]

    result = analyze(sources, [forbid("app.infrastructure")])

    assert result.outcome is Outcome.FAIL
    assert len(result.errors) == 1


def test_unreadable_files_make_the_result_incomplete_unless_excluded():
    settings = ProjectSettings(exclude_paths=["vendor/**"])
    unreadable = [
        Unreadable("app/domain/big.py", "too_large"),
        Unreadable("vendor/blob.py", "undecodable"),
        Unreadable("app\u0000.py", "invalid_path"),
    ]

    result = analyze(
        domain("import json\n"),
        [forbid("app.infrastructure")],
        settings,
        unreadable=unreadable,
    )

    assert result.outcome is Outcome.INCOMPLETE
    assert [(item.code, item.path) for item in result.errors] == [
        ("invalid_path", "app\u0000.py"),
        ("too_large", "app/domain/big.py"),
    ]
    assert (result.files.eligible, result.files.skipped, result.files.excluded) == (
        3,
        2,
        1,
    )


def test_caller_errors_such_as_a_truncated_list_make_the_result_incomplete():
    truncated = Diagnostic(
        kind="error",
        code="file_list_truncated",
        severity="error",
        message="GitHub listed 3000 of 3500 changed files",
    )

    result = analyze(
        domain("import json\n"), [forbid("app.infrastructure")], errors=[truncated]
    )

    assert result.outcome is Outcome.INCOMPLETE
    assert result.errors == (truncated,)


@pytest.mark.parametrize(
    ("sources", "rules"),
    [
        (domain("import json\n"), []),
        ([], [forbid("app.infrastructure")]),
        (domain("import json\n", path="tools/x.py"), [forbid("app.infrastructure")]),
    ],
)
def test_nothing_checked_is_not_applicable_rather_than_a_pass(sources, rules):
    result = analyze(sources, rules)

    assert result.outcome is Outcome.NOT_APPLICABLE
    assert result.passed is None
    assert result.evaluations == 0


def test_analysis_is_deterministic_for_any_input_order():
    sources = [
        Source(
            f"app/domain/m{i}.py", f"import app.infrastructure\nfrom .x import y{i}\n"
        )
        for i in range(20)
    ]
    rules = [forbid("app.infrastructure"), no_relative(severity="warning")]
    shuffled = sources[:]
    random.Random(7).shuffle(shuffled)

    assert analyze(sources, rules).to_dict() == analyze(shuffled, rules).to_dict()


def test_duplicate_paths_are_a_caller_error():
    with pytest.raises(ValueError, match="more than once"):
        analyze([Source("a.py", ""), Source("./a.py", "")], [])


def test_analyzed_code_is_never_executed(tmp_path):
    created = tmp_path / "created"
    ran = tmp_path / "ran"
    content = (
        f"open({str(created)!r}, 'w').write('x')\n"
        "import os, subprocess\n"
        f"os.system('touch {ran}')\n"
        "import app.infrastructure\n"
    )

    result = analyze(domain(content), [forbid("app.infrastructure")])

    assert imported(result) == ["app.infrastructure"]
    assert not created.exists()
    assert not ran.exists()
    # Control: the same code does have side effects when it is executed.
    runnable = content.replace("import app.infrastructure\n", "")
    subprocess.run([sys.executable, "-c", runnable], check=True)
    assert created.exists()
    assert ran.exists()


def test_result_serializes_with_a_schema_version():
    data = analyze(
        domain("import app.infrastructure\n"), [forbid("app.infrastructure")]
    ).to_dict()

    assert data["schema_version"] == 1
    assert (data["outcome"], data["passed"], data["fail_on"]) == (
        "fail",
        False,
        "error",
    )
    assert data["files"] == {
        "total": 1,
        "eligible": 1,
        "analyzed": 1,
        "excluded": 0,
        "skipped": 0,
        "ignored": 0,
    }
    assert data["rules"] == {"total": 1, "enabled": 1, "evaluations": 1}
    assert data["summary"] == {"errors": 1, "warnings": 0, "analysis_errors": 0}
    assert data["diagnostics"][0]["rule_key"] == "infra"
