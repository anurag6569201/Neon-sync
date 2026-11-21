"""Content-addressed temporary artifact storage with SHA-256 deduplication and TTL cleanup."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from neon_sync.config import Settings
from neon_sync.security import ensure_safe_path, sanitize_filename


@dataclass
class StoredArtifact:
    job_id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    disk_path: Path
    expires_at: datetime
    download_url: str


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
    """Content-addressed artifact store with TTL expiry (S3/R2-swappable interface)."""

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

        # Content-addressed deduplication: skip copy if identical blob already exists
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
        if artifact is None:
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
            if art and art.disk_path not in active_paths:
                art.disk_path.unlink(missing_ok=True)
                removed += 1
        return removed
