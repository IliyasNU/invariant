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
    assert result["files_analyzed"] == 2
    assert result["rules_evaluated"] == 1
    assert [item["imported_module"] for item in result["violations"]] == [
        "app.infrastructure.db",
        "fastapi.routing",
    ]
    assert [item["line"] for item in result["violations"]] == [1, 2]


def test_analysis_rejects_invalid_python(client):
    project = create_project(client)
    response = client.post(
        f"/projects/{project['id']}/analyze",
        json={"files": [{"path": "broken.py", "content": "def broken(:\n"}]},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_python"
