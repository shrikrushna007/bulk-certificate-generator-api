from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update

from app.db.models import ArchiveStatus, CertificateJob, JobStatus
from app.db.session import Database
from app.services.archive import JobArchiveBuilder
from app.services.jobs import JobService
from app.services.storage import ArtifactStorage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimedArchive:
    job: CertificateJob
    token: str


class ArchiveProcessor:
    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        archive_builder: JobArchiveBuilder,
        claim_timeout_seconds: int,
    ) -> None:
        self.database = database
        self.storage = storage
        self.archive_builder = archive_builder
        self.job_service = JobService(database)
        self.claim_timeout = timedelta(seconds=claim_timeout_seconds)

    def process(self, job_id: str) -> bool:
        claimed = self._claim(job_id)
        if claimed is None:
            return False
        archive = None
        try:
            archive = self.archive_builder.build(
                claimed.job,
                lambda: self.job_service.iter_certificates(job_id),
            )
            base_key = self.storage.archive_key(job_id)
            key = f"{base_key.removesuffix('.zip')}-{claimed.token}.zip"
            self.storage.put_fileobj(archive, key, "application/zip")
            if not self._complete(job_id, claimed.token, key):
                self.storage.delete(key)
            return True
        except Exception:
            self._release_after_failure(job_id, claimed.token)
            logger.exception("Certificate archive generation failed", extra={"job_id": job_id})
            raise
        finally:
            if archive is not None:
                archive.close()

    def recover_stalled(self) -> list[str]:
        now = datetime.now(UTC)
        with self.database.session() as session:
            job_ids = list(
                session.scalars(
                    select(CertificateJob.id)
                    .where(
                        CertificateJob.archive_status == ArchiveStatus.PROCESSING,
                        CertificateJob.archive_lease_expires_at < now,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            if job_ids:
                session.execute(
                    update(CertificateJob)
                    .where(CertificateJob.id.in_(job_ids))
                    .values(
                        archive_status=ArchiveStatus.PENDING,
                        archive_claim_token=None,
                        archive_lease_expires_at=None,
                        archive_error="Archive processing was interrupted and will resume.",
                    )
                )
            session.commit()
            return job_ids

    def _claim(self, job_id: str) -> ClaimedArchive | None:
        now = datetime.now(UTC)
        token = str(uuid.uuid4())
        with self.database.session() as session:
            claim = session.execute(
                update(CertificateJob)
                .where(
                    CertificateJob.id == job_id,
                    CertificateJob.status.in_(
                        [
                            JobStatus.COMPLETED,
                            JobStatus.PARTIALLY_COMPLETED,
                            JobStatus.FAILED,
                        ]
                    ),
                    or_(
                        CertificateJob.archive_status == ArchiveStatus.PENDING,
                        CertificateJob.archive_status == ArchiveStatus.FAILED,
                        and_(
                            CertificateJob.archive_status == ArchiveStatus.PROCESSING,
                            CertificateJob.archive_lease_expires_at < now,
                        ),
                    ),
                )
                .values(
                    archive_status=ArchiveStatus.PROCESSING,
                    archive_error=None,
                    archive_claim_token=token,
                    archive_lease_expires_at=now + self.claim_timeout,
                )
            )
            if claim.rowcount != 1:
                session.rollback()
                return None
            session.commit()
            job = session.get(CertificateJob, job_id)
            return ClaimedArchive(job, token) if job is not None else None

    def _complete(self, job_id: str, claim_token: str, key: str) -> bool:
        with self.database.session() as session:
            result = session.execute(
                update(CertificateJob)
                .where(
                    CertificateJob.id == job_id,
                    CertificateJob.archive_status == ArchiveStatus.PROCESSING,
                    CertificateJob.archive_claim_token == claim_token,
                )
                .values(
                    archive_status=ArchiveStatus.COMPLETED,
                    archive_path=key,
                    archive_error=None,
                    archive_claim_token=None,
                    archive_lease_expires_at=None,
                )
            )
            session.commit()
            return result.rowcount == 1

    def _release_after_failure(self, job_id: str, claim_token: str) -> None:
        with self.database.session() as session:
            session.execute(
                update(CertificateJob)
                .where(
                    CertificateJob.id == job_id,
                    CertificateJob.archive_status == ArchiveStatus.PROCESSING,
                    CertificateJob.archive_claim_token == claim_token,
                )
                .values(
                    archive_status=ArchiveStatus.FAILED,
                    archive_error="Archive generation failed. The request may be retried.",
                    archive_claim_token=None,
                    archive_lease_expires_at=None,
                )
            )
            session.commit()
