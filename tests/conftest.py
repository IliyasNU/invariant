import os

import pytest
from fastapi.testclient import TestClient

# Tests recreate the schema, so point INVARIANT_TEST_DATABASE_URL at a
# disposable database, e.g. to run the suite against PostgreSQL in CI.
os.environ["DATABASE_URL"] = os.environ.get(
    "INVARIANT_TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:"
)
# The test client presents this key, as every client of the API must.
os.environ["INVARIANT_API_KEY"] = API_KEY = "test-api-key-" + "k" * 32
# Tests send many requests at once; tests of the limits set their own.
os.environ["INVARIANT_RATE_LIMIT_PER_MINUTE"] = "0"
os.environ["INVARIANT_AUTH_FAILURES_PER_MINUTE"] = "0"
# Workers in tests keep no heartbeat file, but the test of it.
os.environ["INVARIANT_WORKER_HEARTBEAT"] = ""

from app.database import engine
from app.main import app
from app.models import Base


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client() -> TestClient:
    with TestClient(app, headers={"Authorization": f"Bearer {API_KEY}"}) as test_client:
        yield test_client
