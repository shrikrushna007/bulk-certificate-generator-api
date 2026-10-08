from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.models import CertificateJob, CertificateRecord, CertificateStatus, JobStatus
from app.db.session import Database
from app.schemas.jobs import JobCreateRequest
from app.services.certificate_renderer import CertificateRenderer
from app.services.storage import CertificateStorage
from app.services.validation import validate_recipient

logger = logging.getLogger(__name__)


class JobService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_job(self, request: JobCreateRequest) -> CertificateJob:
        job = CertificateJob(
            certificate_title=request.certificate.title,
            issuing_organization=request.certificate.issuing_organization,
            issue_date=request.certificate.issue_date,
            total_count=len(request.recipients),
            success_count=0,
            failure_count=0,
        )
        for index, recipient_input in enumerate(request.recipients):
            recipient = validate_recipient(recipient_input.name, recipient_input.email)
            status = CertificateStatus.FAILED if recipient.error else CertificateStatus.PENDING
            job.certificates.append(
                CertificateRecord(
                    recipient_index=index,
                    recipient_name=recipient.name,
                    recipient_email=recipient.email,
                    status=status,
                    error_message=recipient.error,
                )
            )
            if recipient.error:
                job.failure_count += 1

        with self.database.session() as session:
            session.add(job)
            session.commit()
            session.refresh(job)
        return job

    def get_job(self, job_id: str) -> CertificateJob | None:
        with self.database.session() as session:
            statement = (
                select(CertificateJob)
                .where(CertificateJob.id == job_id)
                .options(selectinload(CertificateJob.certificates))
            )
            return session.scalar(statement)

    def get_certificate(self, job_id: str, certificate_id: str) -> CertificateRecord | None:
        with self.database.session() as session:
            statement = select(CertificateRecord).where(
                CertificateRecord.id == certificate_id,
                CertificateRecord.job_id == job_id,
            )
            return session.scalar(statement)


class JobProcessor:
    """Processes pending certificates sequentially with recipient-level failure isolation."""

    def __init__(
        self,
        database: Database,
        storage: CertificateStorage,
        renderer: CertificateRenderer,
    ) -> None:
        self.database = database
        self.storage = storage
        self.renderer = renderer

    def process(self, job_id: str) -> None:
        try:
            self._mark_processing(job_id)
            certificate_ids = self._pending_certificate_ids(job_id)
            for certificate_id in certificate_ids:
                self._process_one(job_id, certificate_id)
            self._finalize(job_id)
        except Exception:
            logger.exception("Unexpected job processing failure", extra={"job_id": job_id})
            self._fail_remaining(job_id)

    def _mark_processing(self, job_id: str) -> None:
        with self.database.session() as session:
            job = session.get(CertificateJob, job_id)
            if job is None:
                return
            job.status = JobStatus.PROCESSING
            job.started_at = datetime.now(UTC)
            session.commit()

    def _pending_certificate_ids(self, job_id: str) -> list[str]:
        with self.database.session() as session:
            statement = (
                select(CertificateRecord.id)
                .where(
                    CertificateRecord.job_id == job_id,
                    CertificateRecord.status == CertificateStatus.PENDING,
                )
                .order_by(CertificateRecord.recipient_index)
            )
            return list(session.scalars(statement))

    def _process_one(self, job_id: str, certificate_id: str) -> None:
        with self.database.session() as session:
            certificate = session.get(CertificateRecord, certificate_id)
            job = session.get(CertificateJob, job_id)
            if (
                certificate is None
                or job is None
                or certificate.status != CertificateStatus.PENDING
            ):
                return
            relative_path = self.storage.relative_path(job_id, certificate_id)
            destination = self.storage.resolve(relative_path)
            try:
                self.renderer.render(
                    destination=destination,
                    recipient_name=certificate.recipient_name or "",
                    certificate_title=job.certificate_title,
                    issuing_organization=job.issuing_organization,
                    issue_date=job.issue_date.isoformat(),
                    certificate_id=certificate.id,
                )
            except Exception:
                logger.exception(
                    "Certificate rendering failed",
                    extra={"job_id": job_id, "certificate_id": certificate_id},
                )
                destination.unlink(missing_ok=True)
                certificate.status = CertificateStatus.FAILED
                certificate.error_message = "Certificate generation failed."
                job.failure_count += 1
            else:
                certificate.status = CertificateStatus.COMPLETED
                certificate.file_path = relative_path.as_posix()
                certificate.error_message = None
                job.success_count += 1
            session.commit()

    def _finalize(self, job_id: str) -> None:
        with self.database.session() as session:
            job = session.get(CertificateJob, job_id)
            if job is None:
                return
            if job.success_count == job.total_count:
                job.status = JobStatus.COMPLETED
            elif job.success_count > 0:
                job.status = JobStatus.PARTIALLY_COMPLETED
            else:
                job.status = JobStatus.FAILED
            job.completed_at = datetime.now(UTC)
            session.commit()

    def _fail_remaining(self, job_id: str) -> None:
        with self.database.session() as session:
            job = session.get(CertificateJob, job_id)
            if job is None:
                return
            pending = list(
                session.scalars(
                    select(CertificateRecord).where(
                        CertificateRecord.job_id == job_id,
                        CertificateRecord.status == CertificateStatus.PENDING,
                    )
                )
            )
            for certificate in pending:
                certificate.status = CertificateStatus.FAILED
                certificate.error_message = "Job processing stopped unexpectedly."
            job.failure_count += len(pending)
            job.status = JobStatus.PARTIALLY_COMPLETED if job.success_count else JobStatus.FAILED
            job.completed_at = datetime.now(UTC)
            session.commit()
