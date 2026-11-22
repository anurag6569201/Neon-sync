"""Content-addressed artifact storage (Durable Local SQLite-indexed FS + S3 / Cloudflare R2)."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import threading
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
    """Content-addressed local artifact store with durable SQLite index, SHA-256 dedup & TTL cleanup."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._root = settings.storage_dir.resolve()
        self._blobs = self._root / "blobs"
        self._blobs.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._index: dict[tuple[str, str], StoredArtifact] = {}
        self._db_path = self._root / "artifacts_index.sqlite3"
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                job_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                blob_relpath TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                download_url TEXT NOT NULL,
                PRIMARY KEY (job_id, filename)
            );
            """
        )
        self._conn.commit()
        self._hydrate_index()

    def _hydrate_index(self) -> None:
        now = datetime.now(UTC)
        with self._lock:
            rows = self._conn.execute("SELECT * FROM artifacts").fetchall()
        for r in rows:
            exp = datetime.fromisoformat(r["expires_at"])
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            blob_path = (self._blobs / r["blob_relpath"]).resolve()
            if exp > now and blob_path.exists():
                self._index[(r["job_id"], r["filename"])] = StoredArtifact(
                    job_id=r["job_id"],
                    filename=r["filename"],
                    mime_type=r["mime_type"],
                    size_bytes=int(r["size_bytes"]),
                    sha256=r["sha256"],
                    disk_path=blob_path,
                    expires_at=exp,
                    download_url=r["download_url"],
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

        shard_dir = ensure_safe_path(self._blobs, self._blobs / digest[:2])
        shard_dir.mkdir(parents=True, exist_ok=True)
        blob_path = ensure_safe_path(self._blobs, shard_dir / f"{digest}{ext}")

        if not blob_path.exists():
            shutil.copy2(source_path, blob_path)

        expires_at = datetime.now(UTC) + timedelta(seconds=self._settings.artifact_ttl_seconds)
        base = self._settings.artifact_base_url.rstrip("/")
        download_url = f"{base}/v1/artifacts/{quote(job_id, safe='')}/{quote(safe_name, safe='')}"
        relpath = f"{digest[:2]}/{digest}{ext}"

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
        with self._lock:
            self._index[(job_id, safe_name)] = artifact
            self._conn.execute(
                """
                INSERT INTO artifacts (
                    job_id, filename, mime_type, size_bytes, sha256,
                    blob_relpath, expires_at, download_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, filename) DO UPDATE SET
                    mime_type = excluded.mime_type,
                    size_bytes = excluded.size_bytes,
                    sha256 = excluded.sha256,
                    blob_relpath = excluded.blob_relpath,
                    expires_at = excluded.expires_at,
                    download_url = excluded.download_url
                """,
                (
                    job_id,
                    safe_name,
                    mime_type,
                    size,
                    digest,
                    relpath,
                    expires_at.isoformat(),
                    download_url,
                ),
            )
            self._conn.commit()
        return artifact

    def resolve(self, job_id: str, filename: str) -> StoredArtifact | None:
        safe_name = sanitize_filename(filename)
        with self._lock:
            artifact = self._index.get((job_id, safe_name))
        if artifact is None or artifact.disk_path is None:
            return None
        if datetime.now(UTC) > artifact.expires_at or not artifact.disk_path.exists():
            with self._lock:
                self._index.pop((job_id, safe_name), None)
                self._conn.execute(
                    "DELETE FROM artifacts WHERE job_id = ? AND filename = ?",
                    (job_id, safe_name),
                )
                self._conn.commit()
            return None
        ensure_safe_path(self._blobs, artifact.disk_path)
        return artifact

    def cleanup_expired(self) -> int:
        now = datetime.now(UTC)
        with self._lock:
            expired_keys = [k for k, v in self._index.items() if now > v.expires_at]
            active_paths = {
                v.disk_path for k, v in self._index.items() if k not in set(expired_keys)
            }
            removed = 0
            for key in expired_keys:
                art = self._index.pop(key, None)
                self._conn.execute(
                    "DELETE FROM artifacts WHERE job_id = ? AND filename = ?",
                    (key[0], key[1]),
                )
                if art and art.disk_path and art.disk_path not in active_paths:
                    art.disk_path.unlink(missing_ok=True)
                    removed += 1
            if expired_keys:
                self._conn.commit()
        return removed

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class S3ArtifactStorage:
    """S3 / Cloudflare R2 / MinIO storage backend issuing pre-signed safe download URLs."""

    def __init__(self, settings: Settings) -> None:
        bucket = settings.cf_r2_bucket or settings.s3_bucket
        if not bucket:
            raise ValueError("s3_bucket or cf_r2_bucket must be configured for S3ArtifactStorage")
        import boto3

        endpoint = settings.s3_endpoint_url
        if not endpoint and settings.cf_r2_bucket and settings.cf_account_id:
            endpoint = f"https://{settings.cf_account_id}.r2.cloudflarestorage.com"

        client_kwargs: dict[str, object] = {
            "endpoint_url": endpoint,
            "region_name": settings.s3_region,
        }
        if settings.cf_r2_access_key_id and settings.cf_r2_secret_access_key:
            client_kwargs["aws_access_key_id"] = settings.cf_r2_access_key_id
            client_kwargs["aws_secret_access_key"] = settings.cf_r2_secret_access_key

        self._settings = settings
        self._bucket = bucket
        self._prefix = settings.s3_key_prefix.strip("/")
        self._client = boto3.client("s3", **client_kwargs)

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
        return None

    def cleanup_expired(self) -> int:
        return 0


def build_storage(settings: Settings) -> ArtifactStorage:
    if settings.cf_r2_bucket or settings.s3_bucket:
        return S3ArtifactStorage(settings)
    return LocalArtifactStorage(settings)
