import pytest


def create_project(client, name="Payments", **fields):
    response = client.post("/projects", json={"name": name, **fields})
    assert response.status_code == 201, response.json()
    return response.json()


def create_rule(client, project_id, **fields):
    response = client.post(f"/projects/{project_id}/rules", json=fields)
    assert response.status_code == 201, response.json()
    return response.json()


def analyze(client, project_id, files: dict[str, str]):
    response = client.post(
        f"/projects/{project_id}/analyze",
        json={
            "files": [{"path": path, "content": text} for path, text in files.items()]
        },
    )
    assert response.status_code == 200, response.json()
    return response.json()


def test_project_settings_are_stored_normalized(client):
    project = create_project(
        client, source_roots=["src/"], exclude_paths=["tests/"], fail_on="warning"
    )

    assert project["source_roots"] == ["src"]
    assert project["exclude_paths"] == ["tests/**"]
    assert project["fail_on"] == "warning"
    assert client.get(f"/projects/{project['id']}").json() == project
    defaults = create_project(client, name="Defaults")
    assert (
        defaults["source_roots"],
        defaults["exclude_paths"],
        defaults["fail_on"],
    ) == (
        ["."],
        [],
        "error",
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"source_roots": ["src", "src/app"]},
        {"source_roots": []},
        {"source_roots": ["/abs"]},
        {"exclude_paths": ["../outside/**"]},
        {"fail_on": "fatal"},
    ],
)
def test_project_settings_are_validated(client, fields):
    assert client.post("/projects", json={"name": "Bad", **fields}).status_code == 422


def test_project_can_be_renamed_and_reconfigured(client):
    project = create_project(client)
    other = create_project(client, name="Other")
    url = f"/projects/{project['id']}"

    renamed = client.patch(url, json={"name": "Billing", "source_roots": ["src"]})
    assert renamed.status_code == 200
    assert (renamed.json()["name"], renamed.json()["source_roots"]) == (
        "Billing",
        ["src"],
    )
    assert renamed.json()["description"] == project["description"]
    assert client.patch(url, json={"name": other["name"]}).status_code == 409
    for field in ("name", "source_roots", "exclude_paths", "fail_on"):
        assert client.patch(url, json={field: None}).status_code == 422
    assert client.patch("/projects/999", json={"name": "x"}).status_code == 404


def test_deleting_a_project_deletes_its_rules_and_history(client):
    project = create_project(client)
    kept = create_project(client, name="Kept")
    for owner in (project, kept):
        create_rule(client, owner["id"], name="Infra", forbidden_imports=["app.db"])
    run = analyze(client, project["id"], {"app/x.py": "import app.db\n"})
    kept_run = analyze(client, kept["id"], {"app/x.py": "import app.db\n"})

    assert client.delete(f"/projects/{project['id']}").status_code == 204

    assert client.get(f"/projects/{project['id']}").status_code == 404
    assert client.get(f"/analysis-runs/{run['id']}").status_code == 404
    assert client.delete(f"/projects/{project['id']}").status_code == 404
    assert client.get(f"/analysis-runs/{kept_run['id']}").json() == kept_run
    assert len(client.get(f"/projects/{kept['id']}/rules").json()) == 1


def test_rules_of_every_type_can_be_created(client):
    project = create_project(client)
    url = f"/projects/{project['id']}/rules"

    legacy = create_rule(
        client,
        project["id"],
        name="Domain isolation!",
        source_pattern="app.domain.*",
        forbidden_imports=["app.infrastructure"],
    )
    relative = create_rule(
        client, project["id"], key="absolute", rule_type="no_relative_import"
    )
    size = create_rule(
        client,
        project["id"],
        name="Small files",
        rule_type="max_file_lines",
        max_lines=300,
        severity="warning",
        exclude_paths=["migrations/"],
    )

    assert (legacy["key"], legacy["rule_type"], legacy["forbidden_imports"]) == (
        "domain-isolation",
        "forbidden_import",
        ["app.infrastructure"],
    )
    assert (legacy["enabled"], legacy["severity"], legacy["max_lines"]) == (
        True,
        "error",
        None,
    )
    assert (relative["name"], relative["forbidden_imports"]) == ("absolute", None)
    assert (size["key"], size["max_lines"], size["exclude_paths"]) == (
        "small-files",
        300,
        ["migrations/**"],
    )
    assert client.get(url).json() == [legacy, relative, size]


@pytest.mark.parametrize(
    "body",
    [
        {"forbidden_imports": ["app.db"]},
        {"name": "x", "rule_type": "forbidden_everything"},
        {"name": "x", "rule_type": "max_file_lines", "max_lines": 0},
        {"name": "x", "rule_type": "max_file_lines"},
        {"name": "x", "rule_type": "no_relative_import", "forbidden_imports": ["a"]},
        {"name": "x", "key": "Bad Key", "forbidden_imports": ["app.db"]},
        {"name": "x", "forbidden_imports": ["app.db"], "severty": "warning"},
    ],
)
def test_invalid_rules_are_rejected(client, body):
    project = create_project(client)

    response = client.post(f"/projects/{project['id']}/rules", json=body)

    assert response.status_code == 422


