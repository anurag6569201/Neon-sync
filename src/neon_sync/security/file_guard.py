"""Safe filename sanitization, path confinement, disk floor checks, and magic-byte validation."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from urllib.parse import quote


class DiskQuotaError(RuntimeError):
    """Raised when available scratch/storage disk drops below the safety floor."""


class FileValidationError(ValueError):
    """Raised when a downloaded file fails safety or magic-byte verification."""


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_MULTI_SPACE_RE = re.compile(r"\s+")

# Dangerous executable signatures blocked unless explicitly permitted
_EXECUTABLE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x7fELF", "application/x-elf"),
    (b"MZ", "application/x-dosexec"),
    (b"\xfe\xed\xfa\xce", "application/x-mach-binary"),
    (b"\xfe\xed\xfa\xcf", "application/x-mach-binary"),
    (b"\xce\xfa\xed\xfe", "application/x-mach-binary"),
    (b"\xcf\xfa\xed\xfe", "application/x-mach-binary"),
    (b"\xca\xfe\xba\xbe", "application/x-mach-binary"),
)


def sanitize_filename(raw: str | None, *, fallback: str = "download", max_len: int = 120) -> str:
    """Turn an untrusted title or remote header filename into a safe ASCII filename."""
    text = (raw or "").replace("/", "_").replace("\\", "_").replace("\x00", "")
    cleaned = _SAFE_NAME_RE.sub("_", text)
    cleaned = _MULTI_SPACE_RE.sub("_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip(" ._")
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    return cleaned[:max_len]


def build_content_disposition(filename: str) -> str:
    """Build an RFC 5987 safe attachment Content-Disposition header."""
    safe_ascii = sanitize_filename(filename)
    encoded = quote(safe_ascii, safe="")
    return f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{encoded}'


def ensure_safe_path(base_dir: Path, target: Path) -> Path:
    """Verify `target` resolves strictly inside `base_dir` (blocks path traversal)."""
    base_resolved = base_dir.resolve()
    target_resolved = target.resolve()
    try:
        target_resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise FileValidationError(f"Path traversal blocked: {target}") from exc
    return target_resolved


def check_free_disk_floor(directory: Path, min_free_bytes: int) -> int:
    """Ensure disk has at least `min_free_bytes` available; returns free bytes."""
    directory.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(directory)
    if usage.free < min_free_bytes:
        raise DiskQuotaError(
            f"Insufficient free disk space: {usage.free} bytes free, floor is {min_free_bytes} bytes."
        )
    return usage.free


def detect_magic_mime(source: Path | bytes) -> tuple[str | None, bool]:
    """Inspect leading magic bytes. Returns (detected_mime, is_dangerous_executable)."""
    if isinstance(source, Path):
        with source.open("rb") as fh:
            header = fh.read(64)
    else:
        header = source[:64]

    if not header:
        return None, False

    for sig, mime in _EXECUTABLE_SIGNATURES:
        if header.startswith(sig):
            return mime, True

    if header.startswith(b"%PDF-"):
        return "application/pdf", False
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", False
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", False
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", False
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp", False
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return "audio/wav", False
    if header[:4] == b"RIFF" and header[8:12] == b"AVI ":
        return "video/x-msvideo", False
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in {b"M4A ", b"M4B "}:
            return "audio/mp4", False
        if brand in {b"avif", b"avis", b"heic", b"mif1"}:
            return "image/avif" if brand.startswith(b"avi") else "image/heic", False
        return "video/mp4", False
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm", False
    if header.startswith(b"ID3") or header[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return "audio/mpeg", False
    if header.startswith(b"OggS"):
        return "audio/ogg", False
    if header.startswith(b"fLaC"):
        return "audio/flac", False
    if header.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return "application/zip", False
    if header.startswith(b"\x1f\x8b"):
        return "application/gzip", False

    stripped = header.lstrip().lower()
    if stripped.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return "text/html", False
    if stripped.startswith(b"#extm3u"):
        return "application/vnd.apple.mpegurl", False
    if stripped.startswith(b"<svg") or (stripped.startswith(b"<?xml") and b"<svg" in stripped):
        return "image/svg+xml", False

    return None, False
