from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class JobStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"


class CertificateStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ArchiveStatus(StrEnum):
    NOT_REQUESTED = "NOT_REQUESTED"
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CertificateJob(Base):
    __tablename__ = "certificate_jobs"
    __table_args__ = (
        CheckConstraint("total_count >= 1", name="ck_jobs_total_positive"),
        CheckConstraint("success_count >= 0", name="ck_jobs_success_nonnegative"),
        CheckConstraint("failure_count >= 0", name="ck_jobs_failure_nonnegative"),
        CheckConstraint(
            "success_count + failure_count <= total_count", name="ck_jobs_counts_valid"
        ),
        Index("ix_certificate_jobs_status", "status"),
        Index("ix_certificate_jobs_created_at", "created_at"),
        Index("ix_certificate_jobs_dispatch", "status", "last_dispatched_at"),
        Index(
            "ux_certificate_jobs_idempotency_key_hash",
            "idempotency_key_hash",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=32), default=JobStatus.PENDING, nullable=False
    )
    certificate_title: Mapped[str] = mapped_column(String(200), nullable=False)
    issuing_organization: Mapped[str] = mapped_column(String(200), nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archive_status: Mapped[ArchiveStatus] = mapped_column(
        Enum(ArchiveStatus, native_enum=False, length=32),
        default=ArchiveStatus.NOT_REQUESTED,
        server_default=ArchiveStatus.NOT_REQUESTED.value,
        nullable=False,
    )
    archive_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    archive_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    archive_claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    archive_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    certificates: Mapped[list[CertificateRecord]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="CertificateRecord.recipient_index",
    )

    @property
    def pending_count(self) -> int:
        return self.total_count - self.success_count - self.failure_count

    @property
    def progress_percentage(self) -> float:
        processed = self.success_count + self.failure_count
        return round((processed / self.total_count) * 100, 2)


class CertificateRecord(Base):
    __tablename__ = "certificate_records"
    __table_args__ = (
        UniqueConstraint("job_id", "recipient_index", name="uq_certificate_job_recipient_index"),
        Index("ix_certificate_records_job_status", "job_id", "status"),
        Index(
            "ix_certificate_records_claimable",
            "job_id",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(
        ForeignKey("certificate_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_index: Mapped[int] = mapped_column(Integer, nullable=False)
    recipient_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    recipient_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    status: Mapped[CertificateStatus] = mapped_column(
        Enum(CertificateStatus, native_enum=False, length=32),
        default=CertificateStatus.PENDING,
        nullable=False,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    job: Mapped[CertificateJob] = relationship(back_populates="certificates")
