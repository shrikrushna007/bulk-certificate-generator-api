from contextlib import contextmanager

from app.schemas.jobs import JobCreateRequest


def test_sqlite_connections_use_operational_pragmas(app) -> None:
    with app.state.database.engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()

    assert journal_mode == "wal"
    assert foreign_keys == 1
    assert busy_timeout == 5000


def test_certificate_iteration_releases_session_before_yielding(
    app, valid_payload, monkeypatch
) -> None:
    job = app.state.job_service.create_job(JobCreateRequest.model_validate(valid_payload)).job
    original_session = app.state.database.session
    active_sessions = 0

    @contextmanager
    def tracked_session():
        nonlocal active_sessions
        active_sessions += 1
        try:
            with original_session() as session:
                yield session
        finally:
            active_sessions -= 1

    monkeypatch.setattr(app.state.database, "session", tracked_session)

    certificates = app.state.job_service.iter_certificates(job.id, batch_size=1)
    first = next(certificates)

    assert first.recipient_index == 0
    assert active_sessions == 0
    assert len(list(certificates)) == 1
    assert active_sessions == 0
