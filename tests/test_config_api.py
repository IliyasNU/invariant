import app.api
from app.analysis import rule_columns
from app.config_service import apply_import
from app.models import Rule
from invariant.config import parse_config

CONFIG = """
version = 1

[project]
source_roots = ["src"]
exclude_paths = ["tests/"]

[[rules]]
key = "domain-isolation"
name = "Domain isolation"
rule_type = "forbidden_import"
source_pattern = "app.domain.*"
forbidden_imports = ["app.infrastructure"]

[[rules]]
key = "absolute"
rule_type = "no_relative_import"
severity = "warning"
"""


def create_project(client, name="Payments"):
    response = client.post("/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()


def import_config(client, project_id, content, **options):
    return client.post(
        f"/projects/{project_id}/config/import", json={"content": content, **options}
    )


def rules(client, project_id):
    return client.get(f"/projects/{project_id}/rules").json()


def test_import_creates_rules_and_project_settings(client):
    project = create_project(client)

    response = import_config(client, project["id"], CONFIG)

    assert response.status_code == 200
    plan = response.json()
    assert (plan["applied"], plan["created"], plan["updated"], plan["deleted"]) == (
        True,
        ["domain-isolation", "absolute"],
        [],
        [],
    )
    assert {change["field"] for change in plan["project_changes"]} == {
        "source_roots",
        "exclude_paths",
    }
    stored = client.get(f"/projects/{project['id']}").json()
    assert (stored["source_roots"], stored["exclude_paths"]) == (["src"], ["tests/**"])
    assert [(rule["key"], rule["name"]) for rule in rules(client, project["id"])] == [
        ("domain-isolation", "Domain isolation"),
        ("absolute", "absolute"),
    ]


def test_export_is_a_toml_file_that_reimports_without_changes(client):
    project = create_project(client)
    import_config(client, project["id"], CONFIG)

    exported = client.get(f"/projects/{project['id']}/config")

    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/toml")
    config = parse_config(exported.text)
    assert [rule.key for rule in config.rules] == ["domain-isolation", "absolute"]
    assert config.project.source_roots == ["src"]
    other = create_project(client, name="Copy")
    assert import_config(client, other["id"], exported.text).status_code == 200
    assert client.get(f"/projects/{other['id']}/config").text == exported.text
    again = import_config(client, project["id"], exported.text).json()
    assert (again["created"], again["updated"], again["project_changes"]) == (
        [],
        [],
        [],
    )
    assert again["unchanged"] == ["domain-isolation", "absolute"]


def test_forbidden_call_rules_round_trip_through_the_file(client):
    project = create_project(client)
    content = (
        'version = 1\n[[rules]]\nkey = "no-shell"\nrule_type = "forbidden_call"\n'
        'forbidden_calls = ["os.system", "subprocess"]\n'
    )
    assert import_config(client, project["id"], content).status_code == 200

    exported = client.get(f"/projects/{project['id']}/config").text

    [rule] = parse_config(exported).rules
    assert (rule.rule_type, rule.forbidden_calls) == (
        "forbidden_call",
        ["os.system", "subprocess"],
    )
    assert rules(client, project["id"])[0]["forbidden_calls"] == [
        "os.system",
        "subprocess",
    ]
    again = import_config(client, project["id"], exported).json()
    assert (again["created"], again["updated"], again["unchanged"]) == (
        [],
        [],
        ["no-shell"],
    )


def test_dry_run_reports_changes_without_applying_them(client):
    project = create_project(client)
    before = client.get(f"/projects/{project['id']}").json()

    plan = import_config(client, project["id"], CONFIG, dry_run=True).json()

    assert (plan["dry_run"], plan["applied"]) == (True, False)
    assert plan["created"] == ["domain-isolation", "absolute"]
    assert rules(client, project["id"]) == []
    assert client.get(f"/projects/{project['id']}").json() == before


def test_merge_updates_by_key_and_keeps_other_rules(client):
    project = create_project(client)
    legacy = client.post(
        f"/projects/{project['id']}/rules",
        json={"name": "Legacy", "forbidden_imports": ["fastapi"]},
    ).json()
    import_config(client, project["id"], CONFIG)
    changed = CONFIG.replace('severity = "warning"', 'severity = "error"')

    plan = import_config(client, project["id"], changed).json()

    assert (plan["updated"], plan["unchanged"], plan["deleted"]) == (
        ["absolute"],
        ["domain-isolation"],
        [],
    )
    by_key = {rule["key"]: rule for rule in rules(client, project["id"])}
    assert by_key["absolute"]["severity"] == "error"
    assert by_key["legacy"]["id"] == legacy["id"]


def test_replace_deletes_rules_missing_from_the_file_but_keeps_history(client):
    project = create_project(client)
    legacy = client.post(
        f"/projects/{project['id']}/rules",
        json={"name": "Legacy", "forbidden_imports": ["fastapi"]},
    ).json()
    run = client.post(
        f"/projects/{project['id']}/analyze",
        json={"files": [{"path": "app/x.py", "content": "import fastapi\n"}]},
    ).json()

    plan = import_config(client, project["id"], CONFIG, mode="replace").json()

    assert plan["deleted"] == ["legacy"]
    assert [rule["key"] for rule in rules(client, project["id"])] == [
        "domain-isolation",
        "absolute",
    ]
    [violation] = client.get(f"/analysis-runs/{run['id']}").json()["violations"]
    assert (violation["rule_id"], violation["rule_key"], violation["rule_name"]) == (
        None,
        legacy["key"],
        "Legacy",
    )


def test_invalid_files_are_rejected_with_locations(client):
    project = create_project(client)

    syntax = import_config(client, project["id"], "version = 1\n[[rules]\n")
    field = import_config(
        client,
        project["id"],
        'version = 1\n[[rules]]\nkey = "size"\nrule_type = "max_file_lines"\nmax_lines = 0\n',
    )

    for response in (syntax, field):
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_config"
    assert syntax.json()["detail"]["problems"][0]["location"].startswith("line 2")
    assert field.json()["detail"]["problems"] == [
        {
            "location": "rules[0].max_lines",
            "message": "Input should be greater than or equal to 1",
        }
    ]
    assert rules(client, project["id"]) == []


def test_import_rejects_a_name_another_rule_keeps(client):
    project = create_project(client)
    client.post(
        f"/projects/{project['id']}/rules",
        json={"name": "Domain isolation", "key": "kept", "forbidden_imports": ["a"]},
    )

    response = import_config(client, project["id"], CONFIG)

    assert response.status_code == 422
    [problem] = response.json()["detail"]["problems"]
    assert problem["location"] == "rules[0].name"
    assert "already used by rule 'kept'" in problem["message"]
    assert [rule["key"] for rule in rules(client, project["id"])] == ["kept"]


def test_import_can_swap_rule_names(client):
    project = create_project(client)
    first = (
        'version = 1\n[[rules]]\nkey = "a"\nname = "First"\n'
        'rule_type = "no_relative_import"\n'
        '[[rules]]\nkey = "b"\nname = "Second"\nrule_type = "no_relative_import"\n'
    )
    import_config(client, project["id"], first)

    swapped = first.replace('"First"', '"tmp"').replace('"Second"', '"First"')
    swapped = swapped.replace('"tmp"', '"Second"')
    response = import_config(client, project["id"], swapped)

    assert response.status_code == 200
    assert response.json()["updated"] == ["a", "b"]
    assert [(rule["key"], rule["name"]) for rule in rules(client, project["id"])] == [
        ("a", "Second"),
        ("b", "First"),
    ]


def test_config_endpoints_need_an_existing_project(client):
    assert client.get("/projects/999/config").status_code == 404
    assert import_config(client, 999, CONFIG).status_code == 404


def test_a_failed_import_leaves_nothing_behind(client, monkeypatch):
    project = create_project(client)

    def apply_then_conflict(db, project_row, plan):
        apply_import(db, project_row, plan)
        # A row the database rejects at commit time, after everything is staged.
        duplicate = rule_columns(plan.created[0])
        db.add(Rule(project_id=project_row.id, **duplicate))

    monkeypatch.setattr(app.api, "apply_import", apply_then_conflict)
    response = import_config(client, project["id"], CONFIG)

    assert response.status_code == 409
    assert rules(client, project["id"]) == []
    stored = client.get(f"/projects/{project['id']}").json()
    assert (stored["source_roots"], stored["exclude_paths"]) == (["."], [])
