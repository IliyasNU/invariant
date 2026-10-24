import pytest
from sqlalchemy.exc import DataError, OperationalError


def create_project(client, name="Payments"):
    response = client.post(
        "/projects", json={"name": name, "description": "Payment service rules"}
    )
    assert response.status_code == 201
    return response.json()


def test_health_endpoints(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/db-health").json() == {"database": "ok", "result": 1}


def test_project_crud_and_conflict(client):
    project = create_project(client)

    assert client.get("/projects").json() == [project]
    assert client.get(f"/projects/{project['id']}").json() == project
    assert client.get("/projects/999").status_code == 404

    duplicate = client.post("/projects", json={"name": "Payments"})
    assert duplicate.status_code == 409


def test_project_github_configuration(client):
    response = client.post(
        "/projects",
        json={
            "name": "Invariant",
            "github_owner": "IliyasNU",
            "github_repo": "Invariant",
        },
    )

    assert response.status_code == 201
    assert response.json()["github_owner"] == "iliyasnu"
    assert response.json()["github_repo"] == "invariant"
    assert (
        client.post(
            "/projects", json={"name": "Incomplete", "github_owner": "owner"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/projects",
            json={
                "name": "Duplicate repository",
                "github_owner": "ILIYASNU",
                "github_repo": "INVARIANT",
            },
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    ("owner", "repo"), [("..", "invariant"), ("iliyasnu", ".."), (".", ".")]
)
def test_github_names_cannot_be_dot_segments(client, owner, repo):
    # "/repos/../../pulls/1" is "/pulls/1" once the URL is resolved: the
    # token would be sent to an endpoint other than the repository's.
    project = create_project(client, name="Dot segments")

    created = client.post(
        "/projects",
        json={"name": "Dots", "github_owner": owner, "github_repo": repo},
    )
    updated = client.patch(
        f"/projects/{project['id']}",
        json={"github_owner": owner, "github_repo": repo},
    )

    assert (created.status_code, updated.status_code) == (422, 422)
    assert (
        client.post(
            "/projects",
            json={"name": "Dotted", "github_owner": "o", "github_repo": ".github"},
        ).json()["github_repo"]
        == ".github"
    )


def test_existing_project_can_add_and_clear_github_configuration(client):
    project = create_project(client, name="Existing v0.1 project")

    configured = client.patch(
        f"/projects/{project['id']}",
        json={"github_owner": "IliyasNU", "github_repo": "invariant"},
    )
    assert configured.status_code == 200
    assert configured.json()["github_owner"] == "iliyasnu"
    assert (
        client.patch(
            f"/projects/{project['id']}", json={"github_owner": "owner"}
        ).status_code
        == 422
    )

    cleared = client.patch(
        f"/projects/{project['id']}",
        json={"github_owner": None, "github_repo": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["github_owner"] is None
    assert cleared.json()["github_repo"] is None


def test_rule_crud_and_validation(client):
    project = create_project(client)
    payload = {
        "name": "Domain isolation",
        "source_pattern": "app.domain.*",
        "forbidden_imports": ["app.infrastructure", "fastapi"],
    }

    response = client.post(f"/projects/{project['id']}/rules", json=payload)
    assert response.status_code == 201
    rule = response.json()
    assert rule["rule_type"] == "forbidden_import"
    assert client.get(f"/projects/{project['id']}/rules").json() == [rule]

    duplicate = client.post(f"/projects/{project['id']}/rules", json=payload)
    assert duplicate.status_code == 409
    invalid = client.post(
        f"/projects/{project['id']}/rules",
        json={**payload, "name": "Invalid", "forbidden_imports": ["not valid"]},
    )
    assert invalid.status_code == 422
    assert client.post("/projects/999/rules", json=payload).status_code == 404


def test_analysis_returns_deterministic_violations(client):
    project = create_project(client)
    client.post(
        f"/projects/{project['id']}/rules",
        json={
            "name": "Domain isolation",
            "source_pattern": "app.domain.*",
            "forbidden_imports": ["app.infrastructure", "fastapi"],
        },
    )

    response = client.post(
        f"/projects/{project['id']}/analyze",
        json={
            "files": [
                {
                    "path": "app/domain/service.py",
                    "content": (
                        "from app.infrastructure.db import repository\n"
                        "import fastapi.routing\n"
                        "import json\n"
                    ),
                },
                {"path": "app/api.py", "content": "import fastapi\n"},
            ]
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["source"] == "direct"
    assert result["status"] == "completed"
    assert result["passed"] is False
    assert result["violation_count"] == 2
    assert result["files_analyzed"] == 2
    assert result["rules_evaluated"] == 1
    assert [item["imported_module"] for item in result["violations"]] == [
        "app.infrastructure.db",
        "fastapi.routing",
    ]
    assert [item["line"] for item in result["violations"]] == [1, 2]

    history = client.get(f"/projects/{project['id']}/analysis-runs")
    assert history.status_code == 200
    assert history.json() == [
        {
            key: value
            for key, value in result.items()
            if key not in {"violations", "config"}
        }
    ]

    detail = client.get(f"/analysis-runs/{result['id']}")
    assert detail.status_code == 200
    assert detail.json() == result
    assert client.get("/analysis-runs/999").status_code == 404
    assert client.get("/projects/999/analysis-runs").status_code == 404


def test_invalid_python_makes_the_run_incomplete_not_a_pass(client):
    project = create_project(client)
    client.post(
        f"/projects/{project['id']}/rules",
        json={"name": "Domain isolation", "forbidden_imports": ["app.db"]},
    )
    response = client.post(
        f"/projects/{project['id']}/analyze",
        json={"files": [{"path": "broken.py", "content": "def broken(:\n"}]},
    )

    assert response.status_code == 200
    run = response.json()
    assert (run["status"], run["outcome"], run["passed"]) == (
        "completed",
        "incomplete",
        None,
    )
    [error] = run["violations"]
    assert (error["kind"], error["code"], error["path"], error["line"]) == (
        "error",
        "syntax_error",
        "broken.py",
        1,
    )
    assert run["files"] == {
        "total": 1,
        "eligible": 1,
        "analyzed": 0,
        "excluded": 0,
        "skipped": 1,
        "ignored": 0,
    }
    assert run["summary"] == {"errors": 0, "warnings": 0, "analysis_errors": 1}
    assert [
        item["id"]
        for item in client.get(f"/projects/{project['id']}/analysis-runs").json()
    ] == [run["id"]]


def test_a_broken_file_does_not_hide_violations_in_the_others(client):
    project = create_project(client)
    client.post(
        f"/projects/{project['id']}/rules",
        json={"name": "Domain isolation", "forbidden_imports": ["app.db"]},
    )

    run = client.post(
        f"/projects/{project['id']}/analyze",
        json={
            "files": [
                {"path": "app/a.py", "content": "import app.db\n"},
                {"path": "app/b.py", "content": "def broken(:\n"},
            ]
        },
    ).json()

    # A blocking violation fails the run even though a file was not analyzed.
    assert (run["outcome"], run["passed"]) == ("fail", False)
    assert [(item["kind"], item["path"]) for item in run["violations"]] == [
        ("violation", "app/a.py"),
        ("error", "app/b.py"),
    ]
    assert run["violation_count"] == 1


def test_too_complex_python_is_reported_as_such(client):
    project = create_project(client)
    response = client.post(
        f"/projects/{project['id']}/analyze",
        json={"files": [{"path": "deep.py", "content": "x = " + "-" * 200_000 + "1"}]},
    )

    assert response.status_code == 200
    run = response.json()
    assert run["outcome"] == "incomplete"
    [error] = run["violations"]
    assert (error["code"], error["path"], error["message"]) == (
        "too_complex",
        "deep.py",
        "Python source is too complex to parse",
    )


def analyze(client, project_id, content="import json\n"):
    response = client.post(
        f"/projects/{project_id}/analyze",
        json={"files": [{"path": "app/domain/service.py", "content": content}]},
    )
    assert response.status_code == 200
    return response.json()


def test_rule_deletion_keeps_history(client):
    project = create_project(client)
    other = create_project(client, name="Other")
    rule = client.post(
        f"/projects/{project['id']}/rules",
        json={
            "name": "Domain isolation",
            "source_pattern": "app.domain.*",
            "forbidden_imports": ["fastapi"],
        },
    ).json()
    run = analyze(client, project["id"], content="import fastapi\n")
    assert run["violations"][0]["rule_id"] == rule["id"]

    url = f"/projects/{project['id']}/rules/{rule['id']}"
    assert (
        client.delete(f"/projects/{other['id']}/rules/{rule['id']}").status_code == 404
    )
    assert client.delete(url).status_code == 204
    assert client.delete(url).status_code == 404
    assert client.get(f"/projects/{project['id']}/rules").json() == []

    [violation] = client.get(f"/analysis-runs/{run['id']}").json()["violations"]
    assert violation["rule_id"] is None
    assert violation["rule_name"] == "Domain isolation"


def test_analysis_history_is_paginated_newest_first(client):
    project = create_project(client)
    run_ids = [analyze(client, project["id"])["id"] for _ in range(3)]
    url = f"/projects/{project['id']}/analysis-runs"

    assert [run["id"] for run in client.get(url).json()] == run_ids[::-1]
    page = client.get(url, params={"limit": 2}).json()
    assert [run["id"] for run in page] == run_ids[:0:-1]
    page = client.get(url, params={"limit": 2, "offset": 2}).json()
    assert [run["id"] for run in page] == run_ids[:1]
    assert client.get(url, params={"limit": 0}).status_code == 422
    assert client.get(url, params={"limit": 201}).status_code == 422


def test_long_diagnostics_are_stored_and_read_back(client):
    project = create_project(client)
    long_import = "app.infrastructure." + "a" * 600
    long_forbidden = "app." + "b" * 1100
    response = client.post(
        f"/projects/{project['id']}/rules",
        json={
            "name": "Infrastructure",
            "source_pattern": "*",
            "forbidden_imports": ["app.infrastructure", long_forbidden],
        },
    )
    assert response.status_code == 201

    run = analyze(
        client,
        project["id"],
        content=f"import {long_import}\nimport {long_forbidden}\n",
    )

    assert [item["imported_module"] for item in run["violations"]] == [
        long_import,
        long_forbidden,
    ]
    assert len(run["violations"][1]["message"]) > 2000
    assert client.get(f"/analysis-runs/{run['id']}").json() == run


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (OperationalError("SELECT 1", {}, Exception("connection refused")), 503),
        (DataError("INSERT", {}, Exception("value too long")), 500),
    ],
)
def test_only_connection_errors_report_the_database_unavailable(
    client, monkeypatch, error, status_code
):
    project = create_project(client)

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("app.api.persist_analysis", fail)
    response = client.post(
        f"/projects/{project['id']}/analyze",
        json={"files": [{"path": "app/x.py", "content": "import json\n"}]},
    )

    assert response.status_code == status_code
    error = response.json()["error"]
    if status_code == 500:
        assert (error["code"], error["message"]) == (
            "storage_error",
            "The database rejected the operation",
        )
    else:
        assert (error["code"], error["message"]) == (
            "database_unavailable",
            "Database unavailable",
        )
    # Neither the SQL nor the driver's message reaches the client.
    assert "INSERT" not in response.text
    assert "too long" not in response.text


@pytest.mark.parametrize(
    "path", ["bad\u0000.py", "app/\u001b[31mred.py", "line\nbreak.py", "del\u007f.py"]
)
def test_paths_with_control_characters_are_rejected(client, path):
    project = create_project(client)

    response = client.post(
        f"/projects/{project['id']}/analyze",
        json={"files": [{"path": path, "content": "import json\n"}]},
    )

    assert response.status_code == 422
    assert client.get(f"/projects/{project['id']}/analysis-runs").json() == []


def test_stored_text_rejects_control_characters(client):
    assert client.post("/projects", json={"name": "a\u0000b"}).status_code == 422
    assert (
        client.post("/projects", json={"name": "Nul", "description": "x\u0000"})
    ).status_code == 422
    project = client.post(
        "/projects", json={"name": "Text", "description": "line one\nline two\tend"}
    )
    assert project.status_code == 201
    assert project.json()["description"] == "line one\nline two\tend"

    project_id = project.json()["id"]
    assert (
        client.patch(f"/projects/{project_id}", json={"description": "\u0000"})
    ).status_code == 422
    rule = {"name": "r\u0000", "forbidden_imports": ["fastapi"]}
    assert client.post(f"/projects/{project_id}/rules", json=rule).status_code == 422
    assert client.get("/projects").json() == [project.json()]


def test_openapi_document_describes_every_rule_type(client):
    spec = client.get("/openapi.json").json()

    body = spec["paths"]["/projects/{project_id}/rules"]["post"]["requestBody"]
    schemas = body["content"]["application/json"]["schema"]["oneOf"]
    assert [item["$ref"].rsplit("/", 1)[1] for item in schemas] == [
        "ForbiddenImportRuleCreate",
        "ForbiddenCallRuleCreate",
        "NoRelativeImportRuleCreate",
        "MaxFileLinesRuleCreate",
    ]
    export = spec["paths"]["/projects/{project_id}/config"]["get"]["responses"]["200"]
    assert list(export["content"]) == ["application/toml"]
    assert client.get("/docs").status_code == 200
