from __future__ import annotations

import csv
import hashlib
import io
import tempfile
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import islice
from typing import BinaryIO
from zipfile import ZIP_DEFLATED, ZipFile

from app.db.models import CertificateJob, CertificateRecord, CertificateStatus
from app.services.storage import ArtifactNotFoundError, ArtifactStorage


class ArchiveFileMissingError(Exception):
    def __init__(self, certificate_id: str) -> None:
        self.certificate_id = certificate_id
        super().__init__(certificate_id)


class ArchiveIntegrityError(Exception):
    def __init__(self, certificate_id: str) -> None:
        self.certificate_id = certificate_id
        super().__init__(certificate_id)


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    content: bytes


class JobArchiveBuilder:
    """Builds a transient ZIP containing successful PDFs and a complete CSV report."""

    def __init__(self, storage: ArtifactStorage, download_workers: int = 16) -> None:
        self.storage = storage
        self.download_workers = download_workers

    def build(
        self,
        job: CertificateJob,
        certificates: Callable[[], Iterable[CertificateRecord]],
        progress: Callable[[int], None] | None = None,
        progress_interval: int = 100,
    ) -> BinaryIO:
        # The response iterator owns and closes this temporary file after streaming it.
        archive = tempfile.SpooledTemporaryFile(  # noqa: SIM115
            max_size=16 * 1024 * 1024,
            mode="w+b",
        )
        try:
            with ZipFile(archive, mode="w", compression=ZIP_DEFLATED) as zip_file:
                self._write_csv_report(zip_file, job, certificates())
                processed = 0
                completed = (
                    certificate
                    for certificate in certificates()
                    if certificate.status == CertificateStatus.COMPLETED
                )
                batch_size = self.download_workers * 2
                with ThreadPoolExecutor(max_workers=self.download_workers) as executor:
                    while batch := list(islice(completed, batch_size)):
                        for member in executor.map(self._read_member, batch):
                            zip_file.writestr(member.name, member.content)
                            processed += 1
                            if progress and processed % progress_interval == 0:
                                progress(processed)
                if progress:
                    progress(processed)
            archive.seek(0)
            return archive
        except Exception:
            archive.close()
            raise

    def _read_member(self, certificate: CertificateRecord) -> ArchiveMember:
        if not certificate.file_path:
            raise ArchiveFileMissingError(certificate.id)
        archive_name = f"certificates/{certificate.recipient_index + 1:05d}-{certificate.id}.pdf"
        digest = hashlib.sha256()
        content = bytearray()
        try:
            for chunk in self.storage.iter_bytes(certificate.file_path):
                digest.update(chunk)
                content.extend(chunk)
        except ArtifactNotFoundError as exc:
            raise ArchiveFileMissingError(certificate.id) from exc
        if certificate.sha256 and digest.hexdigest() != certificate.sha256:
            raise ArchiveIntegrityError(certificate.id)
        return ArchiveMember(archive_name, bytes(content))

    @staticmethod
    def _write_csv_report(
        archive: ZipFile,
        job: CertificateJob,
        certificates: Iterable[CertificateRecord],
    ) -> None:
        with archive.open("report.csv", mode="w") as raw_report:
            output = io.TextIOWrapper(
                raw_report,
                encoding="utf-8",
                newline="",
                write_through=True,
            )
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
            output.detach()


def _csv_safe(value: str | None) -> str:
    if not value:
        return ""
    if value[0] in {"=", "+", "-", "@"}:
        return f"'{value}"
    return value
