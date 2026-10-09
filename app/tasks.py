from __future__ import annotations

from functools import lru_cache

from app.celery_app import celery_app
from app.core.config import Settings
from app.db.session import Database
from app.services.archive import JobArchiveBuilder
from app.services.archive_jobs import ArchiveProcessor
from app.services.certificate_renderer import CertificateRenderer
from app.services.dispatch import DistributedDispatcher
from app.services.distributed_jobs import DistributedJobProcessor
from app.services.storage import create_storage


@lru_cache(maxsize=1)
def _components() -> tuple[
    DistributedJobProcessor,
    DistributedDispatcher,
    ArchiveProcessor,
]:
    settings = Settings()
    if settings.processing_mode != "distributed":
        raise RuntimeError("Celery workers require PROCESSING_MODE=distributed.")
    database = Database(settings.database_url)
    storage = create_storage(settings)
    storage.ensure_ready()
    processor = DistributedJobProcessor(
        database,
        storage,
        CertificateRenderer(),
        render_workers=settings.render_workers,
        batch_size=settings.processing_batch_size,
        claim_timeout_seconds=settings.claim_timeout_seconds,
    )
    dispatcher = DistributedDispatcher(database, settings.distributed_job_parallelism)
    archive_processor = ArchiveProcessor(
        database,
        storage,
        JobArchiveBuilder(storage),
        settings.claim_timeout_seconds,
    )
    return processor, dispatcher, archive_processor


@celery_app.task(
    bind=True,
    name="certflow.process_job",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def process_job(self, job_id: str) -> int:
    del self
    processor, _, _ = _components()
    return processor.drain_job(job_id)


@celery_app.task(
    name="certflow.recover_stalled_jobs",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=5,
)
def recover_stalled_jobs() -> int:
    processor, dispatcher, archive_processor = _components()
    job_ids = processor.reconcile_stalled_jobs()
    for job_id in job_ids:
        dispatcher.dispatch(job_id)
    archive_job_ids = archive_processor.recover_stalled()
    for job_id in archive_job_ids:
        dispatcher.dispatch_archive(job_id)
    return len(job_ids) + len(archive_job_ids)


@celery_app.task(
    name="certflow.process_archive",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def process_archive(job_id: str) -> bool:
    _, _, archive_processor = _components()
    return archive_processor.process(job_id)
