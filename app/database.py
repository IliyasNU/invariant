from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_database_url

DATABASE_URL = get_database_url()

engine_options: dict = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}
    if ":memory:" in DATABASE_URL:
        engine_options["poolclass"] = StaticPool

engine = create_engine(DATABASE_URL, **engine_options)

if DATABASE_URL.startswith("sqlite"):
    # SQLite ignores ON DELETE CASCADE / SET NULL unless asked; match PostgreSQL.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session]:
    with SessionLocal() as session:
        yield session
