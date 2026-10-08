from __future__ import annotations

import hashlib
from pathlib import Path


class CertificateStorage:
    """Maps database-relative certificate paths to a confined storage directory."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def relative_path(self, job_id: str, certificate_id: str) -> Path:
        return Path(job_id) / f"{certificate_id}.pdf"

    def resolve(self, relative_path: str | Path) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Certificate path escapes the configured storage directory.")
        return self.root / relative

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as certificate_file:
            for chunk in iter(lambda: certificate_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
