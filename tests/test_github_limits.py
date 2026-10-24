import json
import time

import httpx
import pytest

import app.github
from app.config import Settings
from app.github import (
    MAX_RESPONSE_BYTES,
    GitHubAPIError,
    GitHubClient,
    PullRequestMetadata,
    SkippedFile,
)
from invariant.files import MAX_FILE_BYTES

HEAD = "0123456789abcdef0123456789abcdef01234567"
OTHER_HEAD = "fedcba9876543210fedcba9876543210fedcba98"


def client(handler) -> GitHubClient:
    settings = Settings(_env_file=None, github_api_url="https://api.github.test")
    return GitHubClient(settings, transport=httpx.MockTransport(handler))


class Stream:
    """A response body in 64 KiB chunks that counts how much was read."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.served = 0

    def __iter__(self):
        while self.served < self.size:
            chunk = min(65_536, self.size - self.served)
            self.served += chunk
            yield b"#" * chunk


def pull_request_with(files: list[str], contents, heads=None):
    """A handler for pull request 7 changing ``files``; ``heads`` in turn."""
    heads = list(heads or [HEAD])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/7"):
            head = heads.pop(0) if len(heads) > 1 else heads[0]
            return httpx.Response(200, json={"number": 7, "head": {"sha": head}})
        if path.endswith("/pulls/7/files"):
            return httpx.Response(
                200, json=[{"filename": name, "status": "added"} for name in files]
            )
        name = path.split("/contents/", 1)[1]
        return contents(name)

    return handler


def test_a_file_over_the_limit_is_abandoned_while_streaming():
    body = Stream(20 * MAX_FILE_BYTES)
    handler = pull_request_with(
        ["app/big.py"], lambda _: httpx.Response(200, content=body)
    )

    with client(handler) as github:
        changed = github.get_changed_python_files(
            "o", "r", PullRequestMetadata(number=7, head_sha=HEAD)
        )

    assert changed.skipped == [SkippedFile("app/big.py", "too_large")]
    # Read up to the limit and one chunk more, not the twenty megabytes.
    assert MAX_FILE_BYTES < body.served <= MAX_FILE_BYTES + 65_536


def test_a_declared_length_over_the_limit_is_not_read_at_all():
    body = Stream(MAX_FILE_BYTES + 1)

    def contents(_name):
        return httpx.Response(
            200, content=body, headers={"Content-Length": str(MAX_FILE_BYTES + 1)}
        )

    with client(pull_request_with(["app/big.py"], contents)) as github:
        changed = github.get_changed_python_files(
            "o", "r", PullRequestMetadata(number=7, head_sha=HEAD)
        )

    assert changed.skipped == [SkippedFile("app/big.py", "too_large")]
    assert body.served == 0


def test_an_oversized_api_response_is_refused():
    def handler(_request):
        return httpx.Response(200, content=Stream(MAX_RESPONSE_BYTES + 1))

    with client(handler) as github, pytest.raises(GitHubAPIError) as error:
        github.get_pull_request("o", "r", 7)

    assert error.value.code == "github_invalid_response"
    assert not error.value.transient


def test_a_pull_request_that_moves_while_listed_is_retried_not_mixed():
    downloaded = []

    def contents(name):
        downloaded.append(name)
        return httpx.Response(200, content=b"x = 1\n")

    handler = pull_request_with(["app/a.py"], contents, heads=[OTHER_HEAD])

    with client(handler) as github, pytest.raises(GitHubAPIError) as error:
        github.get_changed_python_files(
            "o", "r", PullRequestMetadata(number=7, head_sha=HEAD)
        )

    assert (error.value.code, error.value.transient) == (
        "github_pull_request_changed",
        True,
    )
    assert downloaded == []


def test_files_past_the_total_size_or_the_deadline_are_reported(monkeypatch):
    monkeypatch.setattr(app.github, "MAX_TOTAL_BYTES", 10)
    files = ["app/a.py", "app/b.py", "app/c.py"]
    downloaded = []

    def contents(name):
        downloaded.append(name)
        return httpx.Response(200, content=b"x = 1  # twelve\n")

    with client(pull_request_with(files, contents)) as github:
        sized = github.get_changed_python_files(
            "o", "r", PullRequestMetadata(number=7, head_sha=HEAD)
        )
        late = github.get_changed_python_files(
            "o",
            "r",
            PullRequestMetadata(number=7, head_sha=HEAD),
            deadline=time.monotonic() - 1,
        )

    assert [item.path for item in sized.files] == ["app/a.py"]
    assert sized.skipped == [
        SkippedFile("app/b.py", "limit_exceeded"),
        SkippedFile("app/c.py", "limit_exceeded"),
    ]
    assert late.files == []
    assert {item.reason for item in late.skipped} == {"limit_exceeded"}
    assert downloaded == ["app/a.py"]


def test_comments_are_created_and_edited_through_the_issues_api():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, json.loads(request.content)))
        if request.method == "POST":
            return httpx.Response(201, json={"id": 4242})
        if request.url.path.endswith("/comments/404"):
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json={"id": 4242})

    with client(handler) as github:
        created = github.create_comment("o", "r", 7, "first")
        edited = github.update_comment("o", "r", created, "second")
        missing = github.update_comment("o", "r", 404, "third")

    assert (created, edited, missing) == (4242, True, False)
    assert requests == [
        ("POST", "/repos/o/r/issues/7/comments", {"body": "first"}),
        ("PATCH", "/repos/o/r/issues/comments/4242", {"body": "second"}),
        ("PATCH", "/repos/o/r/issues/comments/404", {"body": "third"}),
    ]


def test_a_comment_response_without_an_id_is_an_error():
    with (
        client(lambda _request: httpx.Response(201, json={})) as github,
        pytest.raises(GitHubAPIError) as error,
    ):
        github.create_comment("o", "r", 7, "body")

    assert error.value.code == "github_invalid_response"


@pytest.mark.parametrize(
    ("headers", "retry_after"),
    [
        ({"Retry-After": "120"}, 120.0),
        ({}, None),
        # str.isdigit() accepts it, float() does not: no wait, not a crash.
        ({"Retry-After": "\u00b2".encode("latin-1")}, None),
        ({"X-RateLimit-Reset": "\u00b9\u00b2".encode("latin-1")}, None),
    ],
)
def test_rate_limits_are_transient_and_carry_retry_after(headers, retry_after):
    def handler(_request):
        return httpx.Response(429, headers=headers)

    with client(handler) as github, pytest.raises(GitHubAPIError) as error:
        github.get_pull_request("o", "r", 7)

    assert (error.value.transient, error.value.retry_after) == (True, retry_after)


def test_a_rate_limit_reset_sets_the_wait(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1_000.0)

    def handler(_request):
        return httpx.Response(
            403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1090"}
        )

    with client(handler) as github, pytest.raises(GitHubAPIError) as error:
        github.get_pull_request("o", "r", 7)

    assert (error.value.code, error.value.retry_after) == ("github_rate_limited", 90)


@pytest.mark.parametrize(
    ("status", "transient"), [(500, True), (503, True), (422, False)]
)
def test_server_errors_are_transient_and_other_errors_are_not(status, transient):
    with (
        client(lambda _request: httpx.Response(status)) as github,
        pytest.raises(GitHubAPIError) as error,
    ):
        github.get_pull_request("o", "r", 7)

    assert error.value.transient is transient


def test_network_errors_are_transient():
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    with client(handler) as github, pytest.raises(GitHubAPIError) as error:
        github.get_pull_request("o", "r", 7)

    assert (error.value.code, error.value.transient) == ("github_unavailable", True)


def test_a_moved_repository_is_followed_and_other_redirects_are_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/o/r/pulls/7":
            return httpx.Response(
                301,
                headers={"Location": "https://api.github.test/repos/o/new/pulls/7"},
                json={"message": "Moved Permanently"},
            )
        if request.url.path == "/repos/o/new/pulls/7":
            assert request.headers["Authorization"] == "Bearer token"
            return httpx.Response(200, json={"number": 7, "head": {"sha": HEAD}})
        return httpx.Response(304)

    settings = Settings(
        _env_file=None, github_api_url="https://api.github.test", github_token="token"
    )
    with GitHubClient(settings, transport=httpx.MockTransport(handler)) as github:
        moved = github.get_pull_request("o", "r", 7)
        with pytest.raises(GitHubAPIError) as error:
            github.get_file_bytes("o", "r", "app/a.py", HEAD)

    assert moved.head_sha == HEAD
    assert (error.value.code, error.value.transient) == ("github_api_error", False)
