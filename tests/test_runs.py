from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models import AnalysisRun
from invariant import __version__
from invariant.config import ForbiddenImportRule, ProjectSettings
from invariant.engine import Source, analyze

DOMAIN_RULE = {
    "name": "Domain isolation",
    "source_pattern": "app.domain.*",
    "forbidden_imports": ["app.infrastructure"],
}


def create_project(client, **fields):
    response = client.post("/projects", json={"name": "Payments", **fields})
    assert response.status_code == 201, response.json()
    return response.json()


def create_rule(client, project_id, **fields):
    response = client.post(
        f"/projects/{project_id}/rules", json={**DOMAIN_RULE, **fields}
    )
    assert response.status_code == 201, response.json()
    return response.json()


def run_analysis(client, project_id, files: dict[str, str]):
    response = client.post(
        f"/projects/{project_id}/analyze",
        json={
            "files": [{"path": path, "content": text} for path, text in files.items()]
        },
    )
    assert response.status_code == 200, response.json()
    return response.json()


def test_every_run_records_its_timing_engine_and_configuration(client):
    project = create_project(client, source_roots=["src"])
    create_rule(client, project["id"])

    run = run_analysis(client, project["id"], {"src/app/domain/a.py": "x = 1\n"})

    started = datetime.fromisoformat(run["started_at"])
    finished = datetime.fromisoformat(run["finished_at"])
    assert started <= finished
    assert run["duration_ms"] >= 0
    assert run["engine_version"] == __version__
    assert len(run["config_hash"]) == 64
    config = client.get(f"/analysis-runs/{run['id']}").json()["config"]
    assert config["project"] == {
        "source_roots": ["src"],
        "exclude_paths": [],
        "fail_on": "error",
    }
    assert [(rule["key"], rule["forbidden_imports"]) for rule in config["rules"]] == [
        ("domain-isolation", ["app.infrastructure"])
    ]


def test_editing_or_deleting_a_rule_does_not_rewrite_history(client):
    project = create_project(client)
    rule = create_rule(client, project["id"])
    files = {"app/domain/a.py": "import app.infrastructure\n"}
    first = run_analysis(client, project["id"], files)
    before = client.get(f"/analysis-runs/{first['id']}").json()
    url = f"/projects/{project['id']}/rules/{rule['id']}"

    client.patch(url, json={"name": "Renamed", "forbidden_imports": ["fastapi"]})
    second = run_analysis(client, project["id"], files)
    assert client.delete(url).status_code == 204

    after = client.get(f"/analysis-runs/{first['id']}").json()
    assert after["config"] == before["config"]
    assert after["config_hash"] == first["config_hash"] != second["config_hash"]
    assert (after["outcome"], second["outcome"]) == ("fail", "pass")
    [violation] = after["violations"]
    assert (violation["rule_id"], violation["rule_name"]) == (None, "Domain isolation")


def test_file_and_rule_counters_add_up(client):
    project = create_project(client, exclude_paths=["app/legacy/"])
    create_rule(client, project["id"])
    create_rule(client, project["id"], name="Disabled", enabled=False)

    run = run_analysis(
        client,
        project["id"],
        {
            "app/domain/a.py": "import app.infrastructure\n",
            "app/legacy/b.py": "import app.infrastructure\n",
            "app/c.py": "def broken(:\n",
        },
    )

    files = run["files"]
    assert files == {
        "total": 3,
        "eligible": 2,
        "analyzed": 1,
        "excluded": 1,
        "skipped": 1,
        "ignored": 0,
    }
    assert files["total"] == files["eligible"] + files["excluded"] + files["ignored"]
    assert files["eligible"] == files["analyzed"] + files["skipped"]
    assert run["rules"] == {"total": 2, "enabled": 1, "evaluations": 1}
    assert run["summary"] == {"errors": 1, "warnings": 0, "analysis_errors": 1}
    assert (run["outcome"], run["violation_count"]) == ("fail", 1)


