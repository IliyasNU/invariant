import pytest

from app.database import SessionLocal
from app.models import AnalysisRun

DOMAIN = {
    "name": "Domain isolation",
    "source_pattern": "app.domain.*",
    "forbidden_imports": ["app.infrastructure"],
}


def create_project(client, name="Payments"):
    response = client.post("/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()


def create_rule(client, project_id, **fields):
    response = client.post(f"/projects/{project_id}/rules", json=fields)
    assert response.status_code == 201, response.json()
    return response.json()


def run(client, project_id, files: dict[str, str]):
    response = client.post(
        f"/projects/{project_id}/analyze",
        json={
            "files": [{"path": path, "content": text} for path, text in files.items()]
        },
    )
    assert response.status_code == 200
    return response.json()


def ids(response) -> list[int]:
    return [item["id"] for item in response.json()]


def one_run_of_each_outcome(client, project_id) -> dict[str, int]:
    """Runs that pass, fail, are incomplete and are not applicable, in order."""
    return {
        "pass": run(client, project_id, {"app/domain/a.py": "x = 1\n"})["id"],
        "fail": run(
            client, project_id, {"app/domain/a.py": "import app.infrastructure\n"}
        )["id"],
        "incomplete": run(client, project_id, {"app/domain/a.py": "def (:\n"})["id"],
        "not_applicable": run(client, project_id, {"app/api.py": "x = 1\n"})["id"],
    }


def test_projects_and_rules_are_paginated_with_a_total(client):
    projects = [create_project(client, name=f"P{index}")["id"] for index in range(3)]
    rules = [
        create_rule(client, projects[0], name=f"R{index}", forbidden_imports=["x"])[
            "id"
        ]
        for index in range(3)
    ]

    first = client.get("/projects", params={"limit": 2})
    rest = client.get("/projects", params={"limit": 2, "offset": 2})
    assert (ids(first), ids(rest)) == (projects[:2], projects[2:])
    assert first.headers["X-Total-Count"] == rest.headers["X-Total-Count"] == "3"
    assert ids(client.get("/projects")) == projects

    url = f"/projects/{projects[0]}/rules"
    page = client.get(url, params={"offset": 1})
    assert (ids(page), page.headers["X-Total-Count"]) == (rules[1:], "3")
    for params in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
        assert client.get("/projects", params=params).status_code == 422
        assert client.get(url, params=params).status_code == 422


def test_run_history_can_be_filtered(client):
    project = create_project(client)
    create_rule(client, project["id"], **DOMAIN)
    runs = one_run_of_each_outcome(client, project["id"])
    url = f"/projects/{project['id']}/analysis-runs"

    for outcome, run_id in runs.items():
        response = client.get(url, params={"outcome": outcome})
        assert (ids(response), response.headers["X-Total-Count"]) == ([run_id], "1")
    everything = client.get(url, params={"status": "completed", "source": "direct"})
    assert ids(everything) == list(reversed(runs.values()))
    assert everything.headers["X-Total-Count"] == "4"
    page = client.get(url, params={"status": "completed", "limit": 1, "offset": 1})
    assert (ids(page), page.headers["X-Total-Count"]) == ([runs["incomplete"]], "4")
    assert ids(client.get(url, params={"source": "github_pr"})) == []
    assert ids(client.get(url, params={"status": "failed"})) == []
    for params in ({"outcome": "maybe"}, {"status": "done"}, {"pull_number": 0}):
        assert client.get(url, params=params).status_code == 422


def test_diagnostics_are_paginated_and_filtered(client):
    project = create_project(client)
    create_rule(client, project["id"], **DOMAIN)
    create_rule(
        client,
        project["id"],
        key="absolute",
        rule_type="no_relative_import",
        severity="warning",
    )
    result = run(
        client,
        project["id"],
        {
            "app/domain/a.py": "import app.infrastructure\nfrom . import b\n",
            "app/domain/b.py": "import app.infrastructure.db\n",
            "app/broken.py": "def (:\n",
        },
    )
    url = f"/analysis-runs/{result['id']}/diagnostics"

    def found(**params):
        response = client.get(url, params=params)
        assert response.status_code == 200
        items = [(item["path"], item["line"], item["code"]) for item in response.json()]
        return items, int(response.headers["X-Total-Count"])

    everything = [
        ("app/broken.py", 1, "syntax_error"),
        ("app/domain/a.py", 1, "forbidden_import"),
        ("app/domain/a.py", 2, "no_relative_import"),
        ("app/domain/b.py", 1, "forbidden_import"),
    ]
    assert found() == (everything, 4)
    assert found(limit=2, offset=1) == (everything[1:3], 4)
    assert found(kind="error") == (everything[:1], 1)
    assert found(severity="warning") == ([everything[2]], 1)
    assert found(code="forbidden_import") == ([everything[1], everything[3]], 2)
    assert found(rule_key="domain-isolation", path="app/domain/b.py") == (
        [everything[3]],
        1,
    )
    # The run detail still lists every diagnostic, in the same order.
    detail = client.get(f"/analysis-runs/{result['id']}").json()["violations"]
    assert [(item["path"], item["line"], item["code"]) for item in detail] == everything
    assert client.get("/analysis-runs/999/diagnostics").status_code == 404
    assert client.get(url, params={"kind": "warning"}).status_code == 422
    assert client.get(url, params={"path": ""}).status_code == 422


def test_project_statistics_count_every_run_and_explain_the_pass_rate(client):
    project = create_project(client)
    other = create_project(client, name="Other")
    create_rule(client, project["id"], **DOMAIN)
    runs = one_run_of_each_outcome(client, project["id"])
    # Applies to every module, so it comes after the not-applicable run.
    create_rule(
        client,
        project["id"],
        key="absolute",
        rule_type="no_relative_import",
        severity="warning",
    )
    warned = run(client, project["id"], {"app/domain/c.py": "from . import a\n"})
    run(client, other["id"], {"app/domain/a.py": "x = 1\n"})
    with SessionLocal() as session:
        failed = AnalysisRun(
            project_id=project["id"],
            source="github_pr",
            status="failed",
            error_code="github_unavailable",
            files_analyzed=0,
            rules_evaluated=0,
            violation_count=0,
        )
        session.add(failed)
        session.commit()
        failed_id = failed.id

    stats = client.get(f"/projects/{project['id']}/stats")

    assert stats.status_code == 200
    body = stats.json()
    assert (body["project_id"], body["runs"]) == (project["id"], 6)
    assert body["by_status"] == {
        "queued": 0,
        "running": 0,
        "completed": 5,
        "failed": 1,
    }
    assert body["by_outcome"] == {
        "pass": 2,
        "fail": 1,
        "incomplete": 1,
        "not_applicable": 1,
    }
    # Two passes out of three verdicts; three runs have no verdict at all.
    assert body["pass_rate"] == {
        "passed": 2,
        "decided": 3,
        "without_verdict": 3,
        "rate": pytest.approx(2 / 3),
    }
    assert body["diagnostics"] == {"errors": 1, "warnings": 1, "analysis_errors": 1}
    assert body["duration"]["runs"] == 5  # the failed run was inserted untimed
    assert body["duration"]["max_ms"] >= body["duration"]["average_ms"] >= 0
    assert [item["id"] for item in body["latest"]] == [
        failed_id,
        warned["id"],
        *reversed(runs.values()),
    ]
    assert client.get("/projects/999/stats").status_code == 404


def test_statistics_of_a_project_without_runs(client):
    project = create_project(client)

    body = client.get(f"/projects/{project['id']}/stats").json()

    assert (body["runs"], body["latest"]) == (0, [])
    assert body["pass_rate"] == {
        "passed": 0,
        "decided": 0,
        "without_verdict": 0,
        "rate": None,
    }
    assert body["duration"] == {"runs": 0, "average_ms": None, "max_ms": None}


def test_the_latest_runs_are_the_ten_newest(client):
    project = create_project(client)
    created = [
        run(client, project["id"], {"app/a.py": "x = 1\n"})["id"] for _ in range(12)
    ]

    body = client.get(f"/projects/{project['id']}/stats").json()

    assert [item["id"] for item in body["latest"]] == created[:1:-1]
