from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes import router
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.session import Database
from app.services.archive import JobArchiveBuilder
from app.services.archive_jobs import ArchiveProcessor
from app.services.certificate_renderer import CertificateRenderer
from app.services.dispatch import DistributedDispatcher
from app.services.jobs import JobProcessor, JobService
from app.services.storage import create_storage

logger = logging.getLogger(__name__)
WEB_DIRECTORY = Path(__file__).parent / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    configure_logging(resolved_settings.log_level, resolved_settings.log_format)
    database = Database(resolved_settings.database_url)
    storage = create_storage(resolved_settings)
    job_processor = (
        JobProcessor(
            database,
            storage,
            CertificateRenderer(),
            render_workers=resolved_settings.render_workers,
            batch_size=resolved_settings.processing_batch_size,
        )
        if resolved_settings.processing_mode == "inline"
        else None
    )

    @asynccontextmanager
    async def lifespan(lifespan_app: FastAPI):
        await asyncio.to_thread(storage.ensure_ready)
        recovery_task = None
        if job_processor is not None:
            recovery_task = asyncio.create_task(
                _recover_jobs(job_processor),
                name="certificate-job-recovery",
            )
            lifespan_app.state.recovery_task = recovery_task
        try:
            yield
        finally:
            if recovery_task is not None:
                await recovery_task
            if job_processor is not None:
                job_processor.close()
            database.dispose()

    application = FastAPI(
        title="Bulk Certificate Generator API",
        version="2.0.0",
        description="Create bulk PDF certificate jobs and retrieve recipient-level results.",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.database = database
    application.state.storage = storage
    archive_builder = JobArchiveBuilder(
        storage, download_workers=resolved_settings.archive_download_workers
    )
    application.state.archive_builder = archive_builder
    application.state.archive_processor = ArchiveProcessor(
        database,
        storage,
        archive_builder,
        resolved_settings.archive_claim_timeout_seconds,
    )
    application.state.job_service = JobService(database)
    application.state.job_processor = job_processor
    application.state.dispatcher = (
        DistributedDispatcher(database, resolved_settings.distributed_job_parallelism)
        if resolved_settings.processing_mode == "distributed"
        else None
    )

    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"location": list(error["loc"]), "message": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "request_validation_error",
                    "message": "The request payload is invalid.",
                    "details": details,
                }
            },
        )

    @application.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict):
            content = {"error": exc.detail}
        else:
            content = {"error": {"code": "http_error", "message": str(exc.detail)}}
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

    @application.exception_handler(SQLAlchemyError)
    async def database_exception_handler(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.error("Database operation failed", exc_info=exc)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "database_unavailable",
                    "message": "The database operation could not be completed.",
                }
            },
        )

    @application.exception_handler(Exception)
    async def unexpected_exception_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled request error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_server_error",
                    "message": "The request could not be completed.",
                }
            },
        )

    application.include_router(router)
    application.mount(
        "/static",
        StaticFiles(directory=WEB_DIRECTORY),
        name="static",
    )

    @application.get("/", include_in_schema=False)
    def frontend() -> FileResponse:
        return FileResponse(WEB_DIRECTORY / "index.html")

    return application


async def _recover_jobs(job_processor: JobProcessor) -> None:
    try:
        await asyncio.to_thread(job_processor.recover_incomplete_jobs)
    except Exception:
        logger.exception("Certificate job recovery failed")


app = create_app()
