from typing import Literal

from pydantic import Field, field_validator
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
    github_token: str | None = None
    github_webhook_secret: str | None = None
    github_post_pr_comment: bool = False
    github_api_url: str = "https://api.github.com"
    # Where python -m app.server listens.
    host: str = Field(default="127.0.0.1", validation_alias="INVARIANT_HOST")
    port: int = Field(default=8000, ge=1, le=65535, validation_alias="INVARIANT_PORT")
    # Where this API is reachable, for links in pull request comments.
    public_url: str | None = Field(
        default=None, validation_alias="INVARIANT_PUBLIC_URL"
    )
    # The worker that runs queued GitHub analyses.
    worker_max_attempts: int = Field(default=3, ge=1, le=20)
    worker_lease_seconds: int = Field(default=600, ge=30)
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    # Files not downloaded by then make a run incomplete; less than the lease.
    worker_job_seconds: int = Field(default=300, ge=10)
    # The first retry waits this long, each next one twice as long.
    worker_retry_seconds: float = Field(default=30.0, gt=0)
    # One JSON object per line, or text for a terminal.
    log_format: Literal["json", "text"] = Field(
        default="json", validation_alias="INVARIANT_LOG_FORMAT"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", validation_alias="INVARIANT_LOG_LEVEL"
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    def secrets(self) -> list[str]:
        """Configured values that must never appear in a log."""
        values = [self.github_token, self.github_webhook_secret]
        # The documented local password is not a secret, and would redact the
        # name of the project from every line.
        if self.postgres_password != "invariant":
            values.append(self.postgres_password)
        return [value for value in values if value]


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
