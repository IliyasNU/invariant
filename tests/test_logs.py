import hashlib
import hmac
import json
import logging
from typing import Self

import pytest

from app.github import ChangedPythonFiles, PullRequestMetadata
from app.logs import (
    REQUEST_ID,
    ContextFilter,
    JSONFormatter,
    TextFormatter,
    configure_logging,
    run_context,
)
from app.schemas import SourceFile
from app.worker import Policy, run_worker

TOKEN = "ghp_" + "t" * 36
HEAD = "c" * 40


def record(message="Run 5 completed", *args, exc_info=None, **extra):
    item = logging.LogRecord(
        "app.worker", logging.INFO, __file__, 1, message, args, exc_info
    )
    ContextFilter().filter(item)
    for name, value in extra.items():
        setattr(item, name, value)
    return item


def test_a_record_is_one_json_object_with_its_ids_and_fields():
    request = REQUEST_ID.set("req-1")
    try:
        with run_context(5):
            line = JSONFormatter([]).format(
                record("Run %s completed", 5, event="run_completed", duration_ms=12)
            )
    finally:
        REQUEST_ID.reset(request)

    fields = json.loads(line)
    assert fields.pop("time").endswith("+00:00")
    assert fields == {
        "level": "INFO",
        "logger": "app.worker",
        "message": "Run 5 completed",
        "request_id": "req-1",
        "run_id": 5,
        "event": "run_completed",
        "duration_ms": 12,
    }


def test_an_explicit_run_id_is_kept_inside_another_runs_context():
    with run_context(5):
        item = logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None)
        item.run_id = 9
        ContextFilter().filter(item)

    assert item.run_id == 9


def test_secrets_are_redacted_from_the_message_the_fields_and_the_traceback():
    secret = 'web"hook\\secret'
    try:
        raise RuntimeError(f"failed with {TOKEN}")
    except RuntimeError as exc:
        exc_info = (type(exc), exc, exc.__traceback__)
    item = record(
        "Signed with %s; database %s; Authorization: Bearer abc.def-123456",
        secret,
        "postgresql+psycopg://invariant:hunter2pass@db/invariant",
        exc_info=exc_info,
        detail=f"token {TOKEN}",
    )

    for formatter in (JSONFormatter([secret]), TextFormatter([secret])):
        line = formatter.format(item)
        for leaked in ("hunter2pass", "abc.def-123456", TOKEN, "hook\\\\secret"):
            assert leaked not in line
        assert line.count("[redacted]") >= 5
        assert "RuntimeError" in line  # the traceback itself is kept
    fields = json.loads(JSONFormatter([secret]).format(item))
    assert fields["message"] == (
        "Signed with [redacted]; database "
        "postgresql+psycopg://invariant:[redacted]@db/invariant; "
        "Authorization: Bearer [redacted]"
    )


def test_text_lines_put_the_message_first_and_the_fields_after():
    line = TextFormatter([]).format(record("Run 5 completed", event="run_completed"))

    assert (
        line.split(" ", 1)[1] == "INFO app.worker Run 5 completed event=run_completed"
    )


@pytest.fixture
def restored_logging():
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    uvicorn = logging.getLogger("uvicorn")
    saved_uvicorn = uvicorn.handlers[:], uvicorn.propagate
    yield
    root.handlers[:], level = saved
    root.setLevel(level)
    uvicorn.handlers[:], uvicorn.propagate = saved_uvicorn
    logging.getLogger("httpx").setLevel(logging.NOTSET)


def test_configuration_sends_every_record_to_one_handler(restored_logging):
    configure_logging("debug", "text", [TOKEN])

    [handler] = logging.getLogger().handlers
    assert isinstance(handler.formatter, TextFormatter)
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("uvicorn").propagate
    assert not logging.getLogger("uvicorn").handlers
    assert logging.getLogger("httpx").level == logging.WARNING


class OneFileGitHub:
    comment_enabled = True

    def __init__(self) -> None:
        self.comments: dict[int, str] = {}

    def get_pull_request(self, owner, repo, pull_number) -> PullRequestMetadata:
        return PullRequestMetadata(number=pull_number, head_sha=HEAD)

    def get_changed_python_files(self, owner, repo, pull_request, **options):
        return ChangedPythonFiles(
            files=[SourceFile(path="app/domain/a.py", content="import json\n")],
            skipped=[],
            truncated=False,
            ignored=0,
        )

    def create_comment(self, owner, repo, pull_number, body) -> int:
        self.comments[1] = body
        return 1

    def update_comment(self, owner, repo, comment_id, body) -> bool:
        self.comments[comment_id] = body
        return True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def test_a_run_can_be_followed_from_the_delivery_to_its_comment(
    client, monkeypatch, caplog
):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret")
    project = client.post(
        "/projects", json={"name": "Shop", "github_owner": "o", "github_repo": "r"}
    ).json()
    client.post(
        f"/projects/{project['id']}/rules",
        json={"name": "Domain", "forbidden_imports": ["app.infrastructure"]},
    )
    body = json.dumps(
        {
            "action": "opened",
            "repository": {"name": "r", "owner": {"login": "o"}},
            "pull_request": {"number": 7},
        }
    ).encode()
    signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    caplog.handler.addFilter(ContextFilter())

    with caplog.at_level(logging.INFO):
        response = client.post(
            "/integrations/github/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-7",
                "X-Request-ID": "delivery-request",
            },
        )
        run_id = response.json()["analysis_run_id"]
        run_worker(github_factory=OneFileGitHub, policy=Policy(), until_idle=True)

    trail = [r for r in caplog.records if getattr(r, "run_id", None) == run_id]
    assert [r.event for r in trail] == [
        "run_queued",
        "run_claimed",
        "pull_request_fetched",
        "analysis_finished",
        "run_completed",
        "feedback_posted",
    ]
    assert trail[0].request_id == "delivery-request"
    assert trail[0].github_delivery_id == "delivery-7"
    assert trail[2].head_sha == HEAD
    assert {r.outcome for r in trail if hasattr(r, "outcome")} == {"pass"}
    assert all(r.duration_ms >= 0 for r in trail if hasattr(r, "duration_ms"))
