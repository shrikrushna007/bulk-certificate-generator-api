from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes import router
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.session import Database
from app.services.certificate_renderer import CertificateRenderer
from app.services.jobs import JobProcessor, JobService
from app.services.storage import CertificateStorage

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    configure_logging(resolved_settings.log_level)
    database = Database(resolved_settings.database_url)
    storage = CertificateStorage(resolved_settings.storage_dir)

    @asynccontextmanager
    async def lifespan(lifespan_app: FastAPI):
        storage.root.mkdir(parents=True, exist_ok=True)
        try:
            yield
        finally:
            lifespan_app.state.job_processor.close()
            database.dispose()

    application = FastAPI(
        title="Bulk Certificate Generator API",
        version="1.0.0",
        description="Create bulk PDF certificate jobs and retrieve recipient-level results.",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.database = database
    application.state.storage = storage
    application.state.job_service = JobService(database)
    application.state.job_processor = JobProcessor(
        database,
        storage,
        CertificateRenderer(),
        render_workers=resolved_settings.render_workers,
        batch_size=resolved_settings.processing_batch_size,
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
    return application


app = create_app()
