from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, BinaryIO

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse

from app.api.dependencies import get_job_service
from app.db.models import CertificateRecord, CertificateStatus, JobStatus
from app.schemas.jobs import (
    CertificateOutcomeResponse,
    CertificateVerificationResponse,
    JobCreatedResponse,
    JobCreateRequest,
    JobStatusResponse,
    RecipientPageResponse,
)
from app.services.archive import ArchiveFileMissingError, ArchiveIntegrityError
from app.services.jobs import IdempotencyConflictError, JobService

router = APIRouter()


@router.get("/health", tags=["operations"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post(
    "/api/v1/jobs",
    response_model=JobCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["certificate jobs"],
)
def create_job(
    payload: JobCreateRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    response: Response,
    job_service: Annotated[JobService, Depends(get_job_service)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=255),
    ] = None,
) -> JobCreatedResponse:
    if len(payload.recipients) > request.app.state.settings.max_recipients:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "recipient_limit_exceeded",
                "message": (
                    "A job may contain at most "
                    f"{request.app.state.settings.max_recipients} recipients."
                ),
            },
        )
    normalized_key = idempotency_key.strip() if idempotency_key is not None else None
    if idempotency_key is not None and not normalized_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_idempotency_key",
                "message": "Idempotency-Key must not be blank.",
            },
        )
    try:
        creation = job_service.create_job(payload, normalized_key)
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "idempotency_key_conflict",
                "message": "The idempotency key was already used with a different request.",
            },
        ) from exc
    job = creation.job
    if creation.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    else:
        background_tasks.add_task(request.app.state.job_processor.process, job.id)
    return JobCreatedResponse(
        job_id=job.id,
        status=job.status,
        total_recipients=job.total_count,
        status_url=f"/api/v1/jobs/{job.id}",
        idempotency_replayed=creation.replayed,
    )


@router.get(
    "/api/v1/jobs/{job_id}",
    response_model=JobStatusResponse,
    tags=["certificate jobs"],
)
def get_job(
    job_id: str,
    job_service: Annotated[JobService, Depends(get_job_service)],
    include_recipients: Annotated[
        bool,
        Query(description="Include a paginated recipient outcome collection."),
    ] = True,
    recipient_offset: Annotated[int, Query(ge=0)] = 0,
    recipient_limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> JobStatusResponse:
    job = job_service.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "job_not_found", "message": "The requested job does not exist."},
        )

    certificates = (
        job_service.list_certificates(job_id, recipient_offset, recipient_limit)
        if include_recipients
        else []
    )
    outcomes = [
        CertificateOutcomeResponse(
            certificate_id=certificate.id,
            recipient_index=certificate.recipient_index,
            recipient_name=certificate.recipient_name,
            recipient_email=certificate.recipient_email,
            status=certificate.status,
            error_message=certificate.error_message,
            download_url=(
                f"/api/v1/jobs/{job.id}/certificates/{certificate.id}"
                if certificate.status == CertificateStatus.COMPLETED
                else None
            ),
            sha256=certificate.sha256,
        )
        for certificate in certificates
    ]
    recipient_page = (
        RecipientPageResponse(
            offset=recipient_offset,
            limit=recipient_limit,
            returned=len(outcomes),
            total=job.total_count,
        )
        if include_recipients
        else None
    )
    return JobStatusResponse(
        job_id=job.id,
        status=job.status,
        certificate_title=job.certificate_title,
        issuing_organization=job.issuing_organization,
        issue_date=job.issue_date,
        total_recipients=job.total_count,
        successful_generations=job.success_count,
        failed_generations=job.failure_count,
        pending_generations=job.pending_count,
        progress_percentage=job.progress_percentage,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        recipients=outcomes,
        recipient_page=recipient_page,
    )


@router.get(
    "/api/v1/jobs/{job_id}/archive",
    tags=["certificates"],
    response_class=StreamingResponse,
)
def download_job_archive(
    job_id: str,
    request: Request,
    job_service: Annotated[JobService, Depends(get_job_service)],
) -> StreamingResponse:
    job = job_service.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "job_not_found", "message": "The requested job does not exist."},
        )
    if job.status in {JobStatus.PENDING, JobStatus.PROCESSING}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "job_not_finished",
                "message": "The archive is available after job processing finishes.",
            },
        )
    certificates = job_service.list_certificates(job_id, 0, job.total_count)
    try:
        archive = request.app.state.archive_builder.build(job, certificates)
    except ArchiveFileMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "code": "certificate_file_missing",
                "message": (
                    "A generated certificate required for the archive is no longer available."
                ),
                "certificate_id": exc.certificate_id,
            },
        ) from exc
    except ArchiveIntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "certificate_integrity_check_failed",
                "message": "A certificate required for the archive failed its integrity check.",
                "certificate_id": exc.certificate_id,
            },
        ) from exc
    return StreamingResponse(
        _read_and_close(archive),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="certificates-{job.id}.zip"'},
    )


@router.get(
    "/api/v1/jobs/{job_id}/certificates/{certificate_id}/verify",
    response_model=CertificateVerificationResponse,
    tags=["certificates"],
)
def verify_certificate(
    job_id: str,
    certificate_id: str,
    request: Request,
    job_service: Annotated[JobService, Depends(get_job_service)],
) -> CertificateVerificationResponse:
    certificate, path = _available_certificate(job_id, certificate_id, request, job_service)
    if not certificate.sha256:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "integrity_metadata_unavailable",
                "message": "This certificate does not have recorded integrity metadata.",
            },
        )
    computed = request.app.state.storage.sha256(path)
    return CertificateVerificationResponse(
        job_id=job_id,
        certificate_id=certificate_id,
        stored_sha256=certificate.sha256,
        computed_sha256=computed,
        valid=computed == certificate.sha256,
    )


@router.get(
    "/api/v1/jobs/{job_id}/certificates/{certificate_id}",
    tags=["certificates"],
    response_class=FileResponse,
)
def download_certificate(
    job_id: str,
    certificate_id: str,
    request: Request,
    job_service: Annotated[JobService, Depends(get_job_service)],
) -> FileResponse:
    certificate, path = _available_certificate(job_id, certificate_id, request, job_service)
    if certificate.sha256 and request.app.state.storage.sha256(path) != certificate.sha256:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "certificate_integrity_check_failed",
                "message": "The certificate file failed its integrity check.",
            },
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"certificate-{certificate.id}.pdf",
    )


def _available_certificate(
    job_id: str,
    certificate_id: str,
    request: Request,
    job_service: JobService,
) -> tuple[CertificateRecord, Path]:
    if job_service.get_job(job_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "job_not_found", "message": "The requested job does not exist."},
        )
    certificate = job_service.get_certificate(job_id, certificate_id)
    if certificate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "certificate_not_found",
                "message": "The requested certificate does not belong to this job.",
            },
        )
    if certificate.status != CertificateStatus.COMPLETED or not certificate.file_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "certificate_unavailable",
                "message": "The certificate was not generated successfully.",
            },
        )
    path = request.app.state.storage.resolve(certificate.file_path)
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "code": "certificate_file_missing",
                "message": "The generated certificate file is no longer available.",
            },
        )
    return certificate, path


def _read_and_close(archive: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := archive.read(1024 * 1024):
            yield chunk
    finally:
        archive.close()
