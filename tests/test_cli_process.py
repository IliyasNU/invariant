"""The ``invariant`` command run as a real process, as CI and users run it.

These tests import nothing from the project. CI also runs them against the
wheel installed in a clean environment, with INVARIANT_CLI set to that
installation's ``invariant`` script (``pytest --noconftest``).
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

INSTALLED = os.environ.get("INVARIANT_CLI")
COMMAND = [INSTALLED] if INSTALLED else [sys.executable, "-m", "invariant"]
# The directory holding the ``invariant`` script, for running the example.
SCRIPTS = Path(INSTALLED).parent if INSTALLED else Path(sys.executable).parent
REPOSITORY = Path(__file__).resolve().parent.parent

CONFIG = """\
version = 1

[project]
source_roots = ["src"]

[[rules]]
key = "domain-isolation"
rule_type = "forbidden_import"
source_pattern = "shop.domain.*"
forbidden_imports = ["shop.infrastructure"]

[[rules]]
key = "absolute"
rule_type = "no_relative_import"
severity = "warning"
"""

GIT_ENVIRONMENT = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def invariant(*args: object, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*COMMAND, *map(str, args)],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def write(root: Path, files: dict[str, str | bytes]) -> Path:
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    return root


def test_version_help_and_both_entry_points(tmp_path):
    version = invariant("--version", cwd=tmp_path)
    assert version.returncode == 0
    assert re.fullmatch(r"invariant \d+\.\d+\.\d+\S*\n", version.stdout)

    python = SCRIPTS / ("python.exe" if os.name == "nt" else "python")
    module = subprocess.run(
        [str(python if INSTALLED else sys.executable), "-m", "invariant", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert module.stdout == version.stdout

    help_text = invariant("--help", cwd=tmp_path)
    assert help_text.returncode == 0
    assert "init" in help_text.stdout
    assert "check" in help_text.stdout
    assert invariant(cwd=tmp_path).returncode == 2


SCENARIOS = {
    "clean": ({"src/shop/domain/order.py": "x = 1\n"}, CONFIG, (), 0),
    "violation": (
        {"src/shop/domain/order.py": "import shop.infrastructure\n"},
        CONFIG,
        (),
        1,
    ),
    "warning only": ({"src/shop/api.py": "from . import x\n"}, CONFIG, (), 0),
    "warning threshold": (
        {"src/shop/api.py": "from . import x\n"},
        CONFIG,
        ("--fail-on", "warning"),
        1,
    ),
    "malformed config": (
        {"src/shop/api.py": "x = 1\n"},
        "version = 1\n[[rules]\n",
        (),
        2,
    ),
    "syntax error": ({"src/shop/api.py": "def (:\n"}, CONFIG, (), 2),
    "over the size limit": (
        {"src/shop/api.py": b"#" * 1_000_001},
        CONFIG,
        (),
        2,
    ),
    "nothing applies": ({"docs/x.md": "x\n"}, CONFIG, (), 0),
    "nothing applies, strict": ({"docs/x.md": "x\n"}, CONFIG, ("--strict",), 2),
}


@pytest.mark.parametrize(
    ("files", "config", "options", "code"),
    list(SCENARIOS.values()),
    ids=list(SCENARIOS),
)
def test_exit_codes(tmp_path, files, config, options, code):
    write(tmp_path, {"invariant.toml": config, **files})

    result = invariant("check", ".", *options, cwd=tmp_path)

    assert result.returncode == code, result.stdout + result.stderr


def test_json_on_stdout_is_the_document_alone(tmp_path):
    write(
        tmp_path,
        {
            "invariant.toml": CONFIG,
            "src/shop/domain/order.py": "import shop.infrastructure\n",
        },
    )

    result = invariant("check", ".", "--format", "json", cwd=tmp_path)

    assert (result.returncode, result.stderr) == (1, "")
    document = json.loads(result.stdout)
    assert (document["schema_version"], document["outcome"]) == (1, "fail")
    assert [item["rule_key"] for item in document["diagnostics"]] == [
        "domain-isolation"
    ]


def _example_commands() -> dict[str, str]:
    """The ``invariant check`` commands of the GitHub Actions example."""
    workflow = (REPOSITORY / "examples/github-actions.yml").read_text(encoding="utf-8")
    commands = re.findall(r"^\s*run: (invariant check .*)$", workflow, re.MULTILINE)
    assert len(commands) == 2, commands
    return {
        "pull_request": next(item for item in commands if "--base" in item),
        "push": next(item for item in commands if "--base" not in item),
    }


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="needs git and bash",
)
def test_the_github_actions_example_on_the_demo_project(tmp_path):
    demo = tmp_path / "demo"
    shutil.copytree(REPOSITORY / "examples/demo", demo)
    environment = {
        **os.environ,
        **GIT_ENVIRONMENT,
        "PATH": f"{SCRIPTS}{os.pathsep}{os.environ.get('PATH', '')}",
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=demo,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def step(command: str, **variables: str) -> subprocess.CompletedProcess[str]:
        # The workflow's own command line, run by a shell as Actions runs it.
        return subprocess.run(
            ["bash", "-c", command],
            cwd=demo,
            env={**environment, **variables},
            capture_output=True,
            text=True,
            check=False,
        )

    commands = _example_commands()
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "clean")
    base = git("rev-parse", "HEAD")

    # A push of the clean project is green.
    clean = step(commands["push"])
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert clean.stdout.startswith("PASS")

    # A pull request adding a forbidden import is not.
    write(
        demo,
        {"src/shop/domain/order.py": "from shop.infrastructure import db\n"},
    )
    git("commit", "-q", "-am", "domain imports infrastructure")
    head = git("rev-parse", "HEAD")
    pull_request = step(commands["pull_request"], BASE=base, HEAD=head)
    assert pull_request.returncode == 1, pull_request.stdout + pull_request.stderr
    assert "[domain-isolation]" in pull_request.stdout
    assert "FAIL" in pull_request.stdout
