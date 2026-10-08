from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import UTC, datetime
from zipfile import ZipFile

from sqlalchemy import func, select

from app.core.logging import JsonFormatter
from app.db.models import CertificateJob, CertificateRecord, CertificateStatus, JobStatus
from app.schemas.jobs import JobCreateRequest


def test_idempotency_replays_original_job(client, app, valid_payload) -> None:
    headers = {"Idempotency-Key": "enrollment-import-2026-10-08"}

    first = client.post("/api/v1/jobs", json=valid_payload, headers=headers)
    second = client.post("/api/v1/jobs", json=valid_payload, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert first.json()["idempotency_replayed"] is False
    assert second.json()["idempotency_replayed"] is True
    assert second.headers["Idempotency-Replayed"] == "true"
    with app.state.database.session() as session:
        assert session.scalar(select(func.count(CertificateJob.id))) == 1
        job = session.scalar(select(CertificateJob))
        assert job is not None
        expected_key_hash = hashlib.sha256(
            headers["Idempotency-Key"].encode()
        ).hexdigest()
        assert job.idempotency_key_hash == expected_key_hash
        assert headers["Idempotency-Key"] not in job.idempotency_key_hash


def test_idempotency_key_reuse_with_different_payload_returns_conflict(
    client, valid_payload
) -> None:
    headers = {"Idempotency-Key": "same-operation"}
    first = client.post("/api/v1/jobs", json=valid_payload, headers=headers)
    changed = {
        **valid_payload,
        "recipients": [{"name": "A different recipient"}],
    }

    second = client.post("/api/v1/jobs", json=changed, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_key_conflict"


def test_blank_idempotency_key_is_rejected(client, valid_payload) -> None:
    response = client.post(
        "/api/v1/jobs",
        json=valid_payload,
        headers={"Idempotency-Key": "   "},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_idempotency_key"


def test_certificate_checksum_and_integrity_verification(client, app, valid_payload) -> None:
    creation = client.post("/api/v1/jobs", json=valid_payload).json()
    job = client.get(creation["status_url"]).json()
    certificate = job["recipients"][0]
    verification_url = f"{certificate['download_url']}/verify"

    verified = client.get(verification_url)

    assert len(certificate["sha256"]) == 64
    assert verified.status_code == 200
    assert verified.json()["valid"] is True
    assert verified.json()["stored_sha256"] == certificate["sha256"]

    record = app.state.job_service.get_certificate(
        job["job_id"], certificate["certificate_id"]
    )
    assert record is not None and record.file_path is not None
    path = app.state.storage.resolve(record.file_path)
    with path.open("ab") as pdf:
        pdf.write(b"tampered")

    assert client.get(verification_url).json()["valid"] is False
    download = client.get(certificate["download_url"])
    assert download.status_code == 409
    assert download.json()["error"]["code"] == "certificate_integrity_check_failed"
    archive = client.get(f"/api/v1/jobs/{job['job_id']}/archive")
    assert archive.status_code == 409
    assert archive.json()["error"]["code"] == "certificate_integrity_check_failed"


def test_job_archive_contains_pdfs_and_complete_csv_report(client, valid_payload) -> None:
    valid_payload["recipients"] = [
        {"name": "=FORMULA()", "email": "success@example.com"},
        {"name": "   ", "email": "invalid@example.com"},
    ]
    creation = client.post("/api/v1/jobs", json=valid_payload).json()

    response = client.get(f"/api/v1/jobs/{creation['job_id']}/archive")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert names[0] == "report.csv"
        assert len([name for name in names if name.endswith(".pdf")]) == 1
        rows = list(csv.DictReader(io.StringIO(archive.read("report.csv").decode())))
    assert [row["status"] for row in rows] == ["COMPLETED", "FAILED"]
    assert rows[0]["recipient_name"] == "'=FORMULA()"
    assert rows[0]["sha256"]
    assert rows[1]["error_message"] == "Recipient name must not be blank."


def test_archive_rejects_unfinished_job(client, app, valid_payload) -> None:
    request = JobCreateRequest.model_validate(valid_payload)
    job = app.state.job_service.create_job(request).job

    response = client.get(f"/api/v1/jobs/{job.id}/archive")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_not_finished"


def test_archive_reports_missing_successful_file(client, app, valid_payload) -> None:
    creation = client.post("/api/v1/jobs", json=valid_payload).json()
    job = client.get(creation["status_url"]).json()
    certificate = job["recipients"][0]
    record = app.state.job_service.get_certificate(
        job["job_id"], certificate["certificate_id"]
    )
    assert record is not None and record.file_path is not None
    app.state.storage.resolve(record.file_path).unlink()

    response = client.get(f"/api/v1/jobs/{job['job_id']}/archive")

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "certificate_file_missing"


def test_recovery_resumes_only_pending_certificates(app, valid_payload) -> None:
    request = JobCreateRequest.model_validate(valid_payload)
    job = app.state.job_service.create_job(request).job
    records = app.state.job_service.list_certificates(job.id, 0, job.total_count)
    completed = records[0]
    relative_path = app.state.storage.relative_path(job.id, completed.id)
    destination = app.state.storage.resolve(relative_path)
    app.state.job_processor.renderer.render(
        destination=destination,
        recipient_name=completed.recipient_name or "",
        certificate_title=job.certificate_title,
        issuing_organization=job.issuing_organization,
        issue_date=job.issue_date.isoformat(),
        certificate_id=completed.id,
    )
    with app.state.database.session() as session:
        stored_job = session.get(CertificateJob, job.id)
        stored_record = session.get(CertificateRecord, completed.id)
        assert stored_job is not None and stored_record is not None
        stored_job.status = JobStatus.PROCESSING
        stored_job.success_count = 0  # Recovery recalculates persisted recipient outcomes.
        stored_job.started_at = datetime.now(UTC)
        stored_record.status = CertificateStatus.COMPLETED
        stored_record.file_path = relative_path.as_posix()
        stored_record.sha256 = app.state.storage.sha256(destination)
        session.commit()

    recovered_count = app.state.job_processor.recover_incomplete_jobs()

    recovered = app.state.job_service.get_job(job.id)
    outcomes = app.state.job_service.list_certificates(job.id, 0, job.total_count)
    assert recovered_count == 1
    assert recovered is not None
    assert recovered.status == JobStatus.COMPLETED
    assert recovered.success_count == 2
    assert recovered.failure_count == 0
    assert all(record.status == CertificateStatus.COMPLETED for record in outcomes)
    assert all(record.sha256 for record in outcomes)


class Recipient37FailureRenderer:
    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def render(self, **kwargs) -> None:
        if kwargs["recipient_name"] == "Recipient 37":
            raise RuntimeError("simulated recipient 37 failure")
        self.delegate.render(**kwargs)


def test_one_failure_in_100_recipients_preserves_99_downloads(
    client, app, valid_payload
) -> None:
    app.state.settings.max_recipients = 100
    app.state.job_processor.batch_size = 25
    app.state.job_processor.renderer = Recipient37FailureRenderer(
        app.state.job_processor.renderer
    )
    valid_payload["recipients"] = [
        {"name": f"Recipient {index}"} for index in range(1, 101)
    ]

    creation = client.post("/api/v1/jobs", json=valid_payload)
    job = client.get(
        creation.json()["status_url"], params={"recipient_limit": 100}
    ).json()

    assert job["status"] == "PARTIALLY_COMPLETED"
    assert job["successful_generations"] == 99
    assert job["failed_generations"] == 1
    assert job["pending_generations"] == 0
    assert job["progress_percentage"] == 100.0
    failed = [item for item in job["recipients"] if item["status"] == "FAILED"]
    successful = [item for item in job["recipients"] if item["status"] == "COMPLETED"]
    assert failed[0]["recipient_name"] == "Recipient 37"
    assert failed[0]["error_message"] == "Certificate generation failed."
    assert len(successful) == 99
    assert all(client.get(item["download_url"]).status_code == 200 for item in successful)


def test_json_log_formatter_includes_operational_context() -> None:
    import logging

    record = logging.LogRecord(
        name="app.services.jobs",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Certificate job processing completed",
        args=(),
        exc_info=None,
    )
    record.job_id = "job-123"
    record.duration_ms = 125.5

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["job_id"] == "job-123"
    assert payload["duration_ms"] == 125.5
