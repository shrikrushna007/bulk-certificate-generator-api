from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

from app.schemas.jobs import CertificateDetailsInput, RecipientInput


class CsvImportError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class CsvInspection:
    recipient_count: int
    request_hash: str


def inspect_csv(
    source: BinaryIO,
    certificate: CertificateDetailsInput,
    max_recipients: int,
) -> CsvInspection:
    digest = hashlib.sha256(
        json.dumps(
            certificate.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    source.seek(0)
    while chunk := source.read(1024 * 1024):
        digest.update(chunk)
    source.seek(0)

    count = 0
    try:
        with _text_reader(source) as text_source:
            reader = csv.DictReader(text_source)
            _validate_headers(reader.fieldnames)
            for _ in reader:
                count += 1
                if max_recipients and count > max_recipients:
                    raise CsvImportError(
                        "recipient_limit_exceeded",
                        f"The CSV may contain at most {max_recipients} recipients.",
                    )
    except UnicodeDecodeError as exc:
        raise CsvImportError("invalid_csv_encoding", "The CSV must use UTF-8 encoding.") from exc
    finally:
        source.seek(0)
    if count == 0:
        raise CsvImportError("empty_recipient_list", "The CSV must contain at least one row.")
    return CsvInspection(count, digest.hexdigest())


def iter_csv_recipients(source: BinaryIO) -> Iterator[RecipientInput]:
    source.seek(0)
    try:
        with _text_reader(source) as text_source:
            reader = csv.DictReader(text_source)
            _validate_headers(reader.fieldnames)
            for row in reader:
                yield RecipientInput(
                    name=row.get("name"),
                    email=row.get("email") or None,
                )
    finally:
        source.seek(0)


class _text_reader:
    def __init__(self, source: BinaryIO) -> None:
        self.wrapper = io.TextIOWrapper(source, encoding="utf-8-sig", newline="")

    def __enter__(self) -> io.TextIOWrapper:
        return self.wrapper

    def __exit__(self, *_: object) -> None:
        self.wrapper.detach()


def _validate_headers(fieldnames: list[str] | None) -> None:
    if not fieldnames or "name" not in fieldnames:
        raise CsvImportError(
            "invalid_csv_headers",
            "The CSV must contain a 'name' column and may contain an 'email' column.",
        )
    unexpected = set(fieldnames) - {"name", "email"}
    if unexpected:
        raise CsvImportError(
            "invalid_csv_headers",
            "The CSV contains unsupported columns: " + ", ".join(sorted(unexpected)) + ".",
        )
