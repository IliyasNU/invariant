import hashlib
import hmac
import json

import httpx
import pytest

from app.config import Settings
from app.github import (
    GitHubAPIError,
    GitHubClient,
    PullRequestMetadata,
    get_github_client,
    verify_webhook_signature,
)
from app.main import app
from app.schemas import SourceFile

WEBHOOK_SECRET = "unit-test-webhook-secret"


class FakeGitHubClient:
    def __init__(self) -> None:
        self.comment_enabled = False
        self.pull_calls = 0
        self.comment_calls = 0
        self.error: GitHubAPIError | None = None
        self.comment_error: GitHubAPIError | None = None
        self.files = [
            SourceFile(
                path="app/domain/service.py",
                content="from app.infrastructure.db import engine\n",
            )
        ]

    def get_pull_request(
        self, owner: str, repo: str, pull_number: int
    ) -> PullRequestMetadata:
        self.pull_calls += 1
        if self.error:
            raise self.error
        assert (owner, repo) == ("iliyasnu", "invariant")
        return PullRequestMetadata(number=pull_number, head_sha="abc123")

    def get_changed_python_files(
        self, owner: str, repo: str, pull_number: int, head_sha: str
    ) -> list[SourceFile]:
        assert (owner, repo, head_sha) == ("iliyasnu", "invariant", "abc123")
        assert pull_number >= 1
        return self.files

    def post_pull_request_comment(
        self, owner: str, repo: str, pull_number: int, body: str
    ) -> None:
        self.comment_calls += 1
        if self.comment_error:
            raise self.comment_error


@pytest.fixture
def github() -> FakeGitHubClient:
    fake = FakeGitHubClient()
    app.dependency_overrides[get_github_client] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_github_client, None)


def create_github_project(client):
    response = client.post(
        "/projects",
        json={
            "name": "Invariant",
            "github_owner": "IliyasNU",
            "github_repo": "invariant",
        },
    )
    assert response.status_code == 201
    return response.json()


def create_rule(client, project_id):
    response = client.post(
        f"/projects/{project_id}/rules",
        json={
            "name": "Domain isolation",
            "source_pattern": "app.domain.*",
            "forbidden_imports": ["app.infrastructure"],
        },
    )
    assert response.status_code == 201


