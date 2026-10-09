from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
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