def test_rule_keys_and_names_are_unique_in_a_project(client):
    project = create_project(client)
    other = create_project(client, name="Other")
    create_rule(
        client, project["id"], key="infra", name="Infra", forbidden_imports=["a"]
    )
    url = f"/projects/{project['id']}/rules"

    assert (
        client.post(url, json={"key": "infra", "forbidden_imports": ["b"]}).status_code
        == 409
    )
    assert (
        client.post(url, json={"name": "Infra", "forbidden_imports": ["b"]}).status_code
        == 409
    )
    create_rule(client, other["id"], key="infra", name="Infra", forbidden_imports=["a"])


def test_rules_are_only_reachable_through_their_project(client):
    project = create_project(client)
    other = create_project(client, name="Other")
    rule = create_rule(
        client, project["id"], name="Infra", forbidden_imports=["app.db"]
    )

    assert client.get(f"/projects/{project['id']}/rules/{rule['id']}").json() == rule
    foreign = f"/projects/{other['id']}/rules/{rule['id']}"
    assert client.get(foreign).status_code == 404
    assert client.patch(foreign, json={"enabled": False}).status_code == 404
    assert client.delete(foreign).status_code == 404
    assert client.get(f"/projects/{project['id']}/rules/{rule['id']}").json() == rule


def test_rule_patch_merges_and_revalidates(client):
    project = create_project(client)
    rule = create_rule(
        client, project["id"], name="Infra", forbidden_imports=["app.db"]
    )
    url = f"/projects/{project['id']}/rules/{rule['id']}"

    patched = client.patch(
        url, json={"severity": "warning", "exclude_paths": ["legacy/"]}
    )
    assert patched.status_code == 200
    assert (patched.json()["severity"], patched.json()["exclude_paths"]) == (
        "warning",
        ["legacy/**"],
    )
    assert patched.json()["forbidden_imports"] == ["app.db"]
    for body in (
        {"max_lines": 10},
        {"rule_type": "no_relative_import"},
        {"forbidden_imports": []},
        {"forbidden_imports": ["not a module"]},
        {"enabled": None},
    ):
        assert client.patch(url, json=body).status_code == 422, body
    renamed = client.patch(url, json={"key": "infra-boundary", "name": "Boundary"})
    assert (renamed.json()["key"], renamed.json()["name"]) == (
        "infra-boundary",
        "Boundary",
    )


def test_analysis_applies_roots_exclusions_severity_and_enabled(client):
    project = create_project(
        client, source_roots=["src"], exclude_paths=["src/vendor/"]
    )
    create_rule(
        client,
        project["id"],
        name="Domain isolation",
        source_pattern="app.domain.*",
        forbidden_imports=["app.infrastructure"],
    )
    create_rule(
        client,
        project["id"],
        key="absolute",
        rule_type="no_relative_import",
        severity="warning",
    )
    create_rule(
        client,
        project["id"],
        key="size",
        rule_type="max_file_lines",
        max_lines=1,
        enabled=False,
    )
    files = {
        "src/app/domain/c.py": "from ..infrastructure import db\n",
        "src/app/api.py": "from .domain import c\n\nx = 1\n",
        "src/vendor/lib.py": "import app.infrastructure\n",
    }

    run = analyze(client, project["id"], files)

    found = [
        (item["path"], item["code"], item["severity"], item["imported_module"])
        for item in run["violations"]
    ]
    # Sorted by path, line, column, then rule key ("absolute" < "domain-...").
    assert found == [
        ("src/app/api.py", "no_relative_import", "warning", None),
        ("src/app/domain/c.py", "no_relative_import", "warning", None),
        ("src/app/domain/c.py", "forbidden_import", "error", "app.infrastructure"),
    ]
    assert run["passed"] is False
    assert (run["files_analyzed"], run["rules_evaluated"]) == (2, 2)

    clean = analyze(
        client, project["id"], {"src/app/api.py": "from .domain import c\n"}
    )
    assert (
        clean["passed"] is True
    )  # a warning alone does not fail the default threshold
    assert clean["violation_count"] == 1


def test_stored_violations_carry_diagnostic_fields(client):
    project = create_project(client)
    rule = create_rule(
        client,
        project["id"],
        name="Domain isolation",
        source_pattern="app.domain.*",
        forbidden_imports=["app.infrastructure"],
    )

    run = analyze(
        client, project["id"], {"app/domain/a.py": "import app.infrastructure\n"}
    )

    [violation] = client.get(f"/analysis-runs/{run['id']}").json()["violations"]
    assert {key: violation[key] for key in ("kind", "code", "severity", "rule_id")} == {
        "kind": "violation",
        "code": "forbidden_import",
        "severity": "error",
        "rule_id": rule["id"],
    }
    assert (violation["rule_key"], violation["rule_name"]) == (
        "domain-isolation",
        "Domain isolation",
    )
    assert violation["data"] == {
        "imported_module": "app.infrastructure",
        "forbidden": "app.infrastructure",
    }
