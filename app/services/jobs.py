from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    ArchiveStatus,
    CertificateJob,
    CertificateRecord,
    CertificateStatus,
    JobStatus,
)
from app.db.session import Database
from app.schemas.jobs import CertificateDetailsInput, JobCreateRequest, RecipientInput
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
    sha256: str | None


@dataclass(frozen=True)
class JobCreationResult:
    job: CertificateJob
    replayed: bool


@dataclass(frozen=True)
class JobCompletion:
    status: JobStatus
    total_count: int
    success_count: int
    failure_count: int


class IdempotencyConflictError(Exception):
    """Raised when an idempotency key is reused for a different request."""


class JobService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_job(
        self,
        request: JobCreateRequest,
        idempotency_key: str | None = None,
    ) -> JobCreationResult:
        request_hash = self._request_hash(request)
        return self.create_streamed_job(
            certificate=request.certificate,
            recipients=request.recipients,
            total_count=len(request.recipients),
            request_hash=request_hash,
            idempotency_key=idempotency_key,
        )

    def create_streamed_job(
        self,
        *,
        certificate: CertificateDetailsInput,
        recipients: Iterable[RecipientInput],
        total_count: int,
        request_hash: str,
        idempotency_key: str | None = None,
    ) -> JobCreationResult:
        key_hash = self._key_hash(idempotency_key) if idempotency_key else None

        if key_hash:
            existing = self._get_job_by_idempotency_key(key_hash)
            if existing is not None:
                self._ensure_matching_request(existing, request_hash)
                return JobCreationResult(existing, replayed=True)

        job_id = str(uuid.uuid4())
        job = CertificateJob(
            id=job_id,
            certificate_title=certificate.title,
            issuing_organization=certificate.issuing_organization,
            issue_date=certificate.issue_date,
            total_count=total_count,
            success_count=0,
            failure_count=0,
            idempotency_key_hash=key_hash,
            request_hash=request_hash,
        )
        try:
            with self.database.session() as session:
                session.add(job)
                session.flush()
                record_count = 0
                records: list[dict[str, object]] = []
                for index, recipient_input in enumerate(recipients):
                    recipient = validate_recipient(recipient_input.name, recipient_input.email)
                    record_status = (
                        CertificateStatus.FAILED if recipient.error else CertificateStatus.PENDING
                    )
                    records.append(
                        {
                            "id": str(uuid.uuid4()),
                            "job_id": job_id,
                            "recipient_index": index,
                            "recipient_name": recipient.name,
                            "recipient_email": recipient.email,
                            "status": record_status,
                            "error_message": recipient.error,
                            "file_path": None,
                            "sha256": None,
                        }
                    )
                    record_count += 1
                    if recipient.error:
                        job.failure_count += 1
                    if len(records) >= 1_000:
                        session.execute(insert(CertificateRecord), records)
                        records.clear()
                if records:
                    session.execute(insert(CertificateRecord), records)
                if record_count != total_count:
                    raise ValueError("Recipient stream changed while the job was created.")
                session.commit()
                session.refresh(job)
        except IntegrityError:
            if not key_hash:
                raise
            existing = self._get_job_by_idempotency_key(key_hash)
            if existing is None:
                raise
            self._ensure_matching_request(existing, request_hash)
            return JobCreationResult(existing, replayed=True)
        return JobCreationResult(job, replayed=False)

    @staticmethod
    def _request_hash(request: JobCreateRequest) -> str:
        canonical = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _key_hash(idempotency_key: str) -> str:
        return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()

    def _get_job_by_idempotency_key(self, key_hash: str) -> CertificateJob | None:
        with self.database.session() as session:
            return session.scalar(
                select(CertificateJob).where(CertificateJob.idempotency_key_hash == key_hash)
            )

    @staticmethod
    def _ensure_matching_request(job: CertificateJob, request_hash: str) -> None:
        if job.request_hash != request_hash:
            raise IdempotencyConflictError

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

    def iter_certificates(
        self,
        job_id: str,
        batch_size: int = 1_000,
    ) -> Iterator[CertificateRecord]:
        with self.database.session() as session:
            statement = (
                select(CertificateRecord)
                .where(CertificateRecord.job_id == job_id)
                .order_by(CertificateRecord.recipient_index)
                .execution_options(yield_per=batch_size)
            )
            yield from session.scalars(statement)

    def get_certificate(self, job_id: str, certificate_id: str) -> CertificateRecord | None:
        with self.database.session() as session:
            statement = select(CertificateRecord).where(
                CertificateRecord.id == certificate_id,
                CertificateRecord.job_id == job_id,
            )
            return session.scalar(statement)

    def request_archive(self, job_id: str) -> CertificateJob | None:
        with self.database.session() as session:
            job = session.get(CertificateJob, job_id, with_for_update=True)
            if job is None:
                return None
            if job.archive_status in {ArchiveStatus.NOT_REQUESTED, ArchiveStatus.FAILED}:
                job.archive_status = ArchiveStatus.PENDING
                job.archive_error = None
                job.archive_total_count = job.success_count
                job.archive_processed_count = 0
                job.archive_started_at = None
                job.archive_completed_at = None
                job.archive_claim_token = None
                job.archive_lease_expires_at = None
            session.commit()
            return job


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
        started = time.perf_counter()
        try:
            context = self._start_job(job_id)
            if context is None:
                return
            logger.info("Certificate job processing started", extra={"job_id": job_id})
            while items := self._pending_batch(job_id):
                outcomes = self._render_batch(self.executor, context, items)
                try:
                    self._persist_outcomes(job_id, outcomes)
                except Exception:
                    self._discard_generated(outcomes)
                    raise
            completion = self._finalize(job_id)
            if completion is not None:
                logger.info(
                    "Certificate job processing completed",
                    extra={
                        "job_id": job_id,
                        "status": completion.status.value,
                        "total_count": completion.total_count,
                        "success_count": completion.success_count,
                        "failure_count": completion.failure_count,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    },
                )
        except Exception:
            logger.exception("Unexpected job processing failure", extra={"job_id": job_id})
            self._fail_remaining(job_id)

    def close(self) -> None:
        self.executor.shutdown(wait=True)

    def recover_incomplete_jobs(self) -> int:
        job_ids: list[str] = []
        with self.database.session() as session:
            jobs = list(
                session.scalars(
                    select(CertificateJob).where(
                        CertificateJob.status.in_([JobStatus.PENDING, JobStatus.PROCESSING])
                    )
                )
            )
            for job in jobs:
                counts = {
                    status: count
                    for status, count in session.execute(
                        select(CertificateRecord.status, func.count(CertificateRecord.id))
                        .where(CertificateRecord.job_id == job.id)
                        .group_by(CertificateRecord.status)
                    )
                }
                job.success_count = counts.get(CertificateStatus.COMPLETED, 0)
                job.failure_count = counts.get(CertificateStatus.FAILED, 0)
                if job.success_count + job.failure_count == job.total_count:
                    self._set_terminal_status(job)
                    job.completed_at = job.completed_at or datetime.now(UTC)
                else:
                    job.status = JobStatus.PENDING
                    job.started_at = None
                    job.completed_at = None
                    job_ids.append(job.id)
            session.commit()

        if job_ids:
            logger.info(
                "Recovering incomplete certificate jobs",
                extra={"recovered_job_count": len(job_ids)},
            )
        for job_id in job_ids:
            self.process(job_id)
        return len(job_ids)

    def _start_job(self, job_id: str) -> JobRenderContext | None:
        with self.database.session() as session:
            job = session.get(CertificateJob, job_id)
            if job is None or job.status != JobStatus.PENDING:
                return None
            started_at = datetime.now(UTC)
            claim = session.execute(
                update(CertificateJob)
                .where(
                    CertificateJob.id == job_id,
                    CertificateJob.status == JobStatus.PENDING,
                )
                .values(status=JobStatus.PROCESSING, started_at=started_at)
            )
            if claim.rowcount != 1:
                session.rollback()
                return None
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
            return RenderOutcome(item.certificate_id, False, None, None)
        return RenderOutcome(
            item.certificate_id,
            True,
            relative_path.as_posix(),
            self.storage.sha256(destination),
        )

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
                            "sha256": outcome.sha256,
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
                            "sha256": None,
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

    def _finalize(self, job_id: str) -> JobCompletion | None:
        with self.database.session() as session:
            job = session.get(CertificateJob, job_id)
            if job is None:
                return None
            self._set_terminal_status(job)
            job.completed_at = datetime.now(UTC)
            session.commit()
            return JobCompletion(
                status=job.status,
                total_count=job.total_count,
                success_count=job.success_count,
                failure_count=job.failure_count,
            )

    @staticmethod
    def _set_terminal_status(job: CertificateJob) -> None:
        if job.success_count == job.total_count:
            job.status = JobStatus.COMPLETED
        elif job.success_count > 0:
            job.status = JobStatus.PARTIALLY_COMPLETED
        else:
            job.status = JobStatus.FAILED

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
