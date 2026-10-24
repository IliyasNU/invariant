import logging

import pytest

from app.logs import ContextFilter

SECRET = "ghp_" + "s" * 36


def create_project(client, name="Payments"):
    response = client.post("/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()


def test_every_error_has_one_format_with_the_request_id(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret")
    create_project(client)
    responses = {
        "project_not_found": client.get("/projects/999"),
        "not_found": client.get("/no/such/path"),
        "method_not_allowed": client.delete("/health"),
        "validation_error": client.post("/projects", json={}),
        "project_conflict": client.post("/projects", json={"name": "Payments"}),
        "invalid_github_signature": client.post(
            "/integrations/github/webhook", content=b"{}"
        ),
    }

    for code, response in responses.items():
        error = response.json()["error"]
        assert error["code"] == code, response.text
        assert error["message"]
        assert set(error) <= {"code", "message", "request_id", "fields"}
        assert error["request_id"] == response.headers["X-Request-ID"]
    assert [response.status_code for response in responses.values()] == [
        404,
        404,
        405,
        422,
        409,
        401,
    ]


def test_validation_errors_name_the_fields_without_echoing_the_input(client):
    response = client.post(
        "/projects", json={"name": SECRET * 4, "fail_on": SECRET, "limit": 1}
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert sorted((item["location"], item["type"]) for item in error["fields"]) == [
        ("body.fail_on", "literal_error"),
        ("body.name", "string_too_long"),
    ]
    assert SECRET not in response.text


def test_a_usable_request_id_is_kept_and_any_other_is_replaced(client):
    kept = client.get("/health", headers={"X-Request-ID": "trace-1.2:3_x"})
    assert kept.headers["X-Request-ID"] == "trace-1.2:3_x"

    for offered in ["a" * 65, "has space", "semi;colon", "<x>", ""]:
        response = client.get("/projects/999", headers={"X-Request-ID": offered})
        replaced = response.headers["X-Request-ID"]
        assert replaced != offered
        assert len(replaced) == 32
        assert response.json()["error"]["request_id"] == replaced


def test_an_unexpected_error_is_an_internal_error_logged_with_its_request(
    client, monkeypatch, caplog
):
    project = create_project(client)

    def broken(*_args, **_kwargs):
        raise RuntimeError(f"failed with {SECRET} in the message")

    monkeypatch.setattr("app.api.persist_analysis", broken)
    caplog.handler.addFilter(ContextFilter())
    with caplog.at_level(logging.INFO):
        response = client.post(
            f"/projects/{project['id']}/analyze",
            json={"files": [{"path": "app/x.py", "content": "x = 1\n"}]},
            headers={"X-Request-ID": "req-500"},
        )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "The server failed unexpectedly",
            "request_id": "req-500",
        }
    }
    assert "Traceback" not in response.text
    assert SECRET not in response.text
    [failure] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert failure.request_id == "req-500"
    assert failure.exc_info[0] is RuntimeError
    [request] = [r for r in caplog.records if getattr(r, "event", "") == "request"]
    assert (request.status, request.request_id, request.path) == (
        500,
        "req-500",
        f"/projects/{project['id']}/analyze",
    )


@pytest.mark.parametrize("path", ["/health", "/projects"])
def test_every_request_is_logged_with_its_status_and_duration(client, caplog, path):
    caplog.handler.addFilter(ContextFilter())
    with caplog.at_level(logging.INFO, logger="app.errors"):
        response = client.get(path)

    [record] = [r for r in caplog.records if getattr(r, "event", "") == "request"]
    assert (record.method, record.path, record.status) == ("GET", path, 200)
    assert record.request_id == response.headers["X-Request-ID"]
    assert record.duration_ms >= 0


def test_the_openapi_schema_describes_the_error_format(client):
    components = client.get("/openapi.json").json()["components"]["schemas"]

    assert components["HTTPValidationError"]["properties"] == {
        "error": {"$ref": "#/components/schemas/ErrorBody"}
    }
    assert set(components["ErrorBody"]["properties"]) == {
        "code",
        "message",
        "request_id",
        "fields",
    }
