import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from invariant.cli import main
from invariant.config import parse_config
from invariant.files import MAX_FILE_BYTES

CONFIG = """\
version = 1

[project]
source_roots = ["src"]

[[rules]]
key = "domain-isolation"
name = "Domain isolation"
rule_type = "forbidden_import"
source_pattern = "shop.domain.*"
forbidden_imports = ["shop.infrastructure"]

[[rules]]
key = "absolute"
rule_type = "no_relative_import"
severity = "warning"
"""

VIOLATION = "from shop.infrastructure import db\n"

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file names")


def write(root: Path, files: dict[str, str | bytes]) -> Path:
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    return root


def project(root: Path, files: dict[str, str | bytes], config: str = CONFIG) -> Path:
    return write(root, {"invariant.toml": config, **files})


def run(capsys, *args) -> tuple[int, str, str]:
    code = main([str(arg) for arg in args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def check_json(capsys, *args) -> tuple[int, dict]:
    code, out, err = run(capsys, "check", *args, "--format", "json")
    assert err == ""
    return code, json.loads(out)


# init ---------------------------------------------------------------------


def test_init_writes_a_starter_that_checks_right_away(tmp_path, capsys):
    write(
        tmp_path,
        {
            "src/shop/__init__.py": "",
            "src/shop/domain/__init__.py": "",
            "src/shop/domain/order.py": VIOLATION,
            "src/shop/infrastructure/__init__.py": "",
        },
    )

    code, out, err = run(capsys, "init", tmp_path)

    assert (code, err) == (0, "")
    assert f"invariant check {tmp_path}" in out
    config = parse_config((tmp_path / "invariant.toml").read_text(encoding="utf-8"))
    assert config.project.source_roots == ["src"]
    [rule] = config.rules
    assert (rule.source_pattern, rule.forbidden_imports) == (
        "shop.domain.*",
        ["shop.infrastructure"],
    )
    # The example rule already catches the planted import.
    assert run(capsys, "check", tmp_path)[0] == 1


@pytest.mark.parametrize(
    ("files", "root", "package"),
    [
        ({"billing/__init__.py": ""}, ".", "billing"),
        ({"tests/__init__.py": "", "README.md": ""}, ".", "app"),
        ({"src/x.py": ""}, "src", "app"),
        ({"my-tool/__init__.py": "", "zeta/__init__.py": ""}, ".", "zeta"),
    ],
)
def test_init_adapts_the_example_to_the_layout(tmp_path, capsys, files, root, package):
    write(tmp_path, files)

    assert run(capsys, "init", tmp_path)[0] == 0

    config = parse_config((tmp_path / "invariant.toml").read_text(encoding="utf-8"))
    assert config.project.source_roots == [root]
    assert config.rules[0].source_pattern == f"{package}.domain.*"


def test_init_keeps_an_existing_configuration_unless_forced(tmp_path, capsys):
    target = write(tmp_path, {"invariant.toml": "version = 1\n"}) / "invariant.toml"

    code, out, err = run(capsys, "init", tmp_path)

    assert (code, out) == (2, "")
    assert "--force" in err
    assert target.read_text() == "version = 1\n"
    assert run(capsys, "init", tmp_path, "--force")[0] == 0
    assert "[[rules]]" in target.read_text()


def test_init_does_not_add_a_second_configuration(tmp_path, capsys):
    write(tmp_path, {"pyproject.toml": "[tool.invariant]\nversion = 1\n"})

    code, _, err = run(capsys, "init", tmp_path, "--force")

    assert code == 2
    assert "[tool.invariant]" in err
    assert not (tmp_path / "invariant.toml").exists()


# check: output -------------------------------------------------------------


def test_text_output_lists_diagnostics_then_the_verdict(tmp_path, capsys):
    project(
        tmp_path,
        {
            "src/shop/domain/order.py": VIOLATION,
            "src/shop/api.py": "from .domain import order\n",
        },
    )

    code, out, err = run(capsys, "check", tmp_path)

    assert (code, err) == (1, "")
    assert out.splitlines() == [
        (
            "src/shop/api.py:1:1: warning [absolute] shop.api uses the relative "
            "import '.domain'; import it absolutely"
        ),
        (
            "src/shop/domain/order.py:1:1: error [domain-isolation] "
            "shop.domain.order must not import shop.infrastructure "
            "(matched shop.infrastructure)"
        ),
        "",
        "FAIL: 1 violation at or above error, 1 warning",
        "Files: 2 analyzed, 0 not analyzed, 0 excluded, 1 ignored (3 in total)",
        "Rules: 2 of 2 enabled, applied 3 times",
        f"Configuration: {tmp_path / 'invariant.toml'}",
    ]


def test_json_output_is_the_whole_result_and_nothing_else(tmp_path, capsys):
    project(tmp_path, {"src/shop/domain/order.py": VIOLATION})

    code, document = check_json(capsys, tmp_path)

    assert code == 1
    assert (document["schema_version"], document["outcome"], document["passed"]) == (
        1,
        "fail",
        False,
    )
    assert document["source"] == {
        "mode": "directory",
        "config": str(tmp_path / "invariant.toml"),
        "base": None,
        "head": None,
    }
    [violation] = document["diagnostics"]
    assert (violation["path"], violation["line"], violation["rule_key"]) == (
        "src/shop/domain/order.py",
        1,
        "domain-isolation",
    )
    assert document["files"]["analyzed"] == 1


@pytest.mark.parametrize(
    ("options", "code", "verdict"),
    [
        ((), 0, "PASS with 1 warning"),
        (("--fail-on", "warning"), 1, "FAIL: 1 violation at or above warning"),
    ],
)
def test_the_threshold_decides_whether_warnings_fail(
    tmp_path, capsys, options, code, verdict
):
    project(tmp_path, {"src/shop/api.py": "from . import x\n"})

    result = run(capsys, "check", tmp_path, *options)

    assert result[0] == code
    assert verdict in result[1].splitlines()


@pytest.mark.parametrize(
    ("files", "config", "reason"),
    [
        (
            {"src/shop/api.py": "x = 1\n"},
            CONFIG.replace('severity = "warning"\n', "enabled = false\n").replace(
                '"shop.domain.*"', '"other.*"'
            ),
            "no enabled rule applies to the checked files",
        ),
        ({"README.md": "hello\n"}, CONFIG, "there is no Python file to check"),
        ({"src/shop/api.py": "x = 1\n"}, "version = 1\n", "no rule is enabled"),
    ],
)
def test_nothing_checked_is_visible_and_strict_rejects_it(
    tmp_path, capsys, files, config, reason
):
    project(tmp_path, files, config)

    code, out, _ = run(capsys, "check", tmp_path)
    assert code == 0
    assert f"NOT APPLICABLE: {reason}" in out.splitlines()

    code, out, _ = run(capsys, "check", tmp_path, "--strict")
    assert code == 2
    assert f"NOT APPLICABLE: {reason} (--strict: an error)" in out.splitlines()


# check: reading files ------------------------------------------------------


@posix_only
def test_files_that_cannot_be_analyzed_make_the_check_incomplete(tmp_path, capsys):
    project(
        tmp_path,
        {
            "src/shop/ok.py": "x = 1\n",
            "src/shop/latin1.py": b"x = '\xe9'\n",
            "src/shop/big.py": b"#" * (MAX_FILE_BYTES + 1),
            "src/shop/broken.py": "def (:\n",
        },
    )
    (tmp_path / "src/shop/link.py").symlink_to("ok.py")
    os.mkfifo(tmp_path / "src/shop/pipe.py")

    code, out, _ = run(capsys, "check", tmp_path)

    assert code == 2
    lines = out.splitlines()
    assert "INCOMPLETE: 5 files could not be analyzed, so there is no verdict" in lines
    reasons = {
        line.split(":")[0]: line.split("[")[1].split("]")[0]
        for line in lines
        if "not analyzed [" in line
    }
    assert reasons == {
        "src/shop/big.py": "too_large",
        "src/shop/broken.py": "syntax_error",
        "src/shop/latin1.py": "undecodable",
        "src/shop/link.py": "symlink",
        "src/shop/pipe.py": "unreadable",
    }
    assert (
        "src/shop/pipe.py: not analyzed [unreadable] The file could not be read: "
        "not a regular file"
    ) in lines

    # Excluded files are not read, so they cannot make the check incomplete.
    code, out, _ = run(
        capsys,
        "check",
        tmp_path,
        *("--exclude", "src/shop/big.py", "--exclude", "src/shop/latin1.py"),
    )
    assert "INCOMPLETE: 3 files could not be analyzed" in out


def test_a_utf8_byte_order_mark_is_accepted(tmp_path, capsys):
    project(
        tmp_path, {"src/shop/domain/order.py": b"\xef\xbb\xbf" + VIOLATION.encode()}
    )

    code, document = check_json(capsys, tmp_path)

    assert code == 1
    [violation] = document["diagnostics"]
    assert (violation["line"], violation["column"]) == (1, 1)


def test_tool_and_environment_directories_are_not_searched(tmp_path, capsys):
    config = 'version = 1\n[[rules]]\nkey = "x"\nrule_type = "max_file_lines"\nmax_lines = 1\n'
    files = {
        ".venv/lib/a.py": "x\ny\n",
        ".git/hooks/b.py": "x\ny\n",
        "env/pyvenv.cfg": "home = /usr\n",
        "env/lib/c.py": "x\ny\n",
        "node_modules/gyp/d.py": "x\ny\n",
        "pkg/__pycache__/e.py": "x\ny\n",
        "pkg/environment/f.py": "x\ny\n",
    }
    project(tmp_path, files, config)

    code, document = check_json(capsys, tmp_path)

    assert code == 1
    assert [item["path"] for item in document["diagnostics"]] == [
        "pkg/environment/f.py"
    ]
    assert document["files"]["total"] == 2  # f.py and invariant.toml


@posix_only
def test_linked_directories_are_not_entered(tmp_path, capsys):
    outside = write(tmp_path / "outside", {"secret.py": VIOLATION})
    root = project(tmp_path / "root", {"src/shop/domain/order.py": "x = 1\n"})
    (root / "src/shop/domain/linked").symlink_to(outside, target_is_directory=True)

    code, document = check_json(capsys, root)

    assert code == 0
    assert document["files"]["total"] == 2


def test_command_line_exclusions_add_to_the_configured_ones(tmp_path, capsys):
    config = CONFIG.replace(
        '[project]\nsource_roots = ["src"]\n',
        '[project]\nsource_roots = ["src"]\nexclude_paths = ["src/shop/domain/legacy/"]\n',
    )
    project(
        tmp_path,
        {
            "src/shop/domain/legacy/a.py": VIOLATION,
            "src/shop/domain/old.py": VIOLATION,
            "src/shop/domain/new.py": VIOLATION,
        },
        config,
    )

    code, document = check_json(capsys, tmp_path, "--exclude", "src/shop/domain/old.py")

    assert code == 1
    assert [item["path"] for item in document["diagnostics"]] == [
        "src/shop/domain/new.py"
    ]
    assert document["files"]["excluded"] == 2

    code, out, err = run(capsys, "check", tmp_path, "--exclude", "../elsewhere/")
    assert (code, out) == (2, "")
    assert "--exclude '../elsewhere/'" in err


@posix_only
def test_file_names_are_reported_safely(tmp_path, capsys):
    project(tmp_path, {"src/shop/ok.py": "x = 1\n"})
    shop = os.fsencode(tmp_path / "src/shop")
    for name in (b"caf\xe9.py", b"back\\slash.py", b"red\x1b[31m.py"):
        Path(os.fsdecode(shop + b"/" + name)).write_text("x = 1\n")

    code, out, _ = run(capsys, "check", tmp_path)

    assert code == 2
    assert "\x1b" not in out

    def invalid(name: str, problem: str) -> str:
        return (
            f"src/shop/{name}: not analyzed [invalid_path] The path cannot name a "
            f"file in the repository: {problem}"
        )

    assert out.splitlines()[:3] == [
        invalid("back\\slash.py", "the file name contains a backslash"),
        invalid("caf\N{REPLACEMENT CHARACTER}.py", "the file name is not valid UTF-8"),
        invalid("red\\x1b[31m.py", "path contains control characters"),
    ]


# check: configuration and usage errors --------------------------------------


@pytest.mark.parametrize(
    ("files", "args", "message"),
    [
        ({"invariant.toml": "version = 1\n[[rules]\n"}, (), "invariant.toml: line 2"),
        (
            {"invariant.toml": 'version = 1\n[project]\nfail_on = "never"\n'},
            (),
            "project.fail_on",
        ),
        ({}, (), "create one with 'invariant init'"),
        ({}, ("--config", "missing.toml"), "missing.toml: cannot read the file"),
        (
            {
                "invariant.toml": "version = 1\n",
                "pyproject.toml": "[tool.invariant]\nversion = 1\n",
            },
            (),
            "keep one or choose with --config",
        ),
    ],
)
def test_configuration_problems_exit_with_2(tmp_path, capsys, files, args, message):
    write(tmp_path, {"src/a.py": "x = 1\n", **files})

    code, out, err = run(capsys, "check", tmp_path, *args)

    assert (code, out) == (2, "")
    assert message in err
    assert all(line.startswith("invariant: ") for line in err.splitlines())


def test_the_path_must_be_a_directory(tmp_path, capsys):
    target = write(tmp_path, {"a.py": "x = 1\n"}) / "a.py"

    assert run(capsys, "check", target)[0] == 2
    assert run(capsys, "check", tmp_path / "missing")[0] == 2


def test_an_internal_error_exits_with_2_not_1(tmp_path, capsys, monkeypatch):
    project(tmp_path, {"src/a.py": "x = 1\n"})

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("invariant.cli.analyze", broken)

    code, _, err = run(capsys, "check", tmp_path)

    assert code == 2
    assert "RuntimeError: boom" in err
    assert err.splitlines()[-1] == "invariant: internal error"


def test_usage_errors_exit_with_2(capsys):
    for args in ([], ["check", "--format", "xml"], ["lint"]):
        with pytest.raises(SystemExit) as exit_info:
            main(args)
        assert exit_info.value.code == 2
    capsys.readouterr()


# check: Git diff mode ------------------------------------------------------


@pytest.fixture
def repository(tmp_path, monkeypatch):
    """An empty Git repository, isolated from the user's Git configuration."""
    for name, value in {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(name, value)
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    return root


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(root: Path, message: str = "change") -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


@requires_git
def test_diff_mode_checks_the_head_commit_not_the_working_tree(repository, capsys):
    padding = "".join(f"value_{index} = {index}\n" for index in range(20))
    project(
        repository,
        {
            "src/shop/domain/legacy.py": VIOLATION,
            "src/shop/domain/old name.py": padding,
            "src/shop/domain/gone.py": "x = 1\n",
            "src/shop/domain/order.py": "x = 1\n",
        },
    )
    base = commit(repository, "base")
    write(repository, {"src/shop/domain/order.py": VIOLATION})
    git(repository, "mv", "src/shop/domain/old name.py", "src/shop/domain/new name.py")
    git(repository, "rm", "-q", "src/shop/domain/gone.py")
    head = commit(repository, "head")
    # Neither of these working tree changes may be read.
    write(repository, {"src/shop/domain/new name.py": VIOLATION + padding})
    (repository / "src/shop/domain/order.py").unlink()

    code, document = check_json(capsys, repository, "--base", "HEAD~1")

    assert code == 1
    assert [item["path"] for item in document["diagnostics"]] == [
        "src/shop/domain/order.py"
    ]
    assert (document["files"]["analyzed"], document["files"]["ignored"]) == (2, 1)
    assert document["source"] == {
        "mode": "git_diff",
        "config": f"invariant.toml at {head[:12]}",
        "base": base,
        "head": head,
    }

    code, out, _ = run(capsys, "check", repository, "--base", base, "--head", base)
    assert code == 0
    assert "NOT APPLICABLE: there is no Python file to check" in out


@requires_git
def test_diff_mode_reads_the_configuration_from_the_head_commit(repository, capsys):
    project(repository, {"src/shop/domain/order.py": "x = 1\n"})
    commit(repository, "base")
    write(repository, {"src/shop/domain/order.py": VIOLATION})
    head = commit(repository, "violation")
    # An uncommitted change that would hide the violation is not used...
    write(repository, {"invariant.toml": CONFIG.replace("shop.domain.*", "other.*")})

    code, out, _ = run(capsys, "check", repository, "--base", "HEAD~1")

    assert code == 1
    assert f"Configuration: invariant.toml at {head[:12]}" in out.splitlines()
    # ...unless it is chosen explicitly.
    explicit = repository / "invariant.toml"
    assert (
        run(capsys, "check", repository, "--base", "HEAD~1", "--config", explicit)[0]
        == 0
    )


@requires_git
def test_diff_mode_in_a_subdirectory_sees_only_its_files(repository, capsys):
    service = repository / "services/shop"
    project(service, {"src/shop/domain/order.py": "x = 1\n"})
    write(repository, {"other/src/shop/domain/order.py": "x = 1\n"})
    commit(repository, "base")
    write(service, {"src/shop/domain/order.py": VIOLATION})
    write(repository, {"other/src/shop/domain/order.py": VIOLATION})
    commit(repository, "both")

    code, document = check_json(capsys, service, "--base", "HEAD~1")

    assert code == 1
    assert [item["path"] for item in document["diagnostics"]] == [
        "src/shop/domain/order.py"
    ]
    assert document["files"]["total"] == 1


@requires_git
def test_diff_mode_reports_files_it_cannot_analyze(repository, capsys):
    project(repository, {"src/shop/ok.py": "x = 1\n"})
    commit(repository, "base")
    write(
        repository,
        {
            "src/shop/big.py": b"#" * (MAX_FILE_BYTES + 1),
            "src/shop/latin1.py": b"x = '\xe9'\n",
        },
    )
    if os.name == "posix":
        (repository / "src/shop/link.py").symlink_to("ok.py")
    commit(repository, "unreadable")

    code, document = check_json(capsys, repository, "--base", "HEAD~1")

    assert code == 2
    assert document["outcome"] == "incomplete"
    expected = {"src/shop/big.py": "too_large", "src/shop/latin1.py": "undecodable"}
    if os.name == "posix":
        expected["src/shop/link.py"] = "symlink"
    assert {item["path"]: item["code"] for item in document["diagnostics"]} == expected


@requires_git
@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--base=--output=x",), "'--output=x' is not a revision"),
        (("--base", "no-such-branch"), "'no-such-branch' does not name a commit"),
        (("--base", "HEAD", "--head", "HEAD~3"), "'HEAD~3' does not name a commit"),
        (("--head", "HEAD"), "--head needs --base"),
    ],
)
def test_diff_mode_rejects_unusable_revisions(repository, capsys, args, message):
    project(repository, {"src/a.py": "x = 1\n"})
    commit(repository)

    code, out, err = run(capsys, "check", repository, *args)

    assert (code, out) == (2, "")
    assert message in err
    assert not (repository / "x").exists()


@requires_git
def test_diff_mode_explains_a_shallow_clone(repository, tmp_path, capsys):
    project(repository, {"src/a.py": "x = 1\n"})
    commit(repository, "one")
    write(repository, {"src/a.py": "x = 2\n"})
    commit(repository, "two")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", repository.as_uri(), str(clone)],
        check=True,
        capture_output=True,
    )

    code, _, err = run(capsys, "check", clone, "--base", "HEAD~1")

    assert code == 2
    assert "shallow clone" in err


def test_diff_mode_outside_a_repository_is_an_error(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    project(tmp_path, {"src/a.py": "x = 1\n"})

    code, out, err = run(capsys, "check", tmp_path, "--base", "main")

    assert (code, out) == (2, "")
    assert err.startswith("invariant: ")


def test_python_dash_m_runs_the_same_command(tmp_path):
    project(tmp_path, {"src/shop/domain/order.py": VIOLATION})

    run_module = subprocess.run(
        [sys.executable, "-m", "invariant", "check", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert run_module.returncode == 1
    assert "FAIL: 1 violation at or above error" in run_module.stdout
