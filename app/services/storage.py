from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import Settings


class ArtifactNotFoundError(FileNotFoundError):
    """Raised when artifact metadata points to a missing stored object."""


class ArtifactStorage(Protocol):
    def key(self, job_id: str, certificate_id: str) -> str: ...

    def archive_key(self, job_id: str) -> str: ...

    def put_file(self, source: Path, key: str, content_type: str) -> None: ...

    def put_fileobj(self, source: BinaryIO, key: str, content_type: str) -> None: ...

    def exists(self, key: str) -> bool: ...

    def iter_bytes(self, key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]: ...

    def delete(self, key: str) -> None: ...

    def sha256_key(self, key: str) -> str: ...

    def ensure_ready(self) -> None: ...


class CertificateStorage:
    """Stores artifacts beneath a confined local directory."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def key(self, job_id: str, certificate_id: str) -> str:
        return f"{job_id}/{certificate_id}.pdf"

    def archive_key(self, job_id: str) -> str:
        return f"{job_id}/certificates.zip"

    def relative_path(self, job_id: str, certificate_id: str) -> Path:
        return Path(self.key(job_id, certificate_id))

    def resolve(self, relative_path: str | Path) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Certificate path escapes the configured storage directory.")
        return self.root / relative

    def put_file(self, source: Path, key: str, content_type: str) -> None:
        del content_type
        destination = self.resolve(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)

    def put_fileobj(self, source: BinaryIO, key: str, content_type: str) -> None:
        del content_type
        destination = self.resolve(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            with temporary.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self.resolve(key).is_file()

    def iter_bytes(self, key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        try:
            with self.resolve(key).open("rb") as artifact:
                while chunk := artifact.read(chunk_size):
                    yield chunk
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(key) from exc

    def delete(self, key: str) -> None:
        self.resolve(key).unlink(missing_ok=True)

    @staticmethod
    def sha256(path: Path) -> str:
        return _sha256_chunks(_file_chunks(path))

    def sha256_key(self, key: str) -> str:
        return self.sha256(self.resolve(key))

    def ensure_ready(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)


class S3CertificateStorage:
    """Stores artifacts in S3 or an S3-compatible service such as MinIO."""

    def __init__(self, settings: Settings) -> None:
        client_kwargs: dict[str, object] = {
            "region_name": settings.s3_region,
            "config": Config(
                retries={"total_max_attempts": 4, "mode": "standard"},
                connect_timeout=5,
                read_timeout=60,
                max_pool_connections=50,
                s3={"addressing_style": "path"},
            ),
        }
        if settings.s3_endpoint_url:
            client_kwargs["endpoint_url"] = settings.s3_endpoint_url
        if settings.s3_access_key_id and settings.s3_secret_access_key:
            client_kwargs["aws_access_key_id"] = settings.s3_access_key_id
            client_kwargs["aws_secret_access_key"] = settings.s3_secret_access_key
        self.client = boto3.client("s3", **client_kwargs)
        self.bucket = settings.s3_bucket
        self.region = settings.s3_region
        self.auto_create_bucket = settings.s3_auto_create_bucket

    def key(self, job_id: str, certificate_id: str) -> str:
        return f"certificates/{job_id}/{certificate_id}.pdf"

    def archive_key(self, job_id: str) -> str:
        return f"archives/{job_id}/certificates.zip"

    def put_file(self, source: Path, key: str, content_type: str) -> None:
        self.client.upload_file(
            str(source),
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )

    def put_fileobj(self, source: BinaryIO, key: str, content_type: str) -> None:
        self.client.upload_fileobj(
            source,
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {
                "404",
                "NoSuchKey",
                "NotFound",
            }:
                return False
            raise

    def iter_bytes(self, key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {
                "404",
                "NoSuchKey",
                "NotFound",
            }:
                raise ArtifactNotFoundError(key) from exc
            raise
        body = response["Body"]
        try:
            yield from body.iter_chunks(chunk_size=chunk_size)
        finally:
            body.close()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def sha256_key(self, key: str) -> str:
        return _sha256_chunks(self.iter_bytes(key))

    def ensure_ready(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if not self.auto_create_bucket or code not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            create_args: dict[str, object] = {"Bucket": self.bucket}
            if self.region != "us-east-1":
                create_args["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
            self.client.create_bucket(**create_args)


def create_storage(settings: Settings) -> ArtifactStorage:
    if settings.storage_backend == "s3":
        return S3CertificateStorage(settings)
    return CertificateStorage(settings.storage_dir)


def _file_chunks(path: Path, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    with path.open("rb") as artifact:
        while chunk := artifact.read(chunk_size):
            yield chunk


def _sha256_chunks(chunks: Iterator[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()
