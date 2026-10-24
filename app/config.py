from typing import Literal

from pydantic import Field, SecretStr, field_validator
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
    # The operator's key for the API: every request presents it as
    # "Authorization: Bearer <key>".
    api_key: SecretStr | None = Field(
        default=None, validation_alias="INVARIANT_API_KEY"
    )
    # Without a key the API starts only when this says that it is reachable
    # from this machine alone.
    local_only: bool = Field(default=False, validation_alias="INVARIANT_LOCAL_ONLY")
    # Browser origins allowed to call the API, comma-separated; none by default.
    cors_origins: str = Field(default="", validation_alias="INVARIANT_CORS_ORIGINS")
    # Limits of the API (app.limits); a rate limit of 0 is off.
    max_body_bytes: int = Field(
        default=16 * 1024 * 1024, ge=1024, validation_alias="INVARIANT_MAX_BODY_BYTES"
    )
    rate_limit_per_minute: int = Field(
        default=60, ge=0, validation_alias="INVARIANT_RATE_LIMIT_PER_MINUTE"
    )
    auth_failures_per_minute: int = Field(
        default=30, ge=0, validation_alias="INVARIANT_AUTH_FAILURES_PER_MINUTE"
    )
    max_concurrent_analyses: int = Field(
        default=2, ge=1, le=64, validation_alias="INVARIANT_MAX_CONCURRENT_ANALYSES"
    )
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
    # Runs that finished longer ago are deleted by the worker; 0 keeps them.
    retention_days: int = Field(
        default=0, ge=0, validation_alias="INVARIANT_RETENTION_DAYS"
    )
    # Touched by the worker as it goes, for its health check; empty for none.
    worker_heartbeat_file: str = Field(
        default="/tmp/invariant-worker-heartbeat",
        validation_alias="INVARIANT_WORKER_HEARTBEAT",
    )
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

    @field_validator("api_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def cors_origin_list(self) -> list[str]:
        return [
            origin.strip().rstrip("/")
            for origin in self.cors_origins.split(",")
            if origin.strip()
        ]

    def secrets(self) -> list[str]:
        """Configured values that must never appear in a log."""
        values = [self.github_token, self.github_webhook_secret]
        if self.api_key is not None:
            values.append(self.api_key.get_secret_value())
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
