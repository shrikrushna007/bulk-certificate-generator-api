from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
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
    storage_dir: Path = Path("./data/certificates")
    max_recipients: int = Field(default=500, ge=1, le=10_000)
    render_workers: int = Field(default=4, ge=1, le=32)
    processing_batch_size: int = Field(default=100, ge=1, le=1_000)
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return value.upper()
