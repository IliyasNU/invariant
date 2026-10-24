"""The release names one version everywhere, and documents itself."""

import re
import tomllib
from pathlib import Path

import pytest

from app.config import Settings
from app.github import GitHubClient
from invariant import __version__
from invariant.cli import main

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_one_version_in_the_package_the_api_the_cli_and_github(client, capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["--version"])
    project = client.post("/projects", json={"name": "Version"}).json()
    run = client.post(
        f"/projects/{project['id']}/analyze",
        json={"files": [{"path": "a.py", "content": "x = 1\n"}]},
    ).json()

    assert __version__ == PACKAGE
    assert client.get("/openapi.json").json()["info"]["version"] == PACKAGE
    assert (stopped.value.code, capsys.readouterr().out) == (
        0,
        f"invariant {PACKAGE}\n",
    )
    with GitHubClient(Settings(_env_file=None)) as github:
        assert github._client.headers["User-Agent"] == f"invariant/{PACKAGE}"
    # Results, exports and pull request comments carry the engine version.
    assert run["engine_version"] == PACKAGE


def test_the_changelog_opens_with_this_version_and_its_upgrade_guide():
    changelog = (ROOT / "CHANGELOG.md").read_text()

    [latest] = re.findall(r"^## (\S+)", changelog, re.MULTILINE)[:1]
    assert latest == PACKAGE
    assert "docs/upgrade-from-v0.2.md" in changelog
    assert (ROOT / "docs" / "upgrade-from-v0.2.md").is_file()


@pytest.mark.parametrize(
    "section",
    [
        "Quick start",
        "How rules work",
        "Command line",
        "API key and exposure",
        "GitHub webhook",
        "Worker",
        "Backup and restore",
        "Troubleshooting",
        "Security and limitations",
    ],
)
def test_the_readme_covers_what_the_release_checklist_asks(section):
    assert re.search(
        rf"^##+ {re.escape(section)}$", (ROOT / "README.md").read_text(), re.MULTILINE
    )


def cited_tests(text: str) -> set[tuple[str, str | None]]:
    """``(file, function)`` for each test a document cites; a bare file is ``None``.

    ``test_file::test_function`` names a test; a following ``::test_other`` is
    in the same file; ``test_file`` alone stands for the whole file.
    """
    cited = set()
    for line in text.splitlines():
        current = None
        for match in re.finditer(r"`(test_\w+)?(?:::(test_\w+))?`", line):
            module, function = match.groups()
            current = module or current
            if current is not None and (module or function):
                cited.add((current, function))
    return cited


@pytest.mark.parametrize("document", ["v1.0-traceability.md", "release-checklist.md"])
def test_every_test_the_documents_cite_exists(document):
    cited = cited_tests((ROOT / "docs" / document).read_text())
    assert len(cited) > 10

    missing = []
    for module, function in sorted(cited, key=str):
        path = ROOT / "tests" / f"{module}.py"
        if not path.is_file() or (
            function
            and not re.search(rf"^def {function}\(", path.read_text(), re.MULTILINE)
        ):
            missing.append(f"{module}::{function}" if function else module)
    assert missing == []
