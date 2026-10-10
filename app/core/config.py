from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    database_url: str = "sqlite:///./data/certificates.db"
    database_host: str | None = None
    database_port: int = Field(default=5432, ge=1, le=65_535)
    database_name: str | None = None
    database_user: str | None = None
    database_password: SecretStr | None = None
    database_ssl_mode: Literal[
        "disable", "allow", "prefer", "require", "verify-ca", "verify-full"
    ] = "prefer"
    processing_mode: Literal["inline", "distributed"] = "inline"
    storage_backend: Literal["local", "s3"] = "local"
    storage_dir: Path = Path("./data/certificates")
    max_recipients: int = Field(default=500, ge=1, le=1_000_000)
    max_csv_recipients: int = Field(default=0, ge=0)
    render_workers: int = Field(default=4, ge=1, le=32)
    processing_batch_size: int = Field(default=100, ge=1, le=1_000)
    distributed_job_parallelism: int = Field(default=4, ge=1, le=128)
    claim_timeout_seconds: int = Field(default=300, ge=30, le=86_400)
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_broker_host: str | None = None
    celery_broker_port: int = Field(default=6379, ge=1, le=65_535)
    celery_broker_username: str | None = None
    celery_broker_password: SecretStr | None = None
    celery_broker_tls: bool = False
    s3_bucket: str = "certflow"
    s3_region: str = "us-east-1"
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_auto_create_bucket: bool = False
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def validate_distributed_profile(self) -> Settings:
        self._build_database_url()
        self._build_broker_url()
        if bool(self.s3_access_key_id) != bool(self.s3_secret_access_key):
            raise ValueError(
                "S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be configured together"
            )
        if self.processing_mode == "distributed":
            if self.database_url.startswith("sqlite"):
                raise ValueError("Distributed processing requires a non-SQLite database.")
            if self.storage_backend != "s3":
                raise ValueError("Distributed processing requires STORAGE_BACKEND=s3.")
        elif self.storage_backend == "s3":
            raise ValueError("S3 storage requires PROCESSING_MODE=distributed.")
        return self

    def _build_database_url(self) -> None:
        if not self.database_host:
            return
        if not self.database_name or not self.database_user or not self.database_password:
            raise ValueError(
                "DATABASE_NAME, DATABASE_USER, and DATABASE_PASSWORD are required when "
                "DATABASE_HOST is configured."
            )
        username = quote_plus(self.database_user)
        password = quote_plus(self.database_password.get_secret_value())
        self.database_url = (
            f"postgresql+psycopg://{username}:{password}@{self.database_host}:"
            f"{self.database_port}/{self.database_name}?sslmode={self.database_ssl_mode}"
        )

    def _build_broker_url(self) -> None:
        if not self.celery_broker_host:
            return
        if not self.celery_broker_password:
            raise ValueError(
                "CELERY_BROKER_PASSWORD is required when CELERY_BROKER_HOST is configured."
            )
        scheme = "rediss" if self.celery_broker_tls else "redis"
        username = quote_plus(self.celery_broker_username or "default")
        password = quote_plus(self.celery_broker_password.get_secret_value())
        query = "?ssl_cert_reqs=required" if self.celery_broker_tls else ""
        self.celery_broker_url = (
            f"{scheme}://{username}:{password}@{self.celery_broker_host}:"
            f"{self.celery_broker_port}/0{query}"
        )
