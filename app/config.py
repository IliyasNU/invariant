from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str | None = None
    postgres_db: str = "invariant"
    postgres_user: str = "invariant"
    postgres_password: str = "invariant"
    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5433, ge=1, le=65535)


def get_database_url() -> str:
    """Build the SQLAlchemy URL from the environment.

    DATABASE_URL is useful in hosted environments and tests. For local
    development, the individual POSTGRES_* settings match compose.yaml.
    """
    settings = Settings()
    if settings.database_url:
        return settings.database_url

    return URL.create(
        drivername="postgresql+psycopg",
        username=settings.postgres_user,
        password=settings.postgres_password,
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
    ).render_as_string(hide_password=False)
