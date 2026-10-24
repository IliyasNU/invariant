import hashlib
import hmac
import json
from typing import Self

import httpx
import pytest

from app.config import Settings
from app.database import SessionLocal
from app.github import (
    ChangedPythonFiles,
    GitHubAPIError,
    GitHubClient,
    PullRequestMetadata,
    SkippedFile,
    verify_webhook_signature,
)
from app.models import AnalysisRun
from app.schemas import SourceFile
from app.worker import Policy, run_worker
from invariant.files import MAX_FILE_BYTES

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
        self.base_sha: str | None = None

    def get_pull_request(
        self, owner: str, repo: str, pull_number: int
    ) -> PullRequestMetadata:
        self.pull_calls += 1
        if self.error:
            raise self.error
        assert (owner, repo) == ("iliyasnu", "invariant")
        return PullRequestMetadata(
            number=pull_number, head_sha="abc123", base_sha=self.base_sha
        )

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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


@pytest.fixture
def github() -> FakeGitHubClient:
    return FakeGitHubClient()


def drain(github: FakeGitHubClient, max_attempts: int = 3) -> int:
    """Let the worker run every queued run that is due, with ``github``."""
    return run_worker(
        github_factory=lambda: github,
        policy=Policy(max_attempts=max_attempts),
        until_idle=True,
    )


def analyze(client, github, project_id, pull_number=12, **policy) -> dict:
    """Queue a manual analysis, let the worker run it, and return the run."""
    response = client.post(
        f"/projects/{project_id}/analyze/github", json={"pull_number": pull_number}
    )
    assert response.status_code == 202, response.json()
    assert response.json()["status"] == "queued"
    drain(github, **policy)
    return client.get(f"/analysis-runs/{response.json()['id']}").json()


def deliver(client, github, body: bytes, **header_options) -> dict:
    """Post a signed webhook delivery, let the worker run it, and return the run."""
    response = client.post(
        "/integrations/github/webhook",
        content=body,
        headers=signed_headers(body, **header_options),
    )
    assert response.status_code == 202, response.json()
    assert response.json()["status"] == "queued"
    drain(github)
    return client.get(f"/analysis-runs/{response.json()['analysis_run_id']}").json()


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


def test_manual_github_analysis_is_queued_then_recorded_by_the_worker(client, github):
    project = create_github_project(client)
    create_rule(client, project["id"])

    queued = client.post(
        f"/projects/{project['id']}/analyze/github", json={"pull_number": 12}
    )

    assert queued.status_code == 202
    accepted = queued.json()
    assert (accepted["status"], accepted["outcome"], accepted["passed"]) == (
        "queued",
        None,
        None,
    )
    assert github.pull_calls == 0  # nothing is fetched until the worker runs
    assert drain(github) == 1
    result = client.get(f"/analysis-runs/{accepted['id']}").json()
    assert (result["status"], result["source"], result["pull_number"]) == (
        "completed",
        "github_pr",
        12,
    )
    assert result["repository_owner"] == "iliyasnu"
    assert result["violation_count"] == 1
    assert result["passed"] is False
    assert drain(github) == 0


