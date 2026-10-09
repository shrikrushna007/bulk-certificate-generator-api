from __future__ import annotations

from celery import Celery

from app.core.config import Settings

settings = Settings()

celery_app = Celery(
    "certflow",
    broker=settings.celery_broker_url,
    include=["app.tasks"],
)
celery_app.conf.update(
    broker_connection_retry_on_startup=True,
    broker_transport_options={
        "visibility_timeout": max(settings.claim_timeout_seconds * 2, 3600),
    },
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_ignore_result=True,
    worker_disable_prefetch=True,
    worker_prefetch_multiplier=1,
    timezone="UTC",
    beat_schedule={
        "recover-stalled-certificate-jobs": {
            "task": "certflow.recover_stalled_jobs",
            "schedule": 30.0,
        }
    },
)
