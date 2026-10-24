"""Upgrade a database holding v0.2 data, then run the API on the migrated schema.

Runs only against PostgreSQL (INVARIANT_TEST_DATABASE_URL), in a separate
database that the test creates and drops.
"""

import os

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.main import app
from invariant.config import parse_config

TEST_URL = os.environ.get("INVARIANT_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_URL.startswith("postgresql"), reason="migrations target PostgreSQL"
)

V02_DATA = """
INSERT INTO projects (id, name) VALUES (1, 'payments'), (2, 'billing');
INSERT INTO rules (id, project_id, name, rule_type, source_pattern, forbidden_imports)
VALUES
  (1, 1, 'Domain isolation', 'forbidden_import', 'app.domain.*', '["app.infrastructure"]'),
  (2, 1, 'domain isolation!', 'forbidden_import', '*', '["fastapi"]'),
  (3, 1, 'Изоляция', 'forbidden_import', '*', '["django"]'),
  (4, 2, 'Domain isolation', 'forbidden_import', '*', '["app.db"]');
INSERT INTO analysis_runs
  (id, project_id, source, status, passed, files_analyzed, rules_evaluated, violation_count)
VALUES (1, 1, 'direct', 'completed', false, 1, 3, 2);
INSERT INTO analysis_violations
  (analysis_run_id, rule_id, rule_name, path, line, "column", imported_module, message)
VALUES
  (1, 1, 'Domain isolation', 'app/domain/a.py', 1, 1, 'app.infrastructure.db',
   'app.domain.a must not import app.infrastructure (matched app.infrastructure.db)'),
  (1, NULL, 'Deleted rule', 'app/domain/a.py', 2, 1, 'os', 'deleted rule message');
SELECT setval('projects_id_seq', 2), setval('rules_id_seq', 4),
       setval('analysis_runs_id_seq', 1);
"""


@pytest.fixture
def migration_url():
    url = make_url(TEST_URL)
    name = f"{url.database}_migrations"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    yield url.set(database=name).render_as_string(hide_password=False)
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def migrate(monkeypatch, url: str, direction: str, revision: str) -> None:
    config = Config()  # no ini file, so the test's logging setup is left alone
    config.set_main_option("script_location", "migrations")
    monkeypatch.setenv("DATABASE_URL", url)
    getattr(command, direction)(config, revision)


def test_v02_data_survives_the_upgrade_and_the_api_runs_on_it(
    monkeypatch, migration_url
):
    engine = create_engine(migration_url)
    migrate(monkeypatch, migration_url, "upgrade", "20261024_0003")
    with engine.begin() as connection:
        connection.exec_driver_sql(V02_DATA)

    migrate(monkeypatch, migration_url, "upgrade", "head")

    with engine.connect() as connection:
        keys = connection.execute(
            text("SELECT id, key, params, enabled, severity FROM rules ORDER BY id")
        ).all()
        violations = connection.execute(
            text(
                "SELECT kind, code, severity, rule_key, data FROM analysis_violations "
                "ORDER BY id"
            )
        ).all()
        project = connection.execute(
            text(
                "SELECT source_roots, exclude_paths, fail_on FROM projects WHERE id = 1"
            )
        ).one()
        run = connection.execute(
            text(
                "SELECT status, outcome, passed, fail_on, error_count, warning_count, "
                "analysis_error_count, files_total, started_at, config_hash, attempts "
                "FROM analysis_runs WHERE id = 1"
            )
        ).one()
    assert [(row.id, row.key) for row in keys] == [
        (1, "domain-isolation"),
        (2, "domain-isolation-2"),
        (3, "rule"),
        (4, "domain-isolation"),
    ]
    assert keys[0].params == {"forbidden_imports": ["app.infrastructure"]}
    assert all(row.enabled and row.severity == "error" for row in keys)
    assert [tuple(row) for row in violations] == [
        (
            "violation",
            "forbidden_import",
            "error",
            "domain-isolation",
            {"imported_module": "app.infrastructure.db"},
        ),
        ("violation", "forbidden_import", "error", None, {"imported_module": "os"}),
    ]
    assert tuple(project) == (["."], [], "error")
    # A v0.2 run keeps its verdict; what v0.2 did not record stays unknown.
    assert tuple(run) == (
        "completed",
        "fail",
        False,
        "error",
        2,
        0,
        0,
        None,
        None,
        None,
        1,
    )

    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def migrated_db():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = migrated_db
    try:
        with TestClient(app) as client:
            history = client.get("/analysis-runs/1").json()
            assert [item["rule_key"] for item in history["violations"]] == [
                "domain-isolation",
                None,
            ]
            assert (history["outcome"], history["files"]["total"]) == ("fail", None)
            exported = client.get("/analysis-runs/1/result").json()
            assert (exported["outcome"], len(exported["diagnostics"])) == ("fail", 2)
            incomplete = client.post(
                "/projects/1/analyze",
                json={"files": [{"path": "app/x.py", "content": "def broken(:\n"}]},
            ).json()
            assert incomplete["outcome"] == "incomplete"
            run = client.post(
                "/projects/1/analyze",
                json={
                    "files": [
                        {
                            "path": "app/domain/b.py",
                            "content": "import app.infrastructure\n",
                        }
                    ]
                },
            ).json()
            assert [item["rule_key"] for item in run["violations"]] == [
                "domain-isolation"
            ]
            created = client.post(
                "/projects/1/rules",
                json={"key": "absolute", "rule_type": "no_relative_import"},
            )
            assert created.status_code == 201
            config = parse_config(client.get("/projects/1/config").text)
            assert [rule.key for rule in config.rules] == [
                "domain-isolation",
                "domain-isolation-2",
                "rule",
                "absolute",
            ]
    finally:
        app.dependency_overrides.pop(get_db, None)

    # Downgrading drops what v0.2 cannot represent, as the migrations state:
    # first runs without a verdict, then rule settings and diagnostic fields.
    migrate(monkeypatch, migration_url, "downgrade", "20261024_0004")
    with engine.connect() as connection:
        verdicts = connection.execute(
            text("SELECT id, passed FROM analysis_runs ORDER BY id")
        ).all()
    assert incomplete["id"] not in [row.id for row in verdicts]
    assert verdicts[0] == (1, False)
    migrate(monkeypatch, migration_url, "downgrade", "20261024_0003")
    with engine.connect() as connection:
        remaining = connection.execute(
            text("SELECT id, forbidden_imports FROM rules ORDER BY id")
        ).all()
    assert [row.id for row in remaining] == [1, 2, 3, 4]
    assert remaining[0].forbidden_imports == ["app.infrastructure"]
    migrate(monkeypatch, migration_url, "upgrade", "head")
    engine.dispose()