def test_manual_github_analysis_requires_project_configuration(client, github):
    project = client.post("/projects", json={"name": "Local"}).json()

    response = client.post(
        f"/projects/{project['id']}/analyze/github", json={"pull_number": 1}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "github_not_configured"
    assert github.pull_calls == 0


@pytest.mark.parametrize(
    "error",
    [
        GitHubAPIError(404, "github_not_found", "Not found"),
        GitHubAPIError(503, "github_unavailable", "GitHub could not be reached"),
        GitHubAPIError(429, "github_rate_limited", "GitHub API rate limit exceeded"),
    ],
)
def test_a_github_failure_is_recorded_as_a_failed_run_not_a_pass(client, github, error):
    project = create_github_project(client)
    create_rule(client, project["id"])
    github.comment_enabled = True
    github.error = error

    run = analyze(client, github, project["id"], pull_number=999, max_attempts=1)

    assert (run["status"], run["outcome"], run["passed"]) == ("failed", None, None)
    assert run["error"] == {"code": error.code, "message": error.message}
    assert (run["pull_number"], run["violations"]) == (999, [])
    assert run["finished_at"] is not None
    assert github.comment_calls == 0


def test_github_analysis_of_invalid_python_is_incomplete(client, github):
    project = create_github_project(client)
    create_rule(client, project["id"])
    github.comment_enabled = True
    github.files = [SourceFile(path="app/domain/broken.py", content="def broken(:\n")]

    run = analyze(client, github, project["id"])

    assert (run["outcome"], run["passed"]) == ("incomplete", None)
    assert [(item["code"], item["path"]) for item in run["violations"]] == [
        ("syntax_error", "app/domain/broken.py")
    ]
    assert github.comment_calls == 0


def test_optional_comment_failure_does_not_fail_analysis(client, github):
    project = create_github_project(client)
    create_rule(client, project["id"])
    github.comment_enabled = True
    github.comment_error = GitHubAPIError(502, "github_api_error", "Failed")

    run = analyze(client, github, project["id"])

    assert (run["status"], run["outcome"]) == ("completed", "fail")
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

    run = analyze(client, github, project["id"])

    assert (run["status"], run["outcome"], run["passed"]) == (
        "completed",
        "incomplete",
        None,
    )
    expected = [(item.reason, item.path) for item in skipped]
    if truncated:
        expected.append(("file_list_truncated", None))
    assert [(item["code"], item["path"]) for item in run["violations"]] == expected
    assert all(item["kind"] == "error" for item in run["violations"])
    assert run["files"]["skipped"] == len(skipped)
    history = client.get(f"/projects/{project['id']}/analysis-runs").json()
    assert [item["id"] for item in history] == [run["id"]]
    assert github.comment_calls == 0


def test_github_pr_without_python_files_is_not_applicable(client, github):
    project = create_github_project(client)
    create_rule(client, project["id"])
    github.comment_enabled = True
    github.files = []
    github.ignored = 3

    run = analyze(client, github, project["id"])

    assert (run["outcome"], run["passed"]) == ("not_applicable", None)
    assert (run["files"]["total"], run["files"]["ignored"]) == (3, 3)
    assert github.comment_calls == 0


def test_webhook_records_a_not_applicable_pull_request(client, github, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    create_github_project(client)
    github.comment_enabled = True
    github.files = []
    body = json.dumps(pull_request_payload()).encode()

    run = deliver(client, github, body)

    assert (run["source"], run["outcome"], run["passed"]) == (
        "github_webhook",
        "not_applicable",
        None,
    )
    assert github.comment_calls == 0


def test_webhook_records_an_incomplete_analysis_without_a_verdict(
    client, github, monkeypatch
):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    create_github_project(client)
    github.comment_enabled = True
    github.skipped = [SkippedFile("app/domain/big.py", "too_large")]
    body = json.dumps(pull_request_payload()).encode()

    run = deliver(client, github, body)

    assert (run["outcome"], run["passed"]) == ("incomplete", None)
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

    manual = analyze(client, github, project["id"])
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    webhook = deliver(client, github, json.dumps(pull_request_payload()).encode())

    assert (manual["outcome"], manual["passed"]) == ("not_applicable", None)
    assert manual["files"]["excluded"] == 1
    assert webhook["outcome"] == "not_applicable"
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

    result = analyze(client, github, project["id"])

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


def test_webhook_queues_and_deduplicates_delivery(client, github, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    project = create_github_project(client)
    create_rule(client, project["id"])
    body = json.dumps(pull_request_payload()).encode()
    headers = signed_headers(body, delivery="same-delivery")

    first = client.post("/integrations/github/webhook", content=body, headers=headers)
    early = client.post("/integrations/github/webhook", content=body, headers=headers)
    drain(github)
    late = client.post("/integrations/github/webhook", content=body, headers=headers)

    assert first.status_code == 202
    run_id = first.json()["analysis_run_id"]
    assert first.json() == {
        "status": "queued",
        "analysis_run_id": run_id,
        "outcome": None,
        "detail": None,
    }
    # A repeated delivery returns the first one's run, finished or not.
    assert (early.status_code, early.json()["analysis_run_id"]) == (200, run_id)
    assert (early.json()["status"], early.json()["outcome"]) == ("duplicate", None)
    assert (late.status_code, late.json()["outcome"]) == (200, "fail")
    assert github.pull_calls == 1
    history = client.get(f"/projects/{project['id']}/analysis-runs").json()
    assert len(history) == 1
    assert history[0]["source"] == "github_webhook"


def test_github_runs_record_what_they_checked(client, github, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    project = create_github_project(client)
    create_rule(client, project["id"])
    github.base_sha = "b" * 40
    body = json.dumps(pull_request_payload()).encode()

    run = deliver(client, github, body, delivery="delivery-42")

    assert (
        run["source"],
        run["repository_owner"],
        run["repository_name"],
        run["pull_number"],
        run["base_sha"],
        run["head_sha"],
    ) == ("github_webhook", "iliyasnu", "invariant", 12, "b" * 40, "abc123")
    assert run["config"]["rules"][0]["key"] == "domain-isolation"
    with SessionLocal() as session:
        stored = session.get(AnalysisRun, run["id"])
        assert stored.github_delivery_id == "delivery-42"


def mock_github(handler) -> GitHubClient:
    settings = Settings(_env_file=None, github_api_url="https://api.github.test")
    return GitHubClient(settings, transport=httpx.MockTransport(handler))


HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"


def test_github_client_fetches_only_live_python_files():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "head": {"sha": HEAD_SHA},
                    "base": {"sha": BASE_SHA},
                    "changed_files": 4,
                },
            )
        if request.url.path.endswith("/pulls/7/files"):
            return httpx.Response(
                200,
                json=[
                    {"filename": "app/service.py", "status": "modified"},
                    {"filename": "app/deleted.py", "status": "removed"},
                    {"filename": "README.md", "status": "modified"},
                    {
                        "filename": "app/new_name.py",
                        "previous_filename": "app/old_name.py",
                        "status": "renamed",
                    },
                ],
            )
        if request.url.path.endswith("/contents/app/service.py"):
            assert request.url.params["ref"] == HEAD_SHA
            return httpx.Response(200, content=b"import json\n")
        if request.url.path.endswith("/contents/app/new_name.py"):
            assert request.url.params["ref"] == HEAD_SHA
            return httpx.Response(200, content=b"import os\n")
        raise AssertionError(f"Unexpected request: {request.url}")

    with mock_github(handler) as github:
        pull = github.get_pull_request("owner", "repo", 7)
        changed = github.get_changed_python_files("owner", "repo", pull)

    assert (pull.changed_files, pull.head_sha, pull.base_sha) == (4, HEAD_SHA, BASE_SHA)
    # A renamed file is checked under its new path.
    assert changed.files == [
        SourceFile(path="app/service.py", content="import json\n"),
        SourceFile(path="app/new_name.py", content="import os\n"),
    ]
    assert changed.ignored == 2
    assert changed.complete


@pytest.mark.parametrize(
    "head",
    [{"sha": "deadbeef"}, {"sha": "../../evil"}, {"sha": HEAD_SHA + "0" * 40}, {}],
)
def test_github_client_rejects_a_head_that_is_not_a_commit_id(head):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"number": 7, "head": head})

    with mock_github(handler) as github, pytest.raises(GitHubAPIError) as error:
        github.get_pull_request("owner", "repo", 7)

    assert error.value.code == "github_invalid_response"


def test_github_client_reports_files_it_could_not_analyze():
    downloaded: list[str] = []
    contents = {
        "app/big.py": b"import app.infrastructure\n" + b"#" * MAX_FILE_BYTES,
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
