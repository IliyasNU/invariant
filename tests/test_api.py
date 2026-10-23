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
        {key: value for key, value in result.items() if key != "violations"}
    ]

    detail = client.get(f"/analysis-runs/{result['id']}")
    assert detail.status_code == 200
    assert detail.json() == result
    assert client.get("/analysis-runs/999").status_code == 404
    assert client.get("/projects/999/analysis-runs").status_code == 404


def test_analysis_rejects_invalid_python(client):
    project = create_project(client)
    response = client.post(
        f"/projects/{project['id']}/analyze",
        json={"files": [{"path": "broken.py", "content": "def broken(:\n"}]},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_python"
    assert client.get(f"/projects/{project['id']}/analysis-runs").json() == []


def test_analysis_rejects_too_complex_python(client):
    project = create_project(client)
    response = client.post(
        f"/projects/{project['id']}/analyze",
        json={"files": [{"path": "deep.py", "content": "x = " + "-" * 200_000 + "1"}]},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "invalid_python",
        "path": "deep.py",
        "line": None,
        "column": None,
        "message": "Python source is too complex to parse",
    }
