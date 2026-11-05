import pytest


@pytest.mark.parametrize("number", [0, -1, 2**31, 10**20])
@pytest.mark.parametrize(
    "path",
    [
        "/projects/{number}",
        "/projects/1/rules/{number}",
        "/analysis-runs/{number}",
        "/analysis-runs/{number}/result",
        "/analysis-runs/{number}/diagnostics",
    ],
)
def test_ids_outside_database_range_are_validation_errors(client, path, number):
    response = client.get(path.format(number=number))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("number", [2**31, 10**20])
def test_large_query_and_pull_numbers_never_reach_storage(client, number):
    project = client.post(
        "/projects",
        json={"name": "Bounds", "github_owner": "owner", "github_repo": "repo"},
    ).json()
    base = f"/projects/{project['id']}"
    assert client.get("/projects", params={"offset": number}).status_code == 422
    assert (
        client.get(base + "/analysis-runs", params={"pull_number": number}).status_code
        == 422
    )
    assert (
        client.post(base + "/analyze/github", json={"pull_number": number}).status_code
        == 422
    )
    assert (
        client.post(
            f"/projects/{number}/analyze/github", json={"pull_number": 1}
        ).status_code
        == 422
    )
    assert client.get(base + "/analysis-runs").json() == []


@pytest.mark.parametrize("field", ["github_owner", "github_repo"])
@pytest.mark.parametrize("value", [".", ".."])
def test_github_names_cannot_be_navigation_segments(client, field, value):
    body = {"name": "Repo", "github_owner": "owner", "github_repo": "repo"}
    body[field] = value
    assert client.post("/projects", json=body).status_code == 422
    project = client.post("/projects", json={"name": "Existing"}).json()
    assert (
        client.patch(
            f"/projects/{project['id']}",
            json={
                "github_owner": body["github_owner"],
                "github_repo": body["github_repo"],
            },
        ).status_code
        == 422
    )


def test_rule_filters_combine_and_count_before_pagination(client):
    project = client.post("/projects", json={"name": "Rules"}).json()
    other = client.post("/projects", json={"name": "Other"}).json()
    url = f"/projects/{project['id']}/rules"
    payloads = [
        {"key": "imports", "forbidden_imports": ["os"]},
        {
            "key": "relative-one",
            "rule_type": "no_relative_import",
            "severity": "warning",
        },
        {
            "key": "relative-two",
            "rule_type": "no_relative_import",
            "severity": "warning",
        },
        {
            "key": "disabled",
            "rule_type": "no_relative_import",
            "severity": "warning",
            "enabled": False,
        },
    ]
    rules = [client.post(url, json=payload).json() for payload in payloads]
    client.post(f"/projects/{other['id']}/rules", json=payloads[1])
    filters = {
        "enabled": True,
        "severity": "warning",
        "rule_type": "no_relative_import",
    }
    page = client.get(url, params={**filters, "limit": 1, "offset": 1})
    assert page.status_code == 200
    assert page.json() == [rules[2]]
    assert page.headers["X-Total-Count"] == "2"
    assert client.get(url, params={"enabled": False}).json() == [rules[3]]
    empty = client.get(url, params={"rule_type": "forbidden_call"})
    assert empty.json() == []
    assert empty.headers["X-Total-Count"] == "0"
    assert client.get(url).json() == rules
    for params in (
        {"enabled": "maybe"},
        {"severity": "info"},
        {"rule_type": "unknown"},
    ):
        assert client.get(url, params=params).status_code == 422
