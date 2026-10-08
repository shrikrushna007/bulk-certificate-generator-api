from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models import CertificateStatus, JobStatus


class RecipientInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Any = None
    email: Any = None


class CertificateDetailsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    issuing_organization: str = Field(min_length=1, max_length=200)
    issue_date: date

    @field_validator("title", "issuing_organization")
    @classmethod
    def require_nonblank(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    certificate: CertificateDetailsInput
    recipients: list[RecipientInput]

    @field_validator("recipients")
    @classmethod
    def require_recipients(cls, value: list[RecipientInput]) -> list[RecipientInput]:
        if not value:
            raise ValueError("at least one recipient is required")
        return value


class JobCreatedResponse(BaseModel):
    job_id: str
    status: JobStatus
    total_recipients: int
    status_url: str


class CertificateOutcomeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    certificate_id: str
    recipient_index: int
    recipient_name: str | None
    recipient_email: str | None
    status: CertificateStatus
    error_message: str | None
    download_url: str | None


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    certificate_title: str
    issuing_organization: str
    issue_date: date
    total_recipients: int
    successful_generations: int
    failed_generations: int
    pending_generations: int
    progress_percentage: float
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    recipients: list[CertificateOutcomeResponse]
