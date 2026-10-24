import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

import app.api
import app.main
from app.config import Settings
from app.database import engine
from app.errors import APIError
from app.healthcheck import api_ready, main, worker_alive
from app.limits import AnalysisSlots, RateLimiter
from app.main import create_app, migration_head
from app.worker import run_worker

KEY = os.environ["INVARIANT_API_KEY"]
AUTH = {"Authorization": f"Bearer {KEY}"}


def limited_app(**values):
    values.setdefault("api_key", KEY)
    names = {name: f"INVARIANT_{name.upper()}" for name in values}
    settings = Settings(_env_file=None, **{names[n]: v for n, v in values.items()})
    return create_app(settings)


def project(client) -> int:
    return client.post("/projects", json={"name": "Limits"}, headers=AUTH).json()["id"]


def analysis(path="app/a.py") -> dict:
    return {"files": [{"path": path, "content": "x = 1\n"}]}


def test_a_body_over_the_limit_is_refused_before_it_is_read():
    with TestClient(limited_app(max_body_bytes=2048)) as client:
        project_id = project(client)
        declared = client.post(
            f"/projects/{project_id}/analyze",
            content=b"{" + b" " * 4096 + b"}",
            headers={**AUTH, "Content-Type": "application/json"},
        )

        def chunks():
            yield b'{"files": ['
            for _ in range(100):
                yield b" " * 100

        streamed = client.post(
            f"/projects/{project_id}/analyze",
            content=chunks(),
            headers={**AUTH, "Content-Type": "application/json"},
        )
        within = client.post(
            f"/projects/{project_id}/analyze", json=analysis(), headers=AUTH
        )

    for response in (declared, streamed):
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "request_too_large"
        assert response.json()["error"]["message"] == (
            "The request body is larger than 2048 bytes"
        )
    assert within.status_code == 200


def test_expensive_requests_are_rate_limited_per_client():
    with TestClient(limited_app(rate_limit_per_minute=2)) as client:
        project_id = project(client)
        answers = [
            client.post(
                f"/projects/{project_id}/analyze", json=analysis(), headers=AUTH
            )
            for _ in range(3)
        ]
        # Reading is not limited.
        reads = [client.get("/projects", headers=AUTH) for _ in range(5)]

    assert [response.status_code for response in answers] == [200, 200, 429]
    refused = answers[-1]
    assert refused.json()["error"]["code"] == "too_many_requests"
    assert 1 <= int(refused.headers["Retry-After"]) <= 30
    assert {response.status_code for response in reads} == {200}


def test_the_bucket_refills_over_time_and_keys_are_separate():
    now = [0.0]
    limiter = RateLimiter(60, clock=lambda: now[0])

    assert [limiter.wait("a") for _ in range(60)] == [None] * 60
    assert limiter.wait("a") == pytest.approx(1.0)
    assert limiter.wait("b") is None
    now[0] += 1.0
    assert limiter.wait("a") is None
    assert RateLimiter(0).wait("a") is None  # 0 turns the limit off


def test_wrong_keys_are_slowed_down_but_the_right_key_never_is():
    with TestClient(limited_app(auth_failures_per_minute=2)) as client:
        wrong = [
            client.get("/projects", headers={"Authorization": "Bearer nope"})
            for _ in range(3)
        ]
        right = client.get("/projects", headers=AUTH)

    assert [response.status_code for response in wrong] == [401, 401, 429]
    assert wrong[-1].json()["error"]["code"] == "too_many_failed_keys"
    assert right.status_code == 200


def test_analyses_beyond_the_concurrency_limit_are_refused():
    application = limited_app(max_concurrent_analyses=1)
    application.state.analysis_slots = slots = AnalysisSlots(1, wait=0.05)
    with TestClient(application) as client:
        project_id = project(client)
        with slots.slot():  # another analysis is running
            busy = client.post(
                f"/projects/{project_id}/analyze", json=analysis(), headers=AUTH
            )
        free = client.post(
            f"/projects/{project_id}/analyze", json=analysis(), headers=AUTH
        )

    assert busy.status_code == 429
    assert busy.json()["error"]["code"] == "too_many_concurrent_analyses"
    assert free.status_code == 200


