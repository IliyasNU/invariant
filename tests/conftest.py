import os

import pytest
from fastapi.testclient import TestClient

# Tests recreate the schema, so point INVARIANT_TEST_DATABASE_URL at a
# disposable database, e.g. to run the suite against PostgreSQL in CI.
os.environ["DATABASE_URL"] = os.environ.get(
    "INVARIANT_TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:"
)

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
    with TestClient(app) as test_client:
        yield test_client
