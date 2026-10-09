from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import update

from app.celery_app import celery_app
from app.db.models import CertificateJob, JobStatus
from app.db.session import Database


class DispatchUnavailableError(Exception):
    """Raised when work cannot be submitted to the distributed queue."""


class DistributedDispatcher:
    def __init__(self, database: Database, parallelism: int) -> None:
        self.database = database
        self.parallelism = parallelism

    def dispatch(self, job_id: str) -> None:
        self._mark_dispatched(job_id, datetime.now(UTC))
        try:
            for _ in range(self.parallelism):
                celery_app.send_task("certflow.process_job", args=[job_id])
        except Exception as exc:
            self._mark_dispatched(job_id, None)
            raise DispatchUnavailableError from exc

    def dispatch_archive(self, job_id: str) -> None:
        try:
            celery_app.send_task("certflow.process_archive", args=[job_id])
        except Exception as exc:
            raise DispatchUnavailableError from exc

    def _mark_dispatched(self, job_id: str, dispatched_at: datetime | None) -> None:
        with self.database.session() as session:
            session.execute(
                update(CertificateJob)
                .where(
                    CertificateJob.id == job_id,
                    CertificateJob.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]),
                )
                .values(last_dispatched_at=dispatched_at)
            )
            session.commit()
