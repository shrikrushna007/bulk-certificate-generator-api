from __future__ import annotations

import csv
import io
import tempfile
from typing import BinaryIO
from zipfile import ZIP_DEFLATED, ZipFile

from app.db.models import CertificateJob, CertificateRecord, CertificateStatus
from app.services.storage import CertificateStorage


class ArchiveFileMissingError(Exception):
    def __init__(self, certificate_id: str) -> None:
        self.certificate_id = certificate_id
        super().__init__(certificate_id)


class ArchiveIntegrityError(Exception):
    def __init__(self, certificate_id: str) -> None:
        self.certificate_id = certificate_id
        super().__init__(certificate_id)


class JobArchiveBuilder:
    """Builds a transient ZIP containing successful PDFs and a complete CSV report."""

    def __init__(self, storage: CertificateStorage) -> None:
        self.storage = storage

    def build(
        self,
        job: CertificateJob,
        certificates: list[CertificateRecord],
    ) -> BinaryIO:
        # The response iterator owns and closes this temporary file after streaming it.
        archive = tempfile.SpooledTemporaryFile(  # noqa: SIM115
            max_size=16 * 1024 * 1024,
            mode="w+b",
        )
        try:
            with ZipFile(archive, mode="w", compression=ZIP_DEFLATED) as zip_file:
                zip_file.writestr("report.csv", self._csv_report(job, certificates))
                for certificate in certificates:
                    if certificate.status != CertificateStatus.COMPLETED:
                        continue
                    if not certificate.file_path:
                        raise ArchiveFileMissingError(certificate.id)
                    path = self.storage.resolve(certificate.file_path)
                    if not path.is_file():
                        raise ArchiveFileMissingError(certificate.id)
                    if certificate.sha256 and self.storage.sha256(path) != certificate.sha256:
                        raise ArchiveIntegrityError(certificate.id)
                    archive_name = (
                        f"certificates/{certificate.recipient_index + 1:05d}-"
                        f"{certificate.id}.pdf"
                    )
                    zip_file.write(path, archive_name)
            archive.seek(0)
            return archive
        except Exception:
            archive.close()
            raise

    @staticmethod
    def _csv_report(
        job: CertificateJob,
        certificates: list[CertificateRecord],
    ) -> str:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            [
                "recipient_index",
                "certificate_id",
                "recipient_name",
                "recipient_email",
                "status",
                "error_message",
                "sha256",
                "download_url",
            ]
        )
        for certificate in certificates:
            download_url = (
                f"/api/v1/jobs/{job.id}/certificates/{certificate.id}"
                if certificate.status == CertificateStatus.COMPLETED
                else ""
            )
            writer.writerow(
                [
                    certificate.recipient_index,
                    certificate.id,
                    _csv_safe(certificate.recipient_name),
                    _csv_safe(certificate.recipient_email),
                    certificate.status.value,
                    _csv_safe(certificate.error_message),
                    certificate.sha256 or "",
                    download_url,
                ]
            )
        return output.getvalue()


def _csv_safe(value: str | None) -> str:
    if not value:
        return ""
    if value[0] in {"=", "+", "-", "@"}:
        return f"'{value}"
    return value
