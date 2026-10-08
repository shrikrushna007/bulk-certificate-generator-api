from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import insert, select, update

from app.db.models import CertificateJob, CertificateRecord, CertificateStatus, JobStatus
from app.db.session import Database
from app.schemas.jobs import JobCreateRequest
from app.services.certificate_renderer import CertificateRenderer
from app.services.storage import CertificateStorage
from app.services.validation import validate_recipient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobRenderContext:
    job_id: str
    certificate_title: str
    issuing_organization: str
    issue_date: str


@dataclass(frozen=True)
class RenderItem:
    certificate_id: str
    recipient_name: str


@dataclass(frozen=True)
class RenderOutcome:
    certificate_id: str
    succeeded: bool
    relative_path: str | None


class JobService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_job(self, request: JobCreateRequest) -> CertificateJob:
        job_id = str(uuid.uuid4())
        job = CertificateJob(
            id=job_id,
            certificate_title=request.certificate.title,
            issuing_organization=request.certificate.issuing_organization,
            issue_date=request.certificate.issue_date,
            total_count=len(request.recipients),
            success_count=0,
            failure_count=0,
        )
        records: list[dict[str, object]] = []
        for index, recipient_input in enumerate(request.recipients):
            recipient = validate_recipient(recipient_input.name, recipient_input.email)
            status = CertificateStatus.FAILED if recipient.error else CertificateStatus.PENDING
            records.append(
                {
                    "id": str(uuid.uuid4()),
                    "job_id": job_id,
                    "recipient_index": index,
                    "recipient_name": recipient.name,
                    "recipient_email": recipient.email,
                    "status": status,
                    "error_message": recipient.error,
                    "file_path": None,
                }
            )
            if recipient.error:
                job.failure_count += 1

        with self.database.session() as session:
            session.add(job)
            session.flush()
            session.execute(insert(CertificateRecord), records)
            session.commit()
            session.refresh(job)
        return job

    def get_job(self, job_id: str) -> CertificateJob | None:
        with self.database.session() as session:
            return session.get(CertificateJob, job_id)

    def list_certificates(self, job_id: str, offset: int, limit: int) -> list[CertificateRecord]:
        with self.database.session() as session:
            statement = (
                select(CertificateRecord)
                .where(CertificateRecord.job_id == job_id)
                .order_by(CertificateRecord.recipient_index)
                .offset(offset)
                .limit(limit)
            )
            return list(session.scalars(statement))

    def get_certificate(self, job_id: str, certificate_id: str) -> CertificateRecord | None:
        with self.database.session() as session:
            statement = select(CertificateRecord).where(
                CertificateRecord.id == certificate_id,
                CertificateRecord.job_id == job_id,
            )
            return session.scalar(statement)