def signed_headers(body: bytes, *, delivery: str = "delivery-1") -> dict[str, str]:
    signature = (
        "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    )
    return {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }


def pull_request_payload(action: str = "opened") -> dict:
    return {
        "action": action,
        "repository": {
            "name": "Invariant",
            "owner": {"login": "IliyasNU"},
        },
        "pull_request": {"number": 12},
    }


def test_manual_github_analysis_persists_result(client, github):
    project = create_github_project(client)
    create_rule(client, project["id"])

    response = client.post(
        f"/projects/{project['id']}/analyze/github", json={"pull_number": 12}
    )

    assert response.status_code == 200
    result = response.json()
    assert result["source"] == "github_pr"
    assert result["pull_number"] == 12
    assert result["repository_owner"] == "iliyasnu"
    assert result["violation_count"] == 1
    assert result["passed"] is False
    assert client.get(f"/analysis-runs/{result['id']}").json() == result


def test_manual_github_analysis_requires_project_configuration(client, github):
    project = client.post("/projects", json={"name": "Local"}).json()

    response = client.post(
        f"/projects/{project['id']}/analyze/github", json={"pull_number": 1}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "github_not_configured"
    assert github.pull_calls == 0


def test_github_api_error_is_structured(client, github):
    project = create_github_project(client)
    github.error = GitHubAPIError(404, "github_not_found", "Not found")

    response = client.post(
        f"/projects/{project['id']}/analyze/github", json={"pull_number": 999}
    )

    assert response.status_code == 404
    assert response.json() == {
        "detail": {"code": "github_not_found", "message": "Not found"}
    }


def test_github_analysis_rejects_invalid_python_without_run(client, github):
    project = create_github_project(client)
    github.files = [SourceFile(path="broken.py", content="def broken(:\n")]

    response = client.post(
        f"/projects/{project['id']}/analyze/github", json={"pull_number": 12}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_python"
    assert client.get(f"/projects/{project['id']}/analysis-runs").json() == []


def test_optional_comment_failure_does_not_fail_analysis(client, github):
    project = create_github_project(client)
    github.comment_enabled = True
    github.comment_error = GitHubAPIError(502, "github_api_error", "Failed")

    response = client.post(
        f"/projects/{project['id']}/analyze/github", json={"pull_number": 12}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert github.comment_calls == 1


def test_webhook_signature_validation():
    body = b'{"action":"opened"}'
    signature = (
        "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    )

    assert verify_webhook_signature(body, signature, WEBHOOK_SECRET)
    assert not verify_webhook_signature(body + b" ", signature, WEBHOOK_SECRET)
    assert not verify_webhook_signature(body, "sha1=bad", WEBHOOK_SECRET)
    assert not verify_webhook_signature(body, None, WEBHOOK_SECRET)


def test_webhook_rejects_invalid_signature(client, github, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)

    response = client.post(
        "/integrations/github/webhook",
        content=b"{}",
        headers={
            "X-Hub-Signature-256": "sha256=invalid",
            "X-GitHub-Event": "pull_request",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_github_signature"


def test_webhook_requires_configured_secret(client, github, monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)

    response = client.post(
        "/integrations/github/webhook",
        content=b"{}",
        headers={"X-GitHub-Event": "pull_request"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "github_webhook_not_configured"


def test_webhook_ignores_irrelevant_event(client, github, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    body = b"{}"
    headers = signed_headers(body)
    headers["X-GitHub-Event"] = "push"

    response = client.post(
        "/integrations/github/webhook", content=body, headers=headers
    )

    assert response.status_code == 202
    assert response.json()["status"] == "ignored"
    assert github.pull_calls == 0


def test_webhook_ignores_irrelevant_pull_request_action(client, github, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    body = json.dumps(pull_request_payload("closed")).encode()

    response = client.post(
        "/integrations/github/webhook", content=body, headers=signed_headers(body)
    )

    assert response.status_code == 202
    assert response.json()["status"] == "ignored"
    assert github.pull_calls == 0


def test_webhook_analyzes_and_deduplicates_delivery(client, github, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    project = create_github_project(client)
    create_rule(client, project["id"])
    body = json.dumps(pull_request_payload()).encode()
    headers = signed_headers(body, delivery="same-delivery")

    first = client.post("/integrations/github/webhook", content=body, headers=headers)
    second = client.post("/integrations/github/webhook", content=body, headers=headers)

    assert first.status_code == 200
    assert first.json()["status"] == "analyzed"
    assert second.status_code == 200
    assert second.json() == {
        "status": "duplicate",
        "analysis_run_id": first.json()["analysis_run_id"],
        "detail": None,
    }
    assert github.pull_calls == 1
    history = client.get(f"/projects/{project['id']}/analysis-runs").json()
    assert len(history) == 1
    assert history[0]["source"] == "github_webhook"


def test_github_client_fetches_only_live_python_files():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json={"number": 7, "head": {"sha": "deadbeef"}})
        if request.url.path.endswith("/pulls/7/files"):
            return httpx.Response(
                200,
                json=[
                    {"filename": "app/service.py", "status": "modified"},
                    {"filename": "app/deleted.py", "status": "removed"},
                    {"filename": "README.md", "status": "modified"},
                ],
            )
        if request.url.path.endswith("/contents/app/service.py"):
            assert request.url.params["ref"] == "deadbeef"
            return httpx.Response(200, content=b"import json\n")
        raise AssertionError(f"Unexpected request: {request.url}")

    settings = Settings(_env_file=None, github_api_url="https://api.github.test")
    with GitHubClient(settings, transport=httpx.MockTransport(handler)) as github:
        pull = github.get_pull_request("owner", "repo", 7)
        files = github.get_changed_python_files(
            "owner", "repo", pull.number, pull.head_sha
        )

    assert files == [SourceFile(path="app/service.py", content="import json\n")]


def test_github_client_maps_rate_limit_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"})

    settings = Settings(_env_file=None, github_api_url="https://api.github.test")
    with (
        GitHubClient(settings, transport=httpx.MockTransport(handler)) as github,
        pytest.raises(GitHubAPIError) as exc_info,
    ):
        github.get_pull_request("owner", "repo", 1)

    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "github_rate_limited"
