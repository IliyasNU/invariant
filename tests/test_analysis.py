import pytest

from app.analysis import SourceSyntaxError, analyze_files
from app.models import Rule
from app.schemas import SourceFile

DOMAIN_RULE = Rule(
    id=1,
    name="Domain isolation",
    rule_type="forbidden_import",
    source_pattern="app.domain.*",
    forbidden_imports=["app.infrastructure"],
)


def violations(content: str, path: str = "app/domain/service.py") -> list[str]:
    files = [SourceFile(path=path, content=content)]
    return [item.imported_module for item in analyze_files(files, [DOMAIN_RULE])]


def test_leading_blank_lines_keep_line_numbers():
    files = [
        SourceFile(
            path="app/domain/service.py",
            content="\n\n\nfrom app.infrastructure import db\n",
        )
    ]

    [violation] = analyze_files(files, [DOMAIN_RULE])

    assert files[0].content.startswith("\n\n\n")
    assert violation.line == 4


def test_from_package_import_submodule_is_checked():
    assert violations("from app import infrastructure\n") == ["app.infrastructure"]
    assert violations("from app import infrastructure_helpers, config\n") == []


def test_from_forbidden_module_reports_one_violation_per_statement():
    assert violations("from app.infrastructure import db, cache\n") == [
        "app.infrastructure"
    ]


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("app/domain/service.py", "from ..infrastructure import db\n"),
        ("app/domain/service.py", "from .. import infrastructure\n"),
        ("app/domain/__init__.py", "from ..infrastructure.db import engine\n"),
        ("app/domain/orders/models.py", "from ...infrastructure import db\n"),
    ],
)
def test_relative_imports_are_resolved(path, content):
    assert violations(content, path=path) != []


def test_package_glob_covers_the_package_init():
    assert violations("import app.infrastructure\n", path="app/domain/__init__.py")
    assert violations("import app.infrastructure\n", path="app/domainx.py") == []


def test_relative_imports_within_allowed_packages_pass():
    assert violations("from . import models\nfrom .models import Order\n") == []
    # Climbing above the top-level package cannot be resolved and is ignored.
    assert violations("from ....app.infrastructure import db\n") == []


@pytest.mark.parametrize(
    "path", ["./app/domain/service.py", "app//domain/service.py", "app/./domain/x.py"]
)
def test_equivalent_paths_map_to_the_same_module(path):
    assert violations("import app.infrastructure\n", path=path) == [
        "app.infrastructure"
    ]


def test_too_complex_source_is_a_syntax_error():
    files = [SourceFile(path="app/deep.py", content="x = " + "-" * 200_000 + "1")]

    with pytest.raises(SourceSyntaxError) as exc_info:
        analyze_files(files, [DOMAIN_RULE])

    assert exc_info.value.message == "Python source is too complex to parse"
