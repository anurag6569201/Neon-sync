"""Content-addressed temporary artifact storage (Local filesystem + optional S3/Cloudflare R2)."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from neon_sync.config import Settings
from neon_sync.security import build_content_disposition, ensure_safe_path, sanitize_filename


@dataclass
class StoredArtifact:
    job_id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    disk_path: Path | None
    expires_at: datetime
    download_url: str


class ArtifactStorage(Protocol):
    def store(
        self,
        *,
        job_id: str,
        source_path: Path,
        filename: str,
        mime_type: str,
    ) -> StoredArtifact: ...

    def resolve(self, job_id: str, filename: str) -> StoredArtifact | None: ...
    def cleanup_expired(self) -> int: ...


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(256 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class LocalArtifactStorage:
    """Content-addressed local artifact store with SHA-256 deduplication and TTL cleanup."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._root = settings.storage_dir.resolve()
        self._blobs = self._root / "blobs"
        self._blobs.mkdir(parents=True, exist_ok=True)
        self._index: dict[tuple[str, str], StoredArtifact] = {}

    def store(
        self,
        *,
        job_id: str,
        source_path: Path,
        filename: str,
        mime_type: str,
    ) -> StoredArtifact:
        safe_name = sanitize_filename(filename)
        ext = Path(safe_name).suffix.lower() or ".bin"
        digest = _sha256_file(source_path)
        size = source_path.stat().st_size

        shard_dir = ensure_safe_path(self._blobs, self._blobs / digest[:2])
        shard_dir.mkdir(parents=True, exist_ok=True)
        blob_path = ensure_safe_path(self._blobs, shard_dir / f"{digest}{ext}")

        if not blob_path.exists():
            shutil.copy2(source_path, blob_path)

        expires_at = datetime.now(UTC) + timedelta(seconds=self._settings.artifact_ttl_seconds)
        base = self._settings.artifact_base_url.rstrip("/")
        download_url = f"{base}/v1/artifacts/{quote(job_id, safe='')}/{quote(safe_name, safe='')}"

        artifact = StoredArtifact(
            job_id=job_id,
            filename=safe_name,
            mime_type=mime_type,
            size_bytes=size,
            sha256=digest,
            disk_path=blob_path,
            expires_at=expires_at,
            download_url=download_url,
        )
        self._index[(job_id, safe_name)] = artifact
        return artifact

    def resolve(self, job_id: str, filename: str) -> StoredArtifact | None:
        safe_name = sanitize_filename(filename)
        artifact = self._index.get((job_id, safe_name))
        if artifact is None or artifact.disk_path is None:
            return None
        if datetime.now(UTC) > artifact.expires_at or not artifact.disk_path.exists():
            self._index.pop((job_id, safe_name), None)
            return None
        ensure_safe_path(self._blobs, artifact.disk_path)
        return artifact

    def cleanup_expired(self) -> int:
        now = datetime.now(UTC)
        expired_keys = [k for k, v in self._index.items() if now > v.expires_at]
        active_paths = {
            v.disk_path for k, v in self._index.items() if k not in set(expired_keys)
        }
        removed = 0
        for key in expired_keys:
            art = self._index.pop(key, None)
            if art and art.disk_path and art.disk_path not in active_paths:
                art.disk_path.unlink(missing_ok=True)
                removed += 1
        return removed


class S3ArtifactStorage:
    """S3 / Cloudflare R2 / MinIO storage backend issuing pre-signed safe download URLs."""

    def __init__(self, settings: Settings) -> None:
        if not settings.s3_bucket:
            raise ValueError("s3_bucket must be configured for S3ArtifactStorage")
        import boto3

        self._settings = settings
        self._bucket = settings.s3_bucket
        self._prefix = settings.s3_key_prefix.strip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
        )

    def store(
        self,
        *,
        job_id: str,
        source_path: Path,
        filename: str,
        mime_type: str,
    ) -> StoredArtifact:
        safe_name = sanitize_filename(filename)
        ext = Path(safe_name).suffix.lower() or ".bin"
        digest = _sha256_file(source_path)
        size = source_path.stat().st_size
        key = f"{self._prefix}/{digest[:2]}/{digest}{ext}" if self._prefix else f"{digest[:2]}/{digest}{ext}"

        self._client.upload_file(
            str(source_path),
            self._bucket,
            key,
            ExtraArgs={
                "ContentType": mime_type,
                "ContentDisposition": build_content_disposition(safe_name),
            },
        )
        ttl = min(self._settings.artifact_ttl_seconds, 3600)
        presigned = self._client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self._bucket,
                "Key": key,
                "ResponseContentDisposition": build_content_disposition(safe_name),
                "ResponseContentType": mime_type,
            },
            ExpiresIn=ttl,
        )
        return StoredArtifact(
            job_id=job_id,
            filename=safe_name,
            mime_type=mime_type,
            size_bytes=size,
            sha256=digest,
            disk_path=None,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
            download_url=presigned,
        )

    def resolve(self, job_id: str, filename: str) -> StoredArtifact | None:
        # S3 artifacts are delivered directly via isolated pre-signed URLs
        return None

    def cleanup_expired(self) -> int:
        # Handled natively by S3 / R2 bucket lifecycle expiration policies
        return 0


def build_storage(settings: Settings) -> ArtifactStorage:
    if settings.s3_bucket:
        return S3ArtifactStorage(settings)
    return LocalArtifactStorage(settings)