def test_the_recorded_threshold_decides_whether_warnings_fail(client):
    project = create_project(client)
    create_rule(client, project["id"], severity="warning")
    files = {"app/domain/a.py": "import app.infrastructure\n"}

    default = run_analysis(client, project["id"], files)
    client.patch(f"/projects/{project['id']}", json={"fail_on": "warning"})
    strict = run_analysis(client, project["id"], files)

    assert (default["outcome"], default["passed"], default["fail_on"]) == (
        "pass",
        True,
        "error",
    )
    assert default["summary"]["warnings"] == 1
    assert (strict["outcome"], strict["passed"], strict["fail_on"]) == (
        "fail",
        False,
        "warning",
    )


@pytest.mark.parametrize("rules", [[], [{"source_pattern": "other.*"}]])
def test_nothing_checked_is_not_applicable_rather_than_a_pass(client, rules):
    project = create_project(client)
    for fields in rules:
        create_rule(client, project["id"], **fields)

    run = run_analysis(client, project["id"], {"app/domain/a.py": "x = 1\n"})

    assert (run["status"], run["outcome"], run["passed"]) == (
        "completed",
        "not_applicable",
        None,
    )
    assert run["rules"]["evaluations"] == 0


def test_the_exported_result_is_the_engines_own_document(client):
    project = create_project(client, exclude_paths=["app/legacy/"])
    create_rule(client, project["id"])
    files = {
        "app/domain/a.py": 'x = "é"; import app.infrastructure.db\n',
        "app/legacy/b.py": "import app.infrastructure\n",
        "app/c.py": "def broken(:\n",
    }
    run = run_analysis(client, project["id"], files)

    exported = client.get(f"/analysis-runs/{run['id']}/result")

    assert exported.status_code == 200
    document = exported.json()
    engine = analyze(
        [Source(path, text) for path, text in files.items()],
        [
            ForbiddenImportRule(
                rule_type="forbidden_import", key="domain-isolation", **DOMAIN_RULE
            )
        ],
        ProjectSettings(exclude_paths=["app/legacy/"]),
    ).to_dict()
    assert {key: document[key] for key in engine} == engine
    assert document["source"] == {
        "kind": "direct",
        "repository_owner": None,
        "repository_name": None,
        "pull_number": None,
        "base_sha": None,
        "head_sha": None,
    }
    assert (document["run"]["id"], document["run"]["status"]) == (
        run["id"],
        "completed",
    )
    assert document["run"]["config_hash"] == run["config_hash"]
    assert client.get("/analysis-runs/999/result").status_code == 404


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "done", "outcome": None, "passed": None},
        {"status": "completed", "outcome": None, "passed": None},
        {"status": "completed", "outcome": "maybe", "passed": None},
        {"status": "completed", "outcome": "pass", "passed": False},
        {"status": "completed", "outcome": "fail", "passed": None},
        {"status": "completed", "outcome": "incomplete", "passed": True},
        {"status": "running", "outcome": "pass", "passed": True},
        {"status": "failed", "outcome": None, "passed": None},
        {
            "status": "completed",
            "outcome": "pass",
            "passed": True,
            "error_code": "github_unavailable",
        },
    ],
)
def test_the_database_refuses_inconsistent_runs(client, fields):
    project = create_project(client)

    with SessionLocal() as session:
        session.add(
            AnalysisRun(
                project_id=project["id"],
                source="direct",
                files_analyzed=0,
                rules_evaluated=0,
                violation_count=0,
                **fields,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "queued", "outcome": None, "passed": None},
        {"status": "running", "outcome": None, "passed": None},
        {"status": "completed", "outcome": "pass", "passed": True},
        {"status": "completed", "outcome": "fail", "passed": False},
        {"status": "completed", "outcome": "incomplete", "passed": None},
        {"status": "completed", "outcome": "not_applicable", "passed": None},
        {
            "status": "failed",
            "outcome": None,
            "passed": None,
            "error_code": "github_unavailable",
        },
    ],
)
def test_every_consistent_run_state_can_be_stored(client, fields):
    project = create_project(client)

    with SessionLocal() as session:
        run = AnalysisRun(
            project_id=project["id"],
            source="direct",
            files_analyzed=0,
            rules_evaluated=0,
            violation_count=0,
            **fields,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    stored = client.get(f"/analysis-runs/{run_id}").json()
    assert (stored["status"], stored["outcome"], stored["passed"]) == (
        fields["status"],
        fields["outcome"],
        fields["passed"],
    )
