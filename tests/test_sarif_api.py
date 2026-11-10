import pytest

from app.database import SessionLocal
from app.models import AnalysisRun
from invariant.config import ForbiddenImportRule
from invariant.engine import Source, analyze
from invariant.sarif import to_sarif


def project(client):
    created = client.post("/projects", json={"name": "SARIF"})
    assert created.status_code == 201
    return created.json()["id"]


def rule(client, project_id, **fields):
    response = client.post(
        f"/projects/{project_id}/rules",
        json={"key": "no-os", "name": "No os", "forbidden_imports": ["os"], **fields},
    )
    assert response.status_code == 201
    return response.json()


def run(client, project_id, content, path="src/é #%.py"):
    response = client.post(
        f"/projects/{project_id}/analyze",
        json={"files": [{"path": path, "content": content}]},
    )
    assert response.status_code == 200
    return response.json()


def test_sarif_api_matches_cli_renderer_and_is_downloadable(client):
    project_id = project(client)
    rule(client, project_id, severity="warning")
    content = 'x = "😀"; import os\n'
    recorded = run(client, project_id, content)
    response = client.get(f"/analysis-runs/{recorded['id']}/sarif")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/sarif+json"
    assert response.headers["Content-Disposition"] == (
        f'attachment; filename="invariant-run-{recorded["id"]}.sarif"'
    )
    [export] = response.json()["runs"]
    rules = [
        ForbiddenImportRule(
            key="no-os",
            name="No os",
            rule_type="forbidden_import",
            forbidden_imports=["os"],
            severity="warning",
        )
    ]
    expected = to_sarif(analyze([Source("src/é #%.py", content)], rules), rules)[
        "runs"
    ][0]
    assert export["results"] == expected["results"]
    assert export["tool"] == expected["tool"]
    assert export["properties"]["summary"] == recorded["summary"]
    assert export["properties"]["outcome"] == "pass"
    assert export["properties"]["config_hash"] == recorded["config_hash"]
    assert export["properties"]["analysis_run_id"] == recorded["id"]
    location = export["results"][0]["locations"][0]["physicalLocation"]
    assert location["artifactLocation"]["uri"] == "src/%C3%A9%20%23%25.py"
    assert location["region"] == {"startLine": 1, "startColumn": 10}


def test_sarif_uses_historical_snapshot_and_producer_version_after_rule_deletion(
    client,
):
    project_id = project(client)
    created_rule = rule(client, project_id)
    recorded = run(client, project_id, "import os\n")
    with SessionLocal() as db:
        db.get(AnalysisRun, recorded["id"]).engine_version = "1.0.0"
        db.commit()
    url = f"/analysis-runs/{recorded['id']}/sarif"
    before = client.get(url).json()
    rule_url = f"/projects/{project_id}/rules/{created_rule['id']}"
    assert (
        client.patch(
            rule_url, json={"name": "Changed", "forbidden_imports": ["sys"]}
        ).status_code
        == 200
    )
    assert client.delete(rule_url).status_code == 204
    assert client.get(url).json() == before
    driver = before["runs"][0]["tool"]["driver"]
    assert driver["version"] == "1.0.0"
    assert driver["rules"][0]["shortDescription"] == {"text": "No os"}


def test_capped_sarif_reports_all_counts_and_warns_about_omitted_results(client):
    project_id = project(client)
    rule(client, project_id)
    recorded = run(client, project_id, "import os\n" * 1005)
    response = client.get(f"/analysis-runs/{recorded['id']}/sarif")
    assert response.status_code == 200
    [export] = response.json()["runs"]
    assert len(export["results"]) == 1000
    assert export["properties"]["summary"]["errors"] == 1005
    assert export["properties"]["omitted_violations"] == {"errors": 5, "warnings": 0}
    assert export["properties"]["diagnostics_truncated"] is True
    [notification] = export["invocations"][0]["toolExecutionNotifications"]
    assert notification["properties"]["code"] == "diagnostics_truncated"
    assert export["properties"]["outcome"] == "fail"


@pytest.mark.parametrize(
    "content, outcome", [("def bad(:\n", "incomplete"), ("x = 1\n", "not_applicable")]
)
def test_sarif_preserves_no_verdict_and_analysis_errors(client, content, outcome):
    project_id = project(client)
    recorded = run(client, project_id, content)
    response = client.get(f"/analysis-runs/{recorded['id']}/sarif")
    assert response.status_code == 200
    [export] = response.json()["runs"]
    assert export["properties"]["outcome"] == outcome
    assert export["results"] == []
    assert export["invocations"][0]["executionSuccessful"] is (outcome != "incomplete")


@pytest.mark.parametrize("status", ["queued", "running", "failed"])
def test_unfinished_or_failed_runs_are_not_exported_as_empty_success(client, status):
    project_id = project(client)
    with SessionLocal() as db:
        stored = AnalysisRun(
            project_id=project_id,
            source="github_pr",
            status=status,
            error_code="github_unavailable" if status == "failed" else None,
            files_analyzed=0,
            rules_evaluated=0,
            violation_count=0,
        )
        db.add(stored)
        db.commit()
        run_id = stored.id
    response = client.get(f"/analysis-runs/{run_id}/sarif")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "analysis_not_completed"


def test_legacy_run_without_snapshot_stays_readable_but_sarif_is_unavailable(client):
    project_id = project(client)
    recorded = run(client, project_id, "x = 1\n")
    with SessionLocal() as db:
        db.get(AnalysisRun, recorded["id"]).config_snapshot = None
        db.commit()
    response = client.get(f"/analysis-runs/{recorded['id']}/sarif")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "sarif_unavailable"
    assert client.get(f"/analysis-runs/{recorded['id']}/result").status_code == 200


def test_sarif_not_found_validation_and_openapi(client):
    assert client.get("/analysis-runs/999/sarif").status_code == 404
    assert client.get("/analysis-runs/2147483648/sarif").status_code == 422
    operation = client.get("/openapi.json").json()["paths"][
        "/analysis-runs/{analysis_run_id}/sarif"
    ]["get"]
    assert "application/sarif+json" in operation["responses"]["200"]["content"]
    assert "409" in operation["responses"]
