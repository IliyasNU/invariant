"""Each API instance verifies deliveries with its own configured secret."""

import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

BODY = b"{}"


def application(secret: str | None):
    return create_app(
        Settings(
            _env_file=None,
            INVARIANT_API_KEY=None,
            INVARIANT_LOCAL_ONLY=True,
            github_webhook_secret=secret,
        )
    )


def headers(secret: str) -> dict[str, str]:
    digest = hmac.new(secret.encode(), BODY, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "X-GitHub-Event": "ping"}


def test_webhook_secrets_are_isolated_between_apps_and_ignore_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "environment-secret")
    first_app = application("first-app-secret")
    second_app = application("second-app-secret")

    with TestClient(first_app) as first, TestClient(second_app) as second:
        for client, own_secret, other_secret in (
            (first, "first-app-secret", "second-app-secret"),
            (second, "second-app-secret", "first-app-secret"),
        ):
            own = client.post(
                "/integrations/github/webhook",
                content=BODY,
                headers=headers(own_secret),
            )
            assert own.status_code == 202
            assert own.json()["status"] == "ignored"
            for secret in (other_secret, "environment-secret"):
                refused = client.post(
                    "/integrations/github/webhook",
                    content=BODY,
                    headers=headers(secret),
                )
                assert refused.status_code == 401
                assert refused.json()["error"]["code"] == "invalid_github_signature"

        # Changing the process environment after startup cannot rotate either
        # application's key; configuration belongs to that application's state.
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "changed-environment-secret")
        for client, secret in (
            (first, "first-app-secret"),
            (second, "second-app-secret"),
        ):
            assert (
                client.post(
                    "/integrations/github/webhook",
                    content=BODY,
                    headers=headers(secret),
                ).status_code
                == 202
            )
            assert (
                client.post(
                    "/integrations/github/webhook",
                    content=BODY,
                    headers=headers("changed-environment-secret"),
                ).status_code
                == 401
            )


def test_webhook_without_app_secret_is_disabled_even_when_environment_has_one(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "environment-secret")
    with TestClient(application(None)) as client:
        response = client.post(
            "/integrations/github/webhook",
            content=BODY,
            headers=headers("environment-secret"),
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "github_webhook_not_configured"


@pytest.mark.parametrize(
    "signature", [None, b"sha256=invalid", b"sha256=\xff", b"sha256=" + b"0" * 64]
)
def test_custom_app_rejects_missing_or_malformed_signature(signature):
    offered = {"X-GitHub-Event": "ping"}
    if signature is not None:
        offered["X-Hub-Signature-256"] = signature
    with TestClient(application("configured-secret")) as client:
        response = client.post(
            "/integrations/github/webhook", content=BODY, headers=offered
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_github_signature"
