from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

from app.db.models import (
    ArchiveStatus,
    CertificateJob,
    CertificateRecord,
    CertificateStatus,
    JobStatus,
)
from app.schemas.jobs import JobCreateRequest
from app.services.certificate_renderer import CertificateRenderer
from app.services.distributed_jobs import DistributedJobProcessor


def _csv_file(recipient_count: int) -> io.BytesIO:
    rows = ["name,email"]
    rows.extend(
        f"CSV Recipient {index},recipient{index}@example.com"
        for index in range(recipient_count)
    )
    return io.BytesIO(("\n".join(rows) + "\n").encode())


def test_streaming_csv_import_creates_large_job_without_json_limit(client) -> None:
    response = client.post(
        "/api/v1/jobs/import",
        data={
            "title": "Scalable Systems",
            "issuing_organization": "AEREO Learning",
            "issue_date": "2026-10-09",
        },
        files={"recipients": ("recipients.csv", _csv_file(250), "text/csv")},
    )

    assert response.status_code == 202
    created = response.json()
    job = client.get(
        created["status_url"], params={"include_recipients": "false"}
    ).json()
    assert created["total_recipients"] == 250
    assert job["status"] == "COMPLETED"
    assert job["successful_generations"] == 250
    assert job["failed_generations"] == 0


def test_csv_import_is_idempotent(client) -> None:
    form = {
        "title": "Scalable Systems",
        "issuing_organization": "AEREO Learning",
        "issue_date": "2026-10-09",
    }
    headers = {"Idempotency-Key": "csv-import-1"}

    first = client.post(
        "/api/v1/jobs/import",
        data=form,
        files={"recipients": ("recipients.csv", _csv_file(3), "text/csv")},
        headers=headers,
    )
    second = client.post(
        "/api/v1/jobs/import",
        data=form,
        files={"recipients": ("recipients.csv", _csv_file(3), "text/csv")},
        headers=headers,
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["idempotency_replayed"] is True


def test_csv_import_rejects_missing_name_header(client) -> None:
    response = client.post(
        "/api/v1/jobs/import",
        data={
            "title": "Scalable Systems",
            "issuing_organization": "AEREO Learning",
            "issue_date": "2026-10-09",
        },
        files={"recipients": ("recipients.csv", b"email\na@example.com\n", "text/csv")},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_csv_headers"


def test_distributed_processor_claims_and_completes_batches(app, valid_payload) -> None:
    request = JobCreateRequest.model_validate(valid_payload)
    job = app.state.job_service.create_job(request).job
    processor = DistributedJobProcessor(
        app.state.database,
        app.state.storage,
        CertificateRenderer(),
        render_workers=2,
        batch_size=1,
        claim_timeout_seconds=60,
    )
    try:
        processed = processor.drain_job(job.id)
    finally:
        processor.close()

    stored = app.state.job_service.get_job(job.id)
    outcomes = app.state.job_service.list_certificates(job.id, 0, job.total_count)
    assert processed == 2
    assert stored is not None and stored.status == JobStatus.COMPLETED
    assert all(record.status == CertificateStatus.COMPLETED for record in outcomes)
    assert all(record.attempt_count == 1 for record in outcomes)
    assert all(
        record.file_path and app.state.storage.exists(record.file_path)
        for record in outcomes
    )


def test_stale_distributed_claim_is_recovered(app, valid_payload) -> None:
    request = JobCreateRequest.model_validate(valid_payload)
    job = app.state.job_service.create_job(request).job
    record = app.state.job_service.list_certificates(job.id, 0, 1)[0]
    expired = datetime.now(UTC) - timedelta(minutes=10)
    with app.state.database.session() as session:
        stored_job = session.get(CertificateJob, job.id)
        stored_record = session.get(CertificateRecord, record.id)
        assert stored_job is not None and stored_record is not None
        stored_job.status = JobStatus.PROCESSING
        stored_job.last_dispatched_at = expired
        stored_record.status = CertificateStatus.PROCESSING
        stored_record.claim_token = "expired-claim"
        stored_record.lease_expires_at = expired
        session.commit()

    processor = DistributedJobProcessor(
        app.state.database,
        app.state.storage,
        CertificateRenderer(),
        render_workers=1,
        batch_size=2,
        claim_timeout_seconds=60,
    )
    try:
        recovered = processor.reconcile_stalled_jobs()
    finally:
        processor.close()

    stored_job = app.state.job_service.get_job(job.id)
    stored_record = app.state.job_service.get_certificate(job.id, record.id)
    assert recovered == [job.id]
    assert stored_job is not None and stored_job.status == JobStatus.PENDING
    assert stored_record is not None
    assert stored_record.status == CertificateStatus.PENDING
    assert stored_record.claim_token is None
    assert stored_record.lease_expires_at is None


def test_archive_can_be_generated_as_background_work(client, valid_payload) -> None:
    created = client.post("/api/v1/jobs", json=valid_payload).json()

    requested = client.post(f"/api/v1/jobs/{created['job_id']}/archive")
    job = client.get(created["status_url"]).json()
    archive = client.get(f"/api/v1/jobs/{created['job_id']}/archive")

    assert requested.status_code == 202
    assert requested.json()["status"] == "PENDING"
    assert job["archive_status"] == "COMPLETED"
    assert job["archive_download_url"].endswith("/archive")
    assert archive.status_code == 200
    assert archive.headers["content-type"] == "application/zip"


def test_stale_archive_claim_is_recovered(app, valid_payload) -> None:
    request = JobCreateRequest.model_validate(valid_payload)
    job = app.state.job_service.create_job(request).job
    expired = datetime.now(UTC) - timedelta(minutes=10)
    with app.state.database.session() as session:
        stored_job = session.get(CertificateJob, job.id)
        assert stored_job is not None
        stored_job.archive_status = ArchiveStatus.PROCESSING
        stored_job.archive_claim_token = "expired-archive-claim"
        stored_job.archive_lease_expires_at = expired
        session.commit()

    recovered = app.state.archive_processor.recover_stalled()

    stored_job = app.state.job_service.get_job(job.id)
    assert recovered == [job.id]
    assert stored_job is not None
    assert stored_job.archive_status == ArchiveStatus.PENDING
    assert stored_job.archive_claim_token is None
    assert stored_job.archive_lease_expires_at is None
