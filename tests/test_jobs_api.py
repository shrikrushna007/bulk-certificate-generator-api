from __future__ import annotations

from io import BytesIO
from unittest.mock import Mock

from pypdf import PdfReader
from sqlalchemy.exc import SQLAlchemyError


def create_job(client, payload: dict) -> tuple[dict, dict]:
    creation = client.post("/api/v1/jobs", json=payload)
    assert creation.status_code == 202
    created = creation.json()
    status = client.get(created["status_url"])
    assert status.status_code == 200
    return created, status.json()


def test_health_check(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_creates_and_completes_bulk_job(client, valid_payload) -> None:
    created, job = create_job(client, valid_payload)

    assert created["status"] == "PENDING"
    assert created["total_recipients"] == 2
    assert job["status"] == "COMPLETED"
    assert job["successful_generations"] == 2
    assert job["failed_generations"] == 0
    assert job["pending_generations"] == 0
    assert job["progress_percentage"] == 100.0
    assert all(item["status"] == "COMPLETED" for item in job["recipients"])
    assert all(item["download_url"] for item in job["recipients"])


def test_generated_pdf_is_retrievable_and_contains_expected_text(client, valid_payload) -> None:
    _, job = create_job(client, valid_payload)
    certificate = job["recipients"][0]

    response = client.get(certificate["download_url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    reader = PdfReader(BytesIO(response.content))
    extracted = "".join(page.extract_text() or "" for page in reader.pages)
    assert "Aarav Sharma" in extracted
    assert "Backend Engineering Foundations" in extracted
    assert "AEREO Learning" in extracted
    assert certificate["certificate_id"] in extracted


def test_invalid_recipient_isolated_from_valid_recipient(client, valid_payload) -> None:
    valid_payload["recipients"].insert(0, {"name": "   ", "email": "invalid"})

    _, job = create_job(client, valid_payload)

    assert job["status"] == "PARTIALLY_COMPLETED"
    assert job["successful_generations"] == 2
    assert job["failed_generations"] == 1
    assert job["pending_generations"] == 0
    invalid = job["recipients"][0]
    assert invalid["status"] == "FAILED"
    assert invalid["error_message"] == "Recipient name must not be blank."
    assert invalid["download_url"] is None


def test_missing_and_wrong_type_names_are_individual_failures(client, valid_payload) -> None:
    valid_payload["recipients"] = [{}, {"name": 42}, {"name": "Valid Recipient"}]

    _, job = create_job(client, valid_payload)

    assert job["status"] == "PARTIALLY_COMPLETED"
    assert job["successful_generations"] == 1
    assert job["failed_generations"] == 2
    assert [item["status"] for item in job["recipients"]] == ["FAILED", "FAILED", "COMPLETED"]


def test_all_invalid_recipients_produce_failed_job(client, valid_payload) -> None:
    valid_payload["recipients"] = [{"name": ""}, {"name": None}]

    _, job = create_job(client, valid_payload)

    assert job["status"] == "FAILED"
    assert job["successful_generations"] == 0
    assert job["failed_generations"] == 2
    assert job["progress_percentage"] == 100.0


def test_duplicate_names_generate_distinct_certificates(client, valid_payload) -> None:
    valid_payload["recipients"] = [{"name": "Same Name"}, {"name": "Same Name"}]

    _, job = create_job(client, valid_payload)

    ids = [item["certificate_id"] for item in job["recipients"]]
    assert len(set(ids)) == 2
    assert job["successful_generations"] == 2


def test_long_recipient_name_generates_valid_pdf(client, valid_payload) -> None:
    long_name = "Alexandria " + "Montgomery-Worthington " * 7
    valid_payload["recipients"] = [{"name": long_name.strip()}]

    _, job = create_job(client, valid_payload)

    assert job["status"] == "COMPLETED"
    response = client.get(job["recipients"][0]["download_url"])
    assert response.status_code == 200
    assert len(PdfReader(BytesIO(response.content)).pages) == 1


def test_multiple_jobs_are_independent(client, valid_payload) -> None:
    first_created, first = create_job(client, valid_payload)
    second_payload = {
        **valid_payload,
        "recipients": [{"name": "Independent Recipient"}],
    }
    second_created, second = create_job(client, second_payload)

    assert first_created["job_id"] != second_created["job_id"]
    assert first["total_recipients"] == 2
    assert second["total_recipients"] == 1


def test_unknown_job_returns_404(client) -> None:
    response = client.get("/api/v1/jobs/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"


def test_unknown_certificate_returns_404(client, valid_payload) -> None:
    created, _ = create_job(client, valid_payload)
    response = client.get(
        f"/api/v1/jobs/{created['job_id']}/certificates/00000000-0000-0000-0000-000000000000"
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "certificate_not_found"


def test_cross_job_certificate_access_is_rejected(client, valid_payload) -> None:
    first_created, first = create_job(client, valid_payload)
    second_created, _ = create_job(
        client,
        {**valid_payload, "recipients": [{"name": "Another Recipient"}]},
    )
    certificate_id = first["recipients"][0]["certificate_id"]

    response = client.get(f"/api/v1/jobs/{second_created['job_id']}/certificates/{certificate_id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "certificate_not_found"
    assert first_created["job_id"] != second_created["job_id"]


def test_missing_generated_file_returns_410(client, app, valid_payload) -> None:
    _, job = create_job(client, valid_payload)
    certificate = job["recipients"][0]
    record = app.state.job_service.get_certificate(job["job_id"], certificate["certificate_id"])
    assert record is not None and record.file_path is not None
    app.state.storage.resolve(record.file_path).unlink()

    response = client.get(certificate["download_url"])

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "certificate_file_missing"


def test_failed_certificate_cannot_be_downloaded(client, valid_payload) -> None:
    valid_payload["recipients"] = [{"name": ""}]
    _, job = create_job(client, valid_payload)
    certificate_id = job["recipients"][0]["certificate_id"]

    response = client.get(f"/api/v1/jobs/{job['job_id']}/certificates/{certificate_id}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "certificate_unavailable"


def test_generated_files_use_confined_identifier_based_paths(client, app, valid_payload) -> None:
    valid_payload["recipients"] = [{"name": "../../unsafe filename"}]
    _, job = create_job(client, valid_payload)
    certificate = job["recipients"][0]
    record = app.state.job_service.get_certificate(job["job_id"], certificate["certificate_id"])
    assert record is not None and record.file_path is not None
    resolved = app.state.storage.resolve(record.file_path)

    assert resolved.is_file()
    assert app.state.storage.root in resolved.parents
    assert "unsafe filename" not in resolved.name


def test_database_failure_returns_safe_service_error(client, app, valid_payload) -> None:
    app.state.job_service.create_job = Mock(side_effect=SQLAlchemyError("sensitive detail"))

    response = client.post("/api/v1/jobs", json=valid_payload)

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "database_unavailable",
            "message": "The database operation could not be completed.",
        }
    }
    assert "sensitive detail" not in response.text
