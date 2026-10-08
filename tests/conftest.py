from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app


@pytest.fixture
def app(tmp_path: Path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        storage_dir=tmp_path / "certificates",
        max_recipients=5,
        log_level="WARNING",
    )
    application = create_app(settings)
    Base.metadata.create_all(application.state.database.engine)
    return application


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def valid_payload() -> dict:
    return {
        "certificate": {
            "title": "Backend Engineering Foundations",
            "issuing_organization": "AEREO Learning",
            "issue_date": "2026-10-08",
        },
        "recipients": [
            {"name": "Aarav Sharma", "email": "aarav@example.com"},
            {"name": "Meera Iyer", "email": "meera@example.com"},
        ],
    }
