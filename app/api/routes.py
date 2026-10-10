from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Annotated, BinaryIO

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.api.dependencies import get_job_service
from app.db.models import (
    ArchiveStatus,
    CertificateJob,
    CertificateRecord,
    CertificateStatus,
    JobStatus,
)
from app.schemas.jobs import (
    ArchiveRequestedResponse,
    CertificateDetailsInput,
    CertificateOutcomeResponse,
    CertificateVerificationResponse,
    JobCreatedResponse,
    JobCreateRequest,
    JobStatusResponse,
    RecipientPageResponse,
)
from app.services.archive import ArchiveFileMissingError, ArchiveIntegrityError
from app.services.csv_import import CsvImportError, inspect_csv, iter_csv_recipients
from app.services.dispatch import DispatchUnavailableError
from app.services.jobs import IdempotencyConflictError, JobService
from app.services.storage import ArtifactNotFoundError

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
    normalized_key = _normalize_idempotency_key(idempotency_key)
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
    _dispatch_job(request, background_tasks, job.id, creation.replayed, job.status)
    return _created_response(job, creation.replayed)


@router.post(
    "/api/v1/jobs/import",
    response_model=JobCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["certificate jobs"],
)
def import_csv_job(
    background_tasks: BackgroundTasks,
    request: Request,
    response: Response,
    job_service: Annotated[JobService, Depends(get_job_service)],
    title: Annotated[str, Form(max_length=200)],
    issuing_organization: Annotated[str, Form(max_length=200)],
    issue_date: Annotated[date, Form()],
    recipients: Annotated[UploadFile, File(description="UTF-8 CSV with name and email columns")],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=255),
    ] = None,
) -> JobCreatedResponse:
    normalized_key = _normalize_idempotency_key(idempotency_key)
    try:
        certificate = CertificateDetailsInput(
            title=title,
            issuing_organization=issuing_organization,
            issue_date=issue_date,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_certificate_details",
                "message": "The certificate details are invalid.",
            },
        ) from exc
    try:
        inspection = inspect_csv(
            recipients.file,
            certificate,
            request.app.state.settings.max_csv_recipients,
        )
        creation = job_service.create_streamed_job(
            certificate=certificate,
            recipients=iter_csv_recipients(recipients.file),
            total_count=inspection.recipient_count,
            request_hash=inspection.request_hash,
            idempotency_key=normalized_key,
        )
    except CsvImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
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
    _dispatch_job(request, background_tasks, job.id, creation.replayed, job.status)
    return _created_response(job, creation.replayed)


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
        archive_status=job.archive_status,
        archive_download_url=(
            f"/api/v1/jobs/{job.id}/archive"
            if job.archive_status == ArchiveStatus.COMPLETED
            else None
        ),
        archive_total_files=job.archive_total_count,
        archive_processed_files=job.archive_processed_count,
        archive_progress_percentage=job.archive_progress_percentage,
        archive_started_at=job.archive_started_at,
        archive_completed_at=job.archive_completed_at,
        archive_elapsed_seconds=job.archive_elapsed_seconds,
        archive_estimated_remaining_seconds=job.archive_estimated_remaining_seconds,
        recipients=outcomes,
        recipient_page=recipient_page,
    )


