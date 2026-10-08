from pathlib import Path

import pytest

from app.services.storage import CertificateStorage


def test_storage_rejects_path_traversal(tmp_path: Path) -> None:
    storage = CertificateStorage(tmp_path / "storage")

    with pytest.raises(ValueError, match="escapes"):
        storage.resolve("../outside.pdf")
