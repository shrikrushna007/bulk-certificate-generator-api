from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"certificate": {}, "recipients": [{"name": "Valid"}]},
        {
            "certificate": {
                "title": "",
                "issuing_organization": "Issuer",
                "issue_date": "2026-10-08",
            },
            "recipients": [{"name": "Valid"}],
        },
        {
            "certificate": {
                "title": "Course",
                "issuing_organization": "Issuer",
                "issue_date": "not-a-date",
            },
            "recipients": [{"name": "Valid"}],
        },
        {
            "certificate": {
                "title": "Course",
                "issuing_organization": "Issuer",
                "issue_date": "2026-10-08",
            },
            "recipients": [],
        },
        {
            "certificate": {
                "title": "Course",
                "issuing_organization": "Issuer",
                "issue_date": "2026-10-08",
            },
            "recipients": ["not-an-object"],
        },
    ],
)
def test_request_level_validation_returns_consistent_422(client, payload) -> None:
    response = client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "request_validation_error"
    assert body["error"]["details"]


def test_recipient_limit_is_enforced(client, valid_payload) -> None:
    valid_payload["recipients"] = [{"name": f"Recipient {index}"} for index in range(6)]

    response = client.post("/api/v1/jobs", json=valid_payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "recipient_limit_exceeded"


def test_extra_fields_are_rejected(client, valid_payload) -> None:
    valid_payload["unexpected"] = True

    response = client.post("/api/v1/jobs", json=valid_payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_invalid_email_is_an_individual_failure(client, valid_payload) -> None:
    valid_payload["recipients"] = [
        {"name": "Invalid Email", "email": "not-an-email"},
        {"name": "Valid Recipient", "email": "valid@example.com"},
    ]

    creation = client.post("/api/v1/jobs", json=valid_payload)
    job = client.get(creation.json()["status_url"]).json()

    assert job["status"] == "PARTIALLY_COMPLETED"
    assert job["recipients"][0]["error_message"] == "Recipient email is not valid."
    assert job["recipients"][1]["status"] == "COMPLETED"


def test_explicit_s3_credentials_must_be_complete() -> None:
    with pytest.raises(ValidationError, match="must be configured together"):
        Settings(s3_access_key_id="local-access-key")