@router.post(
    "/api/v1/jobs/{job_id}/archive",
    response_model=ArchiveRequestedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["certificates"],
)
def request_job_archive(
    job_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
    job_service: Annotated[JobService, Depends(get_job_service)],
) -> ArchiveRequestedResponse:
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
                "message": "An archive can be requested after job processing finishes.",
            },
        )
    requested = job_service.request_archive(job_id)
    if requested is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if requested.archive_status == ArchiveStatus.PENDING:
        if request.app.state.settings.processing_mode == "distributed":
            try:
                request.app.state.dispatcher.dispatch_archive(job_id)
            except DispatchUnavailableError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "code": "archive_queue_unavailable",
                        "message": "The archive request was saved but could not be queued.",
                    },
                ) from exc
        else:
            background_tasks.add_task(request.app.state.archive_processor.process, job_id)
    return ArchiveRequestedResponse(
        job_id=job_id,
        status=requested.archive_status,
        download_url=f"/api/v1/jobs/{job_id}/archive",
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
    if job.archive_status == ArchiveStatus.COMPLETED and job.archive_path:
        if not request.app.state.storage.exists(job.archive_path):
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={
                    "code": "archive_file_missing",
                    "message": "The generated archive is no longer available.",
                },
            )
        return StreamingResponse(
            request.app.state.storage.iter_bytes(job.archive_path),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="certificates-{job.id}.zip"'},
        )
    if request.app.state.settings.processing_mode == "distributed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "archive_not_ready",
                "message": "Request the archive and wait for archive_status COMPLETED.",
                "status": job.archive_status.value,
            },
        )
    try:
        archive = request.app.state.archive_builder.build(
            job,
            lambda: job_service.iter_certificates(job_id),
        )
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
    certificate = _available_certificate(job_id, certificate_id, request, job_service)
    if not certificate.sha256:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "integrity_metadata_unavailable",
                "message": "This certificate does not have recorded integrity metadata.",
            },
        )
    try:
        computed = request.app.state.storage.sha256_key(certificate.file_path)
    except ArtifactNotFoundError as exc:
        raise _certificate_file_missing() from exc
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
    response_class=StreamingResponse,
)
def download_certificate(
    job_id: str,
    certificate_id: str,
    request: Request,
    job_service: Annotated[JobService, Depends(get_job_service)],
) -> StreamingResponse:
    certificate = _available_certificate(job_id, certificate_id, request, job_service)
    try:
        computed_sha256 = (
            request.app.state.storage.sha256_key(certificate.file_path)
            if certificate.sha256
            else None
        )
    except ArtifactNotFoundError as exc:
        raise _certificate_file_missing() from exc
    if certificate.sha256 and computed_sha256 != certificate.sha256:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "certificate_integrity_check_failed",
                "message": "The certificate file failed its integrity check.",
            },
        )
    return StreamingResponse(
        request.app.state.storage.iter_bytes(certificate.file_path),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="certificate-{certificate.id}.pdf"'},
    )


def _available_certificate(
    job_id: str,
    certificate_id: str,
    request: Request,
    job_service: JobService,
) -> CertificateRecord:
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
    if not request.app.state.storage.exists(certificate.file_path):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "code": "certificate_file_missing",
                "message": "The generated certificate file is no longer available.",
            },
        )
    return certificate


def _certificate_file_missing() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "certificate_file_missing",
            "message": "The generated certificate file is no longer available.",
        },
    )


def _normalize_idempotency_key(idempotency_key: str | None) -> str | None:
    normalized = idempotency_key.strip() if idempotency_key is not None else None
    if idempotency_key is not None and not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_idempotency_key",
                "message": "Idempotency-Key must not be blank.",
            },
        )
    return normalized


def _dispatch_job(
    request: Request,
    background_tasks: BackgroundTasks,
    job_id: str,
    replayed: bool,
    job_status: JobStatus,
) -> None:
    if job_status not in {JobStatus.PENDING, JobStatus.PROCESSING}:
        return
    if request.app.state.settings.processing_mode == "distributed":
        try:
            request.app.state.dispatcher.dispatch(job_id)
        except DispatchUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "job_queue_unavailable",
                    "message": (
                        "The job was saved but could not be queued. "
                        "Retry with the same idempotency key."
                    ),
                    "job_id": job_id,
                },
            ) from exc
    elif not replayed:
        background_tasks.add_task(request.app.state.job_processor.process, job_id)


def _created_response(job: CertificateJob, replayed: bool) -> JobCreatedResponse:
    return JobCreatedResponse(
        job_id=job.id,
        status=job.status,
        total_recipients=job.total_count,
        status_url=f"/api/v1/jobs/{job.id}",
        idempotency_replayed=replayed,
    )


def _read_and_close(archive: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := archive.read(1024 * 1024):
            yield chunk
    finally:
        archive.close()
