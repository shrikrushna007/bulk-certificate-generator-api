from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.storage import CertificateStorage, S3CertificateStorage


def test_storage_rejects_path_traversal(tmp_path: Path) -> None:
    storage = CertificateStorage(tmp_path / "storage")

    with pytest.raises(ValueError, match="escapes"):
        storage.resolve("../outside.pdf")


def test_s3_storage_streams_and_closes_response_body() -> None:
    body = MagicMock()
    body.iter_chunks.return_value = iter([b"first", b"second"])
    client = MagicMock()
    client.get_object.return_value = {"Body": body}
    storage = object.__new__(S3CertificateStorage)
    storage.client = client
    storage.bucket = "certflow"

    content = b"".join(storage.iter_bytes("certificates/job/certificate.pdf", chunk_size=5))

    assert content == b"firstsecond"
    client.get_object.assert_called_once_with(
        Bucket="certflow",
        Key="certificates/job/certificate.pdf",
    )
    body.iter_chunks.assert_called_once_with(chunk_size=5)
    body.close.assert_called_once_with()
