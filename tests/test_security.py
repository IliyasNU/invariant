import logging
import os
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import String, Text, select

from app.config import Settings
from app.database import SessionLocal
from app.github import GitHubAPIError
from app.main import app, create_app
from app.models import Base
from app.security import InsecureConfiguration
from app.worker import run_worker

KEY = os.environ["INVARIANT_API_KEY"]
REPOSITORY = Path(__file__).resolve().parents[1]
# Reachable without the key: liveness, the documentation, and the webhook,
# which checks GitHub's signature instead.
OPEN = {
    "/health",
    "/db-health",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/integrations/github/webhook",
}


def settings(**values) -> Settings:
    """Settings by field name; these fields are set by their variable's name."""
    values.setdefault("api_key", KEY)
    names = {name: f"INVARIANT_{name.upper()}" for name in values}
    return Settings(_env_file=None, **{names[name]: v for name, v in values.items()})


class FailingGitHub:
    """GitHub rejecting the token, as it would with the token in its message."""

    comment_enabled = False

    def get_pull_request(self, owner, repo, pull_number):
        raise GitHubAPIError(
            502,
            "github_authentication_failed",
            "GitHub rejected the configured credentials or permissions",
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def business_routes():
    # Every route the application documents, which is every route it has.
    for path, operations in app.openapi()["paths"].items():
        if path not in OPEN:
            for method in operations:
                yield method.upper(), re.sub(r"\{[^}]+\}", "1", path)


def test_every_business_route_requires_the_key():
    routes = sorted(business_routes())
    assert {
        ("GET", "/projects"),
        ("POST", "/projects/1/analyze"),
        ("POST", "/projects/1/analyze/github"),
        ("POST", "/projects/1/config/import"),
        ("GET", "/analysis-runs/1/result"),
    } <= set(routes)

    with TestClient(app) as anonymous:
        for method, path in routes:
            response = anonymous.request(method, path, json={})
            assert response.status_code == 401, (method, path)
            assert response.json()["error"]["code"] == "unauthorized"
            assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("headers", "params", "message"),
    [
        ({}, {}, "An API key is required: Authorization: Bearer <key>"),
        ({"Authorization": f"Bearer {KEY}x"}, {}, "The API key is not valid"),
        ({"Authorization": f"Bearer {KEY[:-1]}"}, {}, "The API key is not valid"),
        (
            {"Authorization": f"Basic {KEY}"},
            {},
            "An API key is required: Authorization: Bearer <key>",
        ),
        # The key is never read from the URL.
        ({}, {"api_key": KEY}, "An API key is required: Authorization: Bearer <key>"),
    ],
)
def test_a_missing_or_wrong_key_is_refused(headers, params, message):
    with TestClient(app) as anonymous:
        response = anonymous.get("/projects", headers=headers, params=params)

    assert response.status_code == 401
    assert response.json()["error"]["message"] == message
    assert KEY not in response.text