def test_slots_are_shared_between_threads():
    slots = AnalysisSlots(2, wait=0.05)
    held, release = threading.Barrier(3), threading.Event()

    def hold():
        with slots.slot():
            held.wait()
            release.wait()

    threads = [threading.Thread(target=hold) for _ in range(2)]
    for thread in threads:
        thread.start()
    held.wait()
    with pytest.raises(APIError, match="Other analyses are running"), slots.slot():
        pass
    release.set()
    for thread in threads:
        thread.join()
    with slots.slot():
        pass


def test_a_project_has_at_most_as_many_rules_as_a_file(client, monkeypatch):
    monkeypatch.setattr(app.api, "MAX_RULES", 2)
    project_id = client.post("/projects", json={"name": "Rules"}).json()["id"]

    created = [
        client.post(
            f"/projects/{project_id}/rules",
            json={"name": f"Rule {n}", "forbidden_imports": ["x"]},
        ).status_code
        for n in range(3)
    ]
    file = 'version = 1\n[[rules]]\nkey = "more"\nrule_type = "no_relative_import"\n'
    preview = client.post(
        f"/projects/{project_id}/config/import",
        json={"content": file, "mode": "merge", "dry_run": True},
    )

    assert created == [201, 201, 409]
    assert preview.status_code == 409
    assert preview.json()["error"]["code"] == "too_many_rules"


def test_readiness_needs_the_database_and_this_versions_schema(client, monkeypatch):
    # The test database is made by create_all, not by the migrations; CI's
    # migration step leaves an empty alembic_version table behind.
    unmigrated = client.get("/ready")
    with engine.begin() as connection:
        existed = inspect(connection).has_table("alembic_version")
        if not existed:
            connection.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32))")
            )
        connection.execute(text("DELETE FROM alembic_version"))
        connection.execute(
            text("INSERT INTO alembic_version VALUES (:head)"),
            {"head": migration_head()},
        )
    migrated = client.get("/ready")
    with engine.begin() as connection:
        # As it was found.
        connection.execute(
            text(
                "DELETE FROM alembic_version"
                if existed
                else "DROP TABLE alembic_version"
            )
        )

    def down(*_args, **_kwargs):
        raise OperationalError("connect", {}, Exception("password=hunter2 refused"))

    monkeypatch.setattr(app.main.engine, "connect", down)
    unavailable = client.get("/ready")

    assert (unmigrated.status_code, unmigrated.json()["error"]["code"]) == (
        503,
        "not_migrated",
    )
    assert (migrated.status_code, migrated.json()) == (200, {"status": "ready"})
    assert (unavailable.status_code, unavailable.json()["error"]["code"]) == (
        503,
        "database_unavailable",
    )
    assert "hunter2" not in unavailable.text
    assert client.get("/health").status_code == 200  # alive all along


class Answer(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(Answer.status if self.path == "/ready" else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_the_api_check_asks_for_readiness(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), Answer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("INVARIANT_PORT", str(server.server_port))
    try:
        assert api_ready()
        Answer.status = 503
        assert not api_ready()
    finally:
        Answer.status = 200
        server.shutdown()
        server.server_close()
    assert not api_ready()  # nothing listens any more


def test_the_worker_check_reads_its_heartbeat(monkeypatch, tmp_path):
    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setenv("INVARIANT_WORKER_HEARTBEAT", str(heartbeat))
    monkeypatch.setenv("WORKER_LEASE_SECONDS", "600")
    assert not worker_alive()  # no heartbeat yet

    run_worker(until_idle=True)

    assert heartbeat.exists()
    assert worker_alive()
    stale = time.time() - 661
    os.utime(heartbeat, (stale, stale))
    assert not worker_alive()
    assert main(["worker"]) == 1
    assert main(["nonsense"]) == 2
