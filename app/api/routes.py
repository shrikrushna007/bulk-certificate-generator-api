from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse

from app.api.dependencies import get_job_service
from app.db.models import CertificateStatus
from app.schemas.jobs import (
    CertificateOutcomeResponse,
    JobCreatedResponse,
    JobCreateRequest,
    JobStatusResponse,
    RecipientPageResponse,
)
from app.services.jobs import JobService

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
    job_service: Annotated[JobService, Depends(get_job_service)],
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
    job = job_service.create_job(payload)
    background_tasks.add_task(request.app.state.job_processor.process, job.id)
    return JobCreatedResponse(
        job_id=job.id,
        status=job.status,
        total_recipients=job.total_count,
        status_url=f"/api/v1/jobs/{job.id}",
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
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"certificate-{certificate.id}.pdf",
    )
