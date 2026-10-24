"""Numbers the database cannot hold are invalid requests, not server errors.

IDs and pull request numbers are PostgreSQL integers. A larger number in a
path, a query or a body reached the database and came back as a 500.
"""

import hashlib
import hmac
import json

import pytest

TOO_LARGE = 2**31


@pytest.fixture
def project(client) -> dict:
    response = client.post(
        "/projects",
        json={"name": "range", "github_owner": "octo", "github_repo": "demo"},
    )
    assert response.status_code == 201
    return response.json()


@pytest.mark.parametrize(
    "path",
    [
        f"/projects/{TOO_LARGE}",
        f"/projects/{2**63}",
        f"/projects/{TOO_LARGE}/rules",
        f"/projects/1/rules/{TOO_LARGE}",
        f"/projects/{TOO_LARGE}/stats",
        f"/projects/{TOO_LARGE}/config",
        f"/analysis-runs/{TOO_LARGE}",
        f"/analysis-runs/{TOO_LARGE}/result",
        f"/analysis-runs/{TOO_LARGE}/diagnostics",
    ],
)
def test_an_id_out_of_range_is_a_validation_error(client, project, path):
    response = client.get(path)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_an_id_in_range_that_does_not_exist_is_still_not_found(client):
    response = client.get(f"/projects/{TOO_LARGE - 1}")

    assert response.status_code == 404


@pytest.mark.parametrize("params", [{"pull_number": TOO_LARGE}, {"offset": 2**63}])
def test_a_filter_out_of_range_is_a_validation_error(client, project, params):
    response = client.get(f"/projects/{project['id']}/analysis-runs", params=params)

    assert response.status_code == 422


def test_a_pull_number_out_of_range_is_not_queued(client, project):
    response = client.post(
        f"/projects/{project['id']}/analyze/github", json={"pull_number": TOO_LARGE}
    )

    assert response.status_code == 422
    runs = client.get(f"/projects/{project['id']}/analysis-runs").json()
    assert runs == []


def test_a_webhook_pull_number_out_of_range_is_an_invalid_payload(
    client, project, monkeypatch
):
    secret = "range-secret"
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)
    body = json.dumps(
        {
            "action": "opened",
            "repository": {"name": "demo", "owner": {"login": "octo"}},
            "pull_request": {"number": TOO_LARGE},
        }
    ).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    response = client.post(
        "/integrations/github/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "range-1",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_webhook_payload"
