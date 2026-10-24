"""The command line, the API and the GitHub worker give the same diagnostics.

One configuration file and one set of files, with a finding of each of the
four rule types, a warning, a file that cannot be parsed and a clean file,
go through all three: ``invariant check``, a direct analysis of a project
that imported the same file, and a pull request analysis by the worker. The
results are compared as the engine writes them.
"""

import json
from typing import Self

from app.github import ChangedPythonFiles, PullRequestMetadata
from app.schemas import SourceFile
from app.worker import run_worker
from invariant.cli import main

CONFIG = """\
version = 1

[project]
source_roots = ["."]
fail_on = "error"

[[rules]]
key = "domain-isolation"
name = "Domain isolation"
rule_type = "forbidden_import"
source_pattern = "app.domain.*"
forbidden_imports = ["app.infrastructure"]

[[rules]]
key = "no-subprocess"
name = "No subprocess"
rule_type = "forbidden_call"
forbidden_calls = ["subprocess.run"]

[[rules]]
key = "absolute"
name = "Absolute imports"
rule_type = "no_relative_import"
severity = "warning"

[[rules]]
key = "short-files"
name = "Short files"
rule_type = "max_file_lines"
max_lines = 20
severity = "warning"
"""
FILES = {
    "app/domain/orders.py": "import app.infrastructure.db\nfrom . import helpers\n",
    "app/domain/pay.py": "import subprocess as sp\n\nsp.run(['ls'])\n",
    "app/domain/long.py": "".join(f"value_{n} = {n}\n" for n in range(25)),
    "app/domain/broken.py": "def broken(:\n",
    "app/clean.py": "x = 1\n",
}


class PullRequestOfTheFiles:
    comment_enabled = False

    def get_pull_request(self, owner, repo, pull_number) -> PullRequestMetadata:
        return PullRequestMetadata(number=pull_number, head_sha="f" * 40)

    def get_changed_python_files(self, owner, repo, pull_request, **options):
        return ChangedPythonFiles(
            files=[SourceFile(path=p, content=c) for p, c in FILES.items()],
            skipped=[],
            truncated=False,
            ignored=0,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def comparable(result: dict) -> dict:
    """What must be equal: the verdict, what was analyzed, every diagnostic."""
    files = result["files"]
    return {
        "outcome": result["outcome"],
        "passed": result["passed"],
        "files": (files["analyzed"], files["skipped"], files["excluded"]),
        "rules": result["rules"],
        "diagnostics": result["diagnostics"],
    }


def test_the_cli_the_api_and_github_report_the_same(client, tmp_path, capsys):
    for path, content in FILES.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(content)
    (tmp_path / "invariant.toml").write_text(CONFIG)
    exit_code = main(["check", str(tmp_path), "--format", "json"])
    command_line = json.loads(capsys.readouterr().out)

    project = client.post(
        "/projects", json={"name": "Parity", "github_owner": "o", "github_repo": "r"}
    ).json()["id"]
    imported = client.post(
        f"/projects/{project}/config/import",
        json={"content": CONFIG, "mode": "replace"},
    )
    assert imported.status_code == 200, imported.text
    direct = client.post(
        f"/projects/{project}/analyze",
        json={"files": [{"path": p, "content": c} for p, c in FILES.items()]},
    ).json()
    api = client.get(f"/analysis-runs/{direct['id']}/result").json()
    queued = client.post(f"/projects/{project}/analyze/github", json={"pull_number": 7})
    assert run_worker(github_factory=PullRequestOfTheFiles, until_idle=True) == 1
    github = client.get(f"/analysis-runs/{queued.json()['id']}/result").json()

    assert comparable(api) == comparable(command_line)
    assert comparable(github) == comparable(command_line)
    # Each rule type, the warning and the broken file are all there.
    found = {(item["code"], item["severity"]) for item in command_line["diagnostics"]}
    assert found == {
        ("forbidden_import", "error"),
        ("forbidden_call", "error"),
        ("no_relative_import", "warning"),
        ("max_file_lines", "warning"),
        ("syntax_error", "error"),
    }
    # A blocking violation fails the check even though a file is broken.
    assert (command_line["outcome"], exit_code) == ("fail", 1)