def test_the_open_routes_need_no_key(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret")
    with TestClient(app) as anonymous:
        assert anonymous.get("/health").status_code == 200
        assert anonymous.get("/db-health").status_code == 200
        schema = anonymous.get("/openapi.json").json()
        webhook = anonymous.post("/integrations/github/webhook", content=b"{}")

    # The webhook answers for its own signature, not for an API key.
    assert webhook.json()["error"]["code"] == "invalid_github_signature"
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert schema["paths"]["/projects"]["get"]["security"] == [{"HTTPBearer": []}]
    assert "security" not in schema["paths"]["/health"]["get"]


@pytest.mark.parametrize(
    ("values", "problem"),
    [
        ({"api_key": None}, "Set INVARIANT_API_KEY"),
        ({"api_key": "  "}, "Set INVARIANT_API_KEY"),
        ({"api_key": "short-key"}, "at least 32 characters"),
        ({"cors_origins": "https://ui.example, *"}, "not '*'"),
    ],
)
def test_the_api_refuses_to_start_open_by_mistake(values, problem):
    with (
        pytest.raises(InsecureConfiguration, match=re.escape(problem)),
        TestClient(create_app(settings(**values))),
    ):
        pass


def test_local_only_mode_must_be_asked_for_and_says_so(caplog):
    local = create_app(settings(api_key=None, local_only=True))

    with caplog.at_level(logging.WARNING), TestClient(local) as anonymous:
        assert anonymous.get("/projects").status_code == 200

    [warning] = [r for r in caplog.records if getattr(r, "event", "") == "local_only"]
    assert "this machine only" in warning.getMessage()


def test_a_key_is_required_even_in_local_only_mode():
    with TestClient(create_app(settings(local_only=True))) as anonymous:
        assert anonymous.get("/projects").status_code == 401


def test_cross_origin_requests_are_refused_unless_the_origin_is_listed():
    preflight = {
        "Origin": "https://ui.example",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    with TestClient(app) as default:
        refused = default.options("/projects", headers=preflight)
    listed = create_app(settings(cors_origins="https://ui.example/"))
    with TestClient(listed) as browser:
        allowed = browser.options("/projects", headers=preflight)
        other = browser.options(
            "/projects", headers={**preflight, "Origin": "https://evil.example"}
        )
        read = browser.get(
            "/projects",
            headers={"Origin": "https://ui.example", "Authorization": f"Bearer {KEY}"},
        )

    assert "access-control-allow-origin" not in refused.headers
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://ui.example"
    assert "access-control-allow-credentials" not in allowed.headers
    assert "access-control-allow-origin" not in other.headers
    assert read.headers["access-control-allow-origin"] == "https://ui.example"
    assert "X-Request-ID" in read.headers["access-control-expose-headers"]


def test_a_plain_http_public_url_is_warned_about(caplog):
    with caplog.at_level(logging.WARNING):
        with TestClient(create_app(settings(public_url="http://invariant.example"))):
            pass
        with TestClient(create_app(settings(public_url="http://127.0.0.1:8000"))):
            pass

    warnings = [r for r in caplog.records if r.event == "public_url_not_https"]
    assert len(warnings) == 1


def test_the_key_is_one_of_the_secrets_redacted_from_logs():
    assert KEY in settings().secrets()
    assert KEY not in repr(settings())


def test_no_secret_reaches_the_database(client, monkeypatch):
    token, webhook_secret = "ghp_" + "q" * 36, "webhook-" + "w" * 24
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", webhook_secret)
    project = client.post(
        "/projects", json={"name": "Shop", "github_owner": "o", "github_repo": "r"}
    ).json()
    client.post(
        f"/projects/{project['id']}/rules",
        json={"name": "Domain", "forbidden_imports": ["app.infrastructure"]},
    )
    client.post(f"/projects/{project['id']}/analyze/github", json={"pull_number": 7})
    assert run_worker(github_factory=FailingGitHub, until_idle=True) == 1
    client.post(
        f"/projects/{project['id']}/analyze",
        json={
            "files": [{"path": "app/a.py", "content": "import app.infrastructure\n"}]
        },
    )

    stored = []
    with SessionLocal() as db:
        for table in Base.metadata.sorted_tables:
            columns = [
                column
                for column in table.columns
                if isinstance(column.type, String | Text) or "JSON" in str(column.type)
            ]
            for row in db.execute(select(*columns)) if columns else []:
                stored.extend(str(value) for value in row)
    assert any("github_authentication_failed" in value for value in stored)
    for secret in (token, webhook_secret, KEY):
        assert not any(secret in value for value in stored)


def test_the_example_environment_holds_no_secret():
    example = (REPOSITORY / ".env.example").read_text()
    values = dict(
        line.split("=", 1)
        for line in example.splitlines()
        if "=" in line and not line.startswith("#")
    )

    for name in ("GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET", "INVARIANT_API_KEY"):
        assert values[name] == "", name


def test_the_repository_holds_no_token_or_private_key():
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPOSITORY,
            capture_output=True,
            check=True,
        ).stdout.split(b"\0")
    except OSError, subprocess.CalledProcessError:
        pytest.skip("not a Git checkout")
    pattern = re.compile(
        rb"gh[opsur]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,}"
        rb"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    )

    leaks = [
        name.decode()
        for name in tracked
        if name and pattern.search((REPOSITORY / name.decode()).read_bytes())
    ]
    assert leaks == []
