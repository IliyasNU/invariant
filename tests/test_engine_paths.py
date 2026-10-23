import pytest

from invariant.paths import (
    ModuleLocation,
    PathError,
    SourceRoots,
    matches_any,
    normalize_path,
    validate_path_glob,
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("app/x.py", "app/x.py"),
        ("./app//x.py", "app/x.py"),
        ("app/./domain/x.py", "app/domain/x.py"),
        ("app\\domain\\x.py", "app/domain/x.py"),
    ],
)
def test_paths_normalize_to_one_spelling(path, expected):
    assert normalize_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "/etc/passwd.py",
        "C:/x.py",
        "C:\\x.py",
        "c:x.py",
        "\\\\server\\share\\x.py",
        "a/../b.py",
        "..\\b.py",
        "a\u0000.py",
        "a\nb.py",
    ],
)
def test_paths_outside_the_repository_or_with_control_characters_fail(path):
    with pytest.raises(PathError):
        normalize_path(path)


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("tests/", "tests/**"),
        ("./tests/**", "tests/**"),
        ("tests\\**", "tests/**"),
        ("tests/**/", "tests/**"),
        ("**/migrations/*.py", "**/migrations/*.py"),
    ],
)
def test_path_globs_normalize(pattern, expected):
    assert validate_path_glob(pattern) == expected


@pytest.mark.parametrize("pattern", ["/abs/**", "../x/**", "./", "a\u0000/**"])
def test_invalid_path_globs_fail(pattern):
    with pytest.raises(PathError):
        validate_path_glob(pattern)


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("tests/**", "tests/a.py", True),
        ("tests/**", "tests/a/b.py", True),
        ("tests/**", "testsx/a.py", False),
        ("*.py", "x.py", True),
        ("*.py", "pkg/x.py", False),
        ("**/migrations/*.py", "migrations/x.py", True),
        ("**/migrations/*.py", "a/b/migrations/x.py", True),
        ("**/migrations/*.py", "a/migrations/sub/x.py", False),
        ("**/*_test.py", "pkg/a_test.py", True),
        (".github/**", ".github/scripts/x.py", True),
        ("app/[ab].py", "app/c.py", False),
    ],
)
def test_path_globs_keep_single_star_inside_a_segment(pattern, path, expected):
    assert matches_any(path, [pattern]) is expected


def test_default_root_is_the_repository():
    roots = SourceRoots()

    assert roots.locate("src/app/domain/a.py") == ModuleLocation(
        module="src.app.domain.a", package=("src", "app", "domain")
    )


def test_source_roots_map_src_layout_to_import_names():
    roots = SourceRoots(["src", "lib\\"])

    assert roots.locate("src/app/domain/a.py") == ModuleLocation(
        module="app.domain.a", package=("app", "domain")
    )
    assert roots.locate("src/app/domain/__init__.py") == ModuleLocation(
        module="app.domain", package=("app", "domain")
    )
    assert roots.locate("lib/tools/x.py") == ModuleLocation(
        module="tools.x", package=("tools",)
    )
    assert roots.locate("scripts/run.py") is None
    assert roots.locate("srcx/app.py") is None


@pytest.mark.parametrize(
    "roots", [["src", "src/app"], [".", "src"], ["src", "src"], ["src", "./src/"]]
)
def test_overlapping_source_roots_are_rejected(roots):
    with pytest.raises(PathError):
        SourceRoots(roots)