class JobProcessor:
    """Renders bounded concurrent batches and persists results through one database writer."""

    def __init__(
        self,
        database: Database,
        storage: CertificateStorage,
        renderer: CertificateRenderer,
        *,
        render_workers: int = 4,
        batch_size: int = 100,
    ) -> None:
        self.database = database
        self.storage = storage
        self.renderer = renderer
        self.render_workers = render_workers
        self.batch_size = batch_size
        self.executor = ThreadPoolExecutor(
            max_workers=self.render_workers,
            thread_name_prefix="certificate-render",
        )

    def process(self, job_id: str) -> None:
        try:
            context = self._start_job(job_id)
            if context is None:
                return
            while items := self._pending_batch(job_id):
                outcomes = self._render_batch(self.executor, context, items)
                try:
                    self._persist_outcomes(job_id, outcomes)
                except Exception:
                    self._discard_generated(outcomes)
                    raise
            self._finalize(job_id)
        except Exception:
            logger.exception("Unexpected job processing failure", extra={"job_id": job_id})
            self._fail_remaining(job_id)

    def close(self) -> None:
        self.executor.shutdown(wait=True)

    def _start_job(self, job_id: str) -> JobRenderContext | None:
        with self.database.session() as session:
            job = session.get(CertificateJob, job_id)
            if job is None:
                return None
            job.status = JobStatus.PROCESSING
            job.started_at = datetime.now(UTC)
            session.commit()
            return JobRenderContext(
                job_id=job.id,
                certificate_title=job.certificate_title,
                issuing_organization=job.issuing_organization,
                issue_date=job.issue_date.isoformat(),
            )

    def _pending_batch(self, job_id: str) -> list[RenderItem]:
        with self.database.session() as session:
            statement = (
                select(CertificateRecord.id, CertificateRecord.recipient_name)
                .where(
                    CertificateRecord.job_id == job_id,
                    CertificateRecord.status == CertificateStatus.PENDING,
                )
                .order_by(CertificateRecord.recipient_index)
                .limit(self.batch_size)
            )
            return [
                RenderItem(certificate_id=certificate_id, recipient_name=recipient_name or "")
                for certificate_id, recipient_name in session.execute(statement)
            ]

    def _render_batch(
        self,
        executor: ThreadPoolExecutor,
        context: JobRenderContext,
        items: list[RenderItem],
    ) -> list[RenderOutcome]:
        futures = [executor.submit(self._render_one, context, item) for item in items]
        return [future.result() for future in as_completed(futures)]

    def _render_one(self, context: JobRenderContext, item: RenderItem) -> RenderOutcome:
        relative_path = self.storage.relative_path(context.job_id, item.certificate_id)
        destination = self.storage.resolve(relative_path)
        try:
            self.renderer.render(
                destination=destination,
                recipient_name=item.recipient_name,
                certificate_title=context.certificate_title,
                issuing_organization=context.issuing_organization,
                issue_date=context.issue_date,
                certificate_id=item.certificate_id,
            )
        except Exception:
            logger.exception(
                "Certificate rendering failed",
                extra={
                    "job_id": context.job_id,
                    "certificate_id": item.certificate_id,
                },
            )
            destination.unlink(missing_ok=True)
            return RenderOutcome(item.certificate_id, False, None)
        return RenderOutcome(item.certificate_id, True, relative_path.as_posix())

    def _persist_outcomes(self, job_id: str, outcomes: list[RenderOutcome]) -> None:
        successful = [outcome for outcome in outcomes if outcome.succeeded]
        failed = [outcome for outcome in outcomes if not outcome.succeeded]
        with self.database.session() as session:
            job = session.get(CertificateJob, job_id)
            if job is None:
                raise RuntimeError("Certificate job disappeared during processing.")
            if successful:
                session.execute(
                    update(CertificateRecord),
                    [
                        {
                            "id": outcome.certificate_id,
                            "status": CertificateStatus.COMPLETED,
                            "file_path": outcome.relative_path,
                            "error_message": None,
                        }
                        for outcome in successful
                    ],
                )
            if failed:
                session.execute(
                    update(CertificateRecord),
                    [
                        {
                            "id": outcome.certificate_id,
                            "status": CertificateStatus.FAILED,
                            "file_path": None,
                            "error_message": "Certificate generation failed.",
                        }
                        for outcome in failed
                    ],
                )
            job.success_count += len(successful)
            job.failure_count += len(failed)
            session.commit()

    def _discard_generated(self, outcomes: list[RenderOutcome]) -> None:
        for outcome in outcomes:
            if outcome.relative_path:
                self.storage.resolve(outcome.relative_path).unlink(missing_ok=True)

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
            result = session.execute(
                update(CertificateRecord)
                .where(
                    CertificateRecord.job_id == job_id,
                    CertificateRecord.status == CertificateStatus.PENDING,
                )
                .values(
                    status=CertificateStatus.FAILED,
                    error_message="Job processing stopped unexpectedly.",
                )
            )
            job.failure_count += result.rowcount
            job.status = JobStatus.PARTIALLY_COMPLETED if job.success_count else JobStatus.FAILED
            job.completed_at = datetime.now(UTC)
            session.commit()
