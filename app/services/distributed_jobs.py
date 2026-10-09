from __future__ import annotations

import hashlib
import logging
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, func, or_, select, update

from app.db.models import CertificateJob, CertificateRecord, CertificateStatus, JobStatus
from app.db.session import Database
from app.services.certificate_renderer import CertificateRenderer
from app.services.jobs import JobCompletion, JobRenderContext, RenderItem, RenderOutcome
from app.services.storage import ArtifactStorage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimedBatch:
    token: str
    context: JobRenderContext
    items: list[RenderItem]


class DistributedJobProcessor:
    """Claims recipient batches so multiple processes can safely render one job."""

    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        renderer: CertificateRenderer,
        *,
        render_workers: int,
        batch_size: int,
        claim_timeout_seconds: int,
    ) -> None:
        self.database = database
        self.storage = storage
        self.renderer = renderer
        self.batch_size = batch_size
        self.claim_timeout = timedelta(seconds=claim_timeout_seconds)
        self.executor = ThreadPoolExecutor(
            max_workers=render_workers,
            thread_name_prefix="distributed-certificate-render",
        )

    def drain_job(self, job_id: str) -> int:
        started = time.perf_counter()
        processed = 0
        while batch := self._claim_batch(job_id):
            outcomes = self._render_batch(batch)
            processed += self._persist_outcomes(job_id, batch.token, outcomes)
        completion = self._finalize_if_done(job_id)
        if completion is not None:
            logger.info(
                "Distributed certificate job processing completed",
                extra={
                    "job_id": job_id,
                    "status": completion.status.value,
                    "total_count": completion.total_count,
                    "success_count": completion.success_count,
                    "failure_count": completion.failure_count,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
        return processed

    def close(self) -> None:
        self.executor.shutdown(wait=True)

    def reconcile_stalled_jobs(self) -> list[str]:
        now = datetime.now(UTC)
        cutoff = now - self.claim_timeout
        recovered: list[str] = []
        with self.database.session() as session:
            jobs = list(
                session.scalars(
                    select(CertificateJob)
                    .where(
                        CertificateJob.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]),
                        or_(
                            CertificateJob.last_dispatched_at.is_(None),
                            CertificateJob.last_dispatched_at < cutoff,
                        ),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for job in jobs:
                session.execute(
                    update(CertificateRecord)
                    .where(
                        CertificateRecord.job_id == job.id,
                        CertificateRecord.status == CertificateStatus.PROCESSING,
                        CertificateRecord.lease_expires_at < now,
                    )
                    .values(
                        status=CertificateStatus.PENDING,
                        claim_token=None,
                        lease_expires_at=None,
                    )
                )
                counts = {
                    record_status: count
                    for record_status, count in session.execute(
                        select(CertificateRecord.status, func.count(CertificateRecord.id))
                        .where(CertificateRecord.job_id == job.id)
                        .group_by(CertificateRecord.status)
                    )
                }
                job.success_count = counts.get(CertificateStatus.COMPLETED, 0)
                job.failure_count = counts.get(CertificateStatus.FAILED, 0)
                if job.success_count + job.failure_count == job.total_count:
                    self._set_terminal_status(job)
                    job.completed_at = job.completed_at or now
                else:
                    job.status = JobStatus.PENDING
                    job.last_dispatched_at = now
                    recovered.append(job.id)
            session.commit()
        return recovered

    def mark_dispatched(self, job_id: str) -> None:
        with self.database.session() as session:
            session.execute(
                update(CertificateJob)
                .where(
                    CertificateJob.id == job_id,
                    CertificateJob.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]),
                )
                .values(last_dispatched_at=datetime.now(UTC))
            )
            session.commit()

    def _claim_batch(self, job_id: str) -> ClaimedBatch | None:
        now = datetime.now(UTC)
        token = str(uuid.uuid4())
        with self.database.session() as session:
            job = session.get(CertificateJob, job_id, with_for_update=True)
            if job is None or job.status not in {JobStatus.PENDING, JobStatus.PROCESSING}:
                return None
            statement = (
                select(CertificateRecord)
                .where(
                    CertificateRecord.job_id == job_id,
                    or_(
                        CertificateRecord.status == CertificateStatus.PENDING,
                        and_(
                            CertificateRecord.status == CertificateStatus.PROCESSING,
                            CertificateRecord.lease_expires_at < now,
                        ),
                    ),
                )
                .order_by(CertificateRecord.recipient_index)
                .limit(self.batch_size)
                .with_for_update(skip_locked=True)
            )
            records = list(session.scalars(statement))
            if not records:
                return None
            for record in records:
                record.status = CertificateStatus.PROCESSING
                record.claim_token = token
                record.lease_expires_at = now + self.claim_timeout
                record.attempt_count += 1
            job.status = JobStatus.PROCESSING
            job.started_at = job.started_at or now
            job.last_dispatched_at = now
            session.commit()
            return ClaimedBatch(
                token=token,
                context=JobRenderContext(
                    job_id=job.id,
                    certificate_title=job.certificate_title,
                    issuing_organization=job.issuing_organization,
                    issue_date=job.issue_date.isoformat(),
                ),
                items=[
                    RenderItem(
                        certificate_id=record.id,
                        recipient_name=record.recipient_name or "",
                    )
                    for record in records
                ],
            )

    def _render_batch(self, batch: ClaimedBatch) -> list[RenderOutcome]:
        futures = [
            self.executor.submit(self._render_one, batch.context, item, batch.token)
            for item in batch.items
        ]
        return [future.result() for future in as_completed(futures)]

    def _render_one(
        self,
        context: JobRenderContext,
        item: RenderItem,
        claim_token: str,
    ) -> RenderOutcome:
        base_key = self.storage.key(context.job_id, item.certificate_id)
        key = f"{base_key.removesuffix('.pdf')}-{claim_token}.pdf"
        with tempfile.TemporaryDirectory(prefix="certflow-") as temporary_dir:
            destination = Path(temporary_dir) / f"{item.certificate_id}.pdf"
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
                return RenderOutcome(item.certificate_id, False, None, None)
            sha256 = _sha256_file(destination)
            self.storage.put_file(destination, key, "application/pdf")
        return RenderOutcome(item.certificate_id, True, key, sha256)

    def _persist_outcomes(
        self,
        job_id: str,
        claim_token: str,
        outcomes: list[RenderOutcome],
    ) -> int:
        success_count = 0
        failure_count = 0
        accepted_ids: set[str] = set()
        with self.database.session() as session:
            job = session.get(CertificateJob, job_id, with_for_update=True)
            if job is None:
                raise RuntimeError("Certificate job disappeared during processing.")
            for outcome in outcomes:
                values = {
                    "status": (
                        CertificateStatus.COMPLETED
                        if outcome.succeeded
                        else CertificateStatus.FAILED
                    ),
                    "file_path": outcome.relative_path,
                    "error_message": (
                        None if outcome.succeeded else "Certificate generation failed."
                    ),
                    "sha256": outcome.sha256,
                    "claim_token": None,
                    "lease_expires_at": None,
                }
                result = session.execute(
                    update(CertificateRecord)
                    .where(
                        CertificateRecord.id == outcome.certificate_id,
                        CertificateRecord.job_id == job_id,
                        CertificateRecord.status == CertificateStatus.PROCESSING,
                        CertificateRecord.claim_token == claim_token,
                    )
                    .values(**values)
                )
                if result.rowcount == 1:
                    accepted_ids.add(outcome.certificate_id)
                    if outcome.succeeded:
                        success_count += 1
                    else:
                        failure_count += 1
            job.success_count += success_count
            job.failure_count += failure_count
            job.last_dispatched_at = datetime.now(UTC)
            if job.success_count + job.failure_count == job.total_count:
                self._set_terminal_status(job)
                job.completed_at = datetime.now(UTC)
            session.commit()
        for outcome in outcomes:
            if outcome.certificate_id in accepted_ids or not outcome.relative_path:
                continue
            try:
                self.storage.delete(outcome.relative_path)
            except Exception:
                logger.warning(
                    "Could not remove an unclaimed certificate artifact",
                    extra={"job_id": job_id, "certificate_id": outcome.certificate_id},
                    exc_info=True,
                )
        return success_count + failure_count

    def _finalize_if_done(self, job_id: str) -> JobCompletion | None:
        with self.database.session() as session:
            job = session.get(CertificateJob, job_id, with_for_update=True)
            if job is None:
                return None
            if job.success_count + job.failure_count != job.total_count:
                return None
            self._set_terminal_status(job)
            job.completed_at = job.completed_at or datetime.now(UTC)
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        while chunk := artifact.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
