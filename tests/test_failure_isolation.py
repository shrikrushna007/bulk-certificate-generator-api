from __future__ import annotations

from threading import Barrier, Lock, get_ident


class SelectiveFailureRenderer:
    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def render(self, **kwargs) -> None:
        if kwargs["recipient_name"] == "Rendering Failure":
            raise RuntimeError("simulated renderer failure")
        self.delegate.render(**kwargs)


class ConcurrentProbeRenderer:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.barrier = Barrier(2, timeout=5)
        self.lock = Lock()
        self.thread_ids: set[int] = set()

    def render(self, **kwargs) -> None:
        with self.lock:
            self.thread_ids.add(get_ident())
        self.barrier.wait()
        self.delegate.render(**kwargs)


def test_rendering_failure_does_not_stop_other_recipients(client, app, valid_payload) -> None:
    processor = app.state.job_processor
    processor.renderer = SelectiveFailureRenderer(processor.renderer)
    valid_payload["recipients"] = [
        {"name": "First Success"},
        {"name": "Rendering Failure"},
        {"name": "Second Success"},
    ]

    creation = client.post("/api/v1/jobs", json=valid_payload)
    job = client.get(creation.json()["status_url"]).json()

    assert job["status"] == "PARTIALLY_COMPLETED"
    assert job["successful_generations"] == 2
    assert job["failed_generations"] == 1
    assert job["pending_generations"] == 0
    assert job["progress_percentage"] == 100.0
    statuses = [recipient["status"] for recipient in job["recipients"]]
    assert statuses == ["COMPLETED", "FAILED", "COMPLETED"]
    assert job["recipients"][1]["error_message"] == "Certificate generation failed."


def test_batch_rendering_uses_bounded_concurrency(client, app, valid_payload) -> None:
    processor = app.state.job_processor
    probe = ConcurrentProbeRenderer(processor.renderer)
    processor.renderer = probe

    creation = client.post("/api/v1/jobs", json=valid_payload)
    job = client.get(creation.json()["status_url"]).json()

    assert job["status"] == "COMPLETED"
    assert len(probe.thread_ids) == 2


def test_unprocessed_job_exposes_pending_status_and_consistent_counts(app, valid_payload) -> None:
    from app.schemas.jobs import JobCreateRequest

    job = app.state.job_service.create_job(JobCreateRequest.model_validate(valid_payload))
    stored = app.state.job_service.get_job(job.id)

    assert stored is not None
    assert stored.status.value == "PENDING"
    assert stored.success_count == 0
    assert stored.failure_count == 0
    assert stored.pending_count == 2
    assert stored.progress_percentage == 0.0
