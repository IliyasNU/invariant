import hashlib
import hmac
import json

import httpx
import pytest

from app.config import Settings
from app.github import (
    MAX_GITHUB_FILE_BYTES,
    ChangedPythonFiles,
    GitHubAPIError,
    GitHubClient,
    PullRequestMetadata,
    SkippedFile,
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
        self.comments: list[str] = []
        self.error: GitHubAPIError | None = None
        self.comment_error: GitHubAPIError | None = None
        self.files = [
            SourceFile(
                path="app/domain/service.py",
                content="from app.infrastructure.db import engine\n",
            )
        ]
        self.skipped: list[SkippedFile] = []
        self.truncated = False
        self.ignored = 0

    def get_pull_request(
        self, owner: str, repo: str, pull_number: int
    ) -> PullRequestMetadata:
        self.pull_calls += 1
        if self.error:
            raise self.error
        assert (owner, repo) == ("iliyasnu", "invariant")
        return PullRequestMetadata(number=pull_number, head_sha="abc123")

    def get_changed_python_files(
        self, owner: str, repo: str, pull_request: PullRequestMetadata
    ) -> ChangedPythonFiles:
        assert (owner, repo) == ("iliyasnu", "invariant")
        assert pull_request.head_sha == "abc123"
        assert pull_request.number >= 1
        return ChangedPythonFiles(
            files=self.files,
            skipped=self.skipped,
            truncated=self.truncated,
            ignored=self.ignored,
        )

    def post_pull_request_comment(
        self, owner: str, repo: str, pull_number: int, body: str
    ) -> None:
        self.comment_calls += 1
        if self.comment_error:
            raise self.comment_error
        self.comments.append(body)


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
    create_rule(client, project["id"])
    github.comment_enabled = True
    github.comment_error = GitHubAPIError(502, "github_api_error", "Failed")

    response = client.post(
        f"/projects/{project['id']}/analyze/github", json={"pull_number": 12}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert github.comment_calls == 1


@pytest.mark.parametrize(
    ("skipped", "truncated"),
    [
        ([SkippedFile("app/domain/big.py", "too_large")], False),
        ([SkippedFile("app/domain/latin.py", "undecodable")], False),
        ([SkippedFile("app\\..\\x.py", "invalid_path")], False),
        ([], True),
    ],
)
def test_incomplete_github_analysis_records_no_pass(client, github, skipped, truncated):
    project = create_github_project(client)
    create_rule(client, project["id"])
    github.comment_enabled = True
    github.files = []
    github.skipped = skipped
    github.truncated = truncated

    response = client.post(
        f"/projects/{project['id']}/analyze/github", json={"pull_number": 12}
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "analysis_incomplete"
    assert detail["skipped"] == [
        {"path": item.path, "reason": item.reason} for item in skipped
    ]
    assert detail["file_list_truncated"] is truncated
    assert client.get(f"/projects/{project['id']}/analysis-runs").json() == []
    assert github.comment_calls == 0


def test_github_pr_without_python_files_is_not_applicable(client, github):
    project = create_github_project(client)
    create_rule(client, project["id"])
    github.comment_enabled = True
    github.files = []
    github.ignored = 3

    response = client.post(
        f"/projects/{project['id']}/analyze/github", json={"pull_number": 12}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "not_applicable"
    assert response.json()["detail"]["ignored_files"] == 3
    assert client.get(f"/projects/{project['id']}/analysis-runs").json() == []
    assert github.comment_calls == 0


def test_webhook_accepts_not_applicable_pull_request_without_run(
    client, github, monkeypatch
):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    project = create_github_project(client)
    github.comment_enabled = True
    github.files = []
    body = json.dumps(pull_request_payload()).encode()

    response = client.post(
        "/integrations/github/webhook", content=body, headers=signed_headers(body)
    )

    assert response.status_code == 202
    assert response.json()["status"] == "not_applicable"
    assert client.get(f"/projects/{project['id']}/analysis-runs").json() == []
    assert github.comment_calls == 0


def test_webhook_rejects_incomplete_analysis_without_run(client, github, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    project = create_github_project(client)
    github.comment_enabled = True
    github.skipped = [SkippedFile("app/domain/big.py", "too_large")]
    body = json.dumps(pull_request_payload()).encode()

    response = client.post(
        "/integrations/github/webhook", content=body, headers=signed_headers(body)
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "analysis_incomplete"
    assert client.get(f"/projects/{project['id']}/analysis-runs").json() == []
    assert github.comment_calls == 0


def valid_event(**changes) -> dict:
    return {**pull_request_payload(), **changes}


@pytest.mark.parametrize(
    "payload",
    [
        {"action": []},
        {"action": {"opened": True}},
        {"pull_request": {"number": 12}},
        valid_event(repository={"name": [], "owner": {"login": "IliyasNU"}}),
        valid_event(repository={"name": "invariant", "owner": "IliyasNU"}),
        valid_event(repository={"name": "invariant", "owner": {"login": ""}}),
        valid_event(pull_request={"number": "12"}),
        valid_event(pull_request={"number": True}),
        valid_event(pull_request={"number": 0}),
        valid_event(pull_request=[]),
    ],
)
def test_webhook_rejects_malformed_signed_payloads(
    client, github, monkeypatch, payload
):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    project = create_github_project(client)
    body = json.dumps(payload).encode()

    response = client.post(
        "/integrations/github/webhook", content=body, headers=signed_headers(body)
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_webhook_payload"
    assert github.pull_calls == 0
    assert client.get(f"/projects/{project['id']}/analysis-runs").json() == []


def test_webhook_rejects_deeply_nested_json(client, github, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    body = b"[" * 100_000 + b"]" * 100_000

    response = client.post(
        "/integrations/github/webhook", content=body, headers=signed_headers(body)
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_webhook_payload"


def test_webhook_checks_signature_before_payload_types(client, github, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    headers = signed_headers(b"{}")
    body = json.dumps({"action": []}).encode()

    response = client.post(
        "/integrations/github/webhook", content=body, headers=headers
    )

    assert response.status_code == 401


def test_excluded_changes_are_not_applicable_rather_than_a_pass(
    client, github, monkeypatch
):
    project = create_github_project(client)
    create_rule(client, project["id"])
    client.patch(f"/projects/{project['id']}", json={"exclude_paths": ["app/"]})
    github.comment_enabled = True

    manual = client.post(
        f"/projects/{project['id']}/analyze/github", json={"pull_number": 12}
    )
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    body = json.dumps(pull_request_payload()).encode()
    webhook = client.post(
        "/integrations/github/webhook", content=body, headers=signed_headers(body)
    )

    assert manual.status_code == 422
    assert manual.json()["detail"]["code"] == "not_applicable"
    assert "No enabled rule applies" in manual.json()["detail"]["message"]
    assert (webhook.status_code, webhook.json()["status"]) == (202, "not_applicable")
    assert client.get(f"/projects/{project['id']}/analysis-runs").json() == []
    assert github.comment_calls == 0


def test_pr_comment_lists_warnings_on_a_pass(client, github):
    project = create_github_project(client)
    response = client.post(
        f"/projects/{project['id']}/rules",
        json={
            "name": "Domain isolation",
            "source_pattern": "app.domain.*",
            "forbidden_imports": ["app.infrastructure"],
            "severity": "warning",
        },
    )
    assert response.status_code == 201
    github.comment_enabled = True

    result = client.post(
        f"/projects/{project['id']}/analyze/github", json={"pull_number": 12}
    ).json()

    assert (result["passed"], result["violation_count"]) == (True, 1)
    [comment] = github.comments
    assert comment.startswith("Invariant: PASS with 1 warning(s):")
    assert (
        "app/domain/service.py:1: warning: app.domain.service must not import"
        in comment
    )


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


def mock_github(handler) -> GitHubClient:
    settings = Settings(_env_file=None, github_api_url="https://api.github.test")
    return GitHubClient(settings, transport=httpx.MockTransport(handler))


def test_github_client_fetches_only_live_python_files():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(
                200,
                json={"number": 7, "head": {"sha": "deadbeef"}, "changed_files": 3},
            )
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

    with mock_github(handler) as github:
        pull = github.get_pull_request("owner", "repo", 7)
        changed = github.get_changed_python_files("owner", "repo", pull)

    assert pull.changed_files == 3
    assert changed.files == [SourceFile(path="app/service.py", content="import json\n")]
    assert changed.ignored == 2
    assert changed.complete


def test_github_client_reports_files_it_could_not_analyze():
    downloaded: list[str] = []
    contents = {
        "app/big.py": b"import app.infrastructure\n" + b"#" * MAX_GITHUB_FILE_BYTES,
        "app/latin.py": b"\xff\xfeimport app.infrastructure\n",
        "app/ok.py": b"import json\n",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7/files"):
            return httpx.Response(
                200,
                json=[
                    {"filename": "app\\..\\escape.py", "status": "added"},
                    {"filename": "app/big.py", "status": "added"},
                    {"filename": "app/latin.py", "status": "modified"},
                    {"filename": "app/ok.py", "status": "renamed"},
                ],
            )
        path = request.url.path.split("/contents/", 1)[1]
        downloaded.append(path)
        return httpx.Response(200, content=contents[path])

    pull = PullRequestMetadata(number=7, head_sha="deadbeef", changed_files=4)
    with mock_github(handler) as github:
        changed = github.get_changed_python_files("owner", "repo", pull)

    assert changed.skipped == [
        SkippedFile("app\\..\\escape.py", "invalid_path"),
        SkippedFile("app/big.py", "too_large"),
        SkippedFile("app/latin.py", "undecodable"),
    ]
    assert changed.files == [SourceFile(path="app/ok.py", content="import json\n")]
    assert not changed.complete
    assert downloaded == ["app/big.py", "app/latin.py", "app/ok.py"]


@pytest.mark.parametrize(
    ("listed", "changed_files", "truncated"),
    [
        (3000, 3001, True),
        (3000, None, True),
        (3000, 3000, False),
        (40, 41, True),
        (40, None, False),
    ],
)
def test_github_client_detects_truncated_file_list(listed, changed_files, truncated):
    entries = [{"filename": f"docs/{i}.md", "status": "added"} for i in range(listed)]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json=entries[(page - 1) * 100 : page * 100])

    pull = PullRequestMetadata(number=7, head_sha="sha", changed_files=changed_files)
    with mock_github(handler) as github:
        changed = github.get_changed_python_files("owner", "repo", pull)

    assert changed.truncated is truncated
    assert changed.ignored == listed


def test_github_client_downloads_nothing_from_a_truncated_list():
    entries = [{"filename": f"app/m{i}.py", "status": "added"} for i in range(100)]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/contents/" not in request.url.path
        page = int(request.url.params["page"])
        return httpx.Response(200, json=entries if page == 1 else [])

    pull = PullRequestMetadata(number=7, head_sha="sha", changed_files=101)
    with mock_github(handler) as github:
        changed = github.get_changed_python_files("owner", "repo", pull)

    assert changed.truncated
    assert changed.files == []


@pytest.mark.parametrize(
    "payload", [[{"status": "added"}], [{"filename": 7}], ["app/x.py"], {}]
)
def test_github_client_rejects_malformed_file_lists(payload):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    pull = PullRequestMetadata(number=7, head_sha="sha")
    with mock_github(handler) as github, pytest.raises(GitHubAPIError) as exc_info:
        github.get_changed_python_files("owner", "repo", pull)

    assert exc_info.value.code == "github_invalid_response"


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
