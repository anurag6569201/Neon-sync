"""Neon Sync Public White-Label Branding & Sanitization Boundary.

Ensures every public API response, SSE stream, error detail, and health payload
presents a unified **Neon Sync** surface without exposing internal extraction
engines, binaries, host paths, or third-party implementation details.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from neon_sync import __version__
from neon_sync.models import (
    EngineManifest,
    EngineProvenance,
    ErrorInfo,
    InstallationStatus,
    JobRecord,
    NormalizedMediaOutput,
    ProgressEvent,
    WarningInfo,
)

PUBLIC_ENGINE_ID = "neon-sync"
PUBLIC_MEDIA_PROCESSOR_ID = "neon-sync-media"

# Internal engine/binary tokens that must NEVER appear in public API responses or logs.
_SANITIZE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:yt[-_]?dlp|ytdlp)\b", re.IGNORECASE), "Neon Sync"),
    (re.compile(r"\b(?:gallery[-_]?dl)\b", re.IGNORECASE), "Neon Sync"),
    (re.compile(r"\b(?:n[-_]?m3u8dl[-_]?re|nm3u8dl)\b", re.IGNORECASE), "Neon Sync"),
    (re.compile(r"\bstreamlink\b", re.IGNORECASE), "Neon Sync"),
    (re.compile(r"\baria2c?\b", re.IGNORECASE), "Neon Sync"),
    (re.compile(r"\b(?:ffprobe|ffmpeg)\b", re.IGNORECASE), "Neon Sync Media"),
    (re.compile(r"\b(?:playwright|chromium)\b", re.IGNORECASE), "Neon Sync Renderer"),
    (re.compile(r"\b(?:trafilatura|beautifulsoup4?|lxml|pillow)\b", re.IGNORECASE), "Neon Sync"),
    (re.compile(r"\bdirect[-_]?http\b", re.IGNORECASE), "Neon Sync"),
    (re.compile(r"\bEngine\s+Registry\b", re.IGNORECASE), "Neon Sync Core"),
    (re.compile(r"\bcandidate\s+engines?\b", re.IGNORECASE), "Neon Sync pipeline"),
    (re.compile(r"\bextraction\s+engines?\b", re.IGNORECASE), "Neon Sync"),
    (re.compile(r"\bTier-3\s+Neon\s+Sync\s+Renderer\s+headless\s+browser\b", re.IGNORECASE), "Dynamic Web Renderer"),
    (re.compile(r"\bTier-3\s+headless\s+browser\b", re.IGNORECASE), "Dynamic Web Renderer"),
    # Strip raw subprocess exit / path noise from public error strings
    (re.compile(r"\s*\(rc=-?\d+\)"), ""),
    (re.compile(r"\bexited with code -?\d+:?\s*", re.IGNORECASE), "failed: "),
    (re.compile(r"(?:/Users/|/home/|/tmp/|/var/|/opt/|/private/var/)[^\s'\",;:)]+"), "[redacted-path]"),
]

# Collapse duplicate "Neon Sync Neon Sync" artifacts if replacement stacks
_DUP_BRAND_RE = re.compile(r"\bNeon Sync(?:\s+Neon Sync)+\b", re.IGNORECASE)


class CapabilityStatus(BaseModel):
    """Public Neon Sync runtime capability module (replaces internal engine manifests)."""

    id: str
    name: str
    category: Literal["extraction", "transfer", "streaming", "processing", "rendering"]
    status: Literal["operational", "degraded", "standby"]
    description: str
    supported_types: list[str] = Field(default_factory=list)
    version: str = __version__


def sanitize_public_text(text: str | None) -> str:
    """Scrub all internal engine names, binary identifiers, and host paths from public text."""
    if not text:
        return ""
    cleaned = str(text)
    for pattern, replacement in _SANITIZE_RULES:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = _DUP_BRAND_RE.sub("Neon Sync", cleaned)
    # Collapse excessive whitespace without destroying intentional newlines
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned


def build_public_capabilities(manifests: list[EngineManifest]) -> list[CapabilityStatus]:
    """Map internal adapter health manifests into unified Neon Sync capability modules."""
    by_id = {m.id: m for m in manifests}

    def _is_ready(*ids: str) -> bool:
        return any(
            (m := by_id.get(i)) is not None and m.installation_status == InstallationStatus.INSTALLED
            for i in ids
        )

    def _is_healthy(*ids: str) -> bool:
        return any(
            (m := by_id.get(i)) is not None
            and m.installation_status == InstallationStatus.INSTALLED
            and m.health_status.state.value == "healthy"
            for i in ids
        )

    def _status_for(*ids: str, optional: bool = False) -> Literal["operational", "degraded", "standby"]:
        if _is_healthy(*ids):
            return "operational"
        if _is_ready(*ids):
            return "degraded"
        return "standby" if optional else "degraded"

    return [
        CapabilityStatus(
            id="neon-universal-media",
            name="Universal Video & Audio",
            category="extraction",
            status=_status_for("yt-dlp"),
            description="Multi-platform video, audio, playlist, chapter, and subtitle extraction.",
            supported_types=["video", "audio", "playlist", "subtitles"],
        ),
        CapabilityStatus(
            id="neon-direct-transfer",
            name="Direct & Accelerated Transfer",
            category="transfer",
            status=_status_for("direct-http", "aria2"),
            description="High-throughput verified file transfer with multi-connection acceleration.",
            supported_types=["file", "document", "archive", "webpage"],
        ),
        CapabilityStatus(
            id="neon-gallery-capture",
            name="Image & Gallery Pipeline",
            category="extraction",
            status=_status_for("gallery-dl", optional=True),
            description="Batch image, photo set, and social gallery capture with archive packaging.",
            supported_types=["image", "gallery"],
        ),
        CapabilityStatus(
            id="neon-adaptive-streams",
            name="Live & Adaptive Streams",
            category="streaming",
            status=_status_for("n-m3u8dl-re", "streamlink", "yt-dlp", optional=True),
            description="HLS, DASH, and live broadcast stream discovery and bounded capture.",
            supported_types=["livestream", "hls", "dash"],
        ),
        CapabilityStatus(
            id="neon-media-mastering",
            name="Media Mastering & Transcode",
            category="processing",
            status=_status_for("ffmpeg"),
            description="Zero-egress remuxing, clipping, audio loudness normalization, and image conversion.",
            supported_types=["mp4", "mkv", "webm", "mp3", "flac", "webp"],
        ),
        CapabilityStatus(
            id="neon-dynamic-renderer",
            name="Dynamic Web Renderer",
            category="rendering",
            status=_status_for("playwright", optional=True),
            description="Isolated page rendering for JS media discovery, full-page snapshots, and printable PDFs.",
            supported_types=["webpage", "spa", "pdf", "png"],
        ),
        CapabilityStatus(
            id="neon-web-reader",
            name="Web Reader, Assets & Feeds",
            category="extraction",
            status=_status_for("direct-http"),
            description="Clean article Markdown/text, webpage SVG & image asset packs, and Podcast/RSS feeds.",
            supported_types=["webpage", "markdown", "rss", "podcast"],
        ),
    ]


def brand_media_output(output: NormalizedMediaOutput) -> NormalizedMediaOutput:
    """Return a white-labeled copy of NormalizedMediaOutput with zero engine leakage."""
    out = output.model_copy(deep=True)

    # 1. White-label engine provenance while preserving schema compliance
    has_post_processor = bool(out.engine.post_processor)
    out.engine = EngineProvenance(
        engine_used=PUBLIC_ENGINE_ID,
        engine_version=__version__,
        post_processor=PUBLIC_MEDIA_PROCESSOR_ID if has_post_processor else None,
        fallback_chain=[PUBLIC_ENGINE_ID],
        used_browser_fallback=out.engine.used_browser_fallback,
        used_authentication=out.engine.used_authentication,
        probe_duration_ms=out.engine.probe_duration_ms,
        download_duration_ms=out.engine.download_duration_ms,
    )

    # 2. Normalize source extractor key so internal adapter identifiers never surface
    if out.source.extractor_key:
        out.source.extractor_key = PUBLIC_ENGINE_ID

    # 3. Rename internal ffprobe metadata key -> media_analysis
    if "ffprobe" in out.metadata.extra:
        out.metadata.extra["media_analysis"] = out.metadata.extra.pop("ffprobe")

    # 4. Drop internal fallback-chain warnings; sanitize any remaining warnings
    clean_warnings: list[WarningInfo] = []
    for w in out.warnings:
        if w.code == "WARN_ENGINE_FALLBACK":
            continue
        clean_code = "WARN_MEDIA_PROBE_SKIPPED" if w.code == "WARN_FFPROBE_SKIPPED" else w.code
        clean_warnings.append(
            WarningInfo(
                code=clean_code,
                message=sanitize_public_text(w.message),
                engine=PUBLIC_ENGINE_ID,
            )
        )
    out.warnings = clean_warnings

    # 5. Sanitize errors
    out.errors = [
        ErrorInfo(
            code=e.code,
            message=sanitize_public_text(e.message),
            retryable=e.retryable,
            engine=PUBLIC_ENGINE_ID,
        )
        for e in out.errors
    ]
    return out


def brand_progress_event(event: ProgressEvent) -> ProgressEvent:
    """Return a sanitized, Neon Sync branded copy of a ProgressEvent."""
    ev = event.model_copy(deep=True)
    ev.message = sanitize_public_text(ev.message)
    return ev


def brand_job_record(record: JobRecord) -> JobRecord:
    """Return a white-labeled copy of JobRecord safe for public API responses."""
    rec = record.model_copy(deep=True)
    if rec.progress is not None:
        rec.progress = brand_progress_event(rec.progress)
    if rec.result is not None:
        rec.result = brand_media_output(rec.result)
    if rec.error is not None:
        rec.error = sanitize_public_text(rec.error)
    return rec


def brand_error_detail(
    code: str,
    message: str,
    *,
    retryable: bool | None = None,
    request_id: str | None = None,
    upgrade_hint: str | None = None,
) -> dict[str, Any]:
    """Build a consistent, white-labeled Neon Sync API error payload."""
    detail: dict[str, Any] = {
        "code": code,
        "message": sanitize_public_text(message),
        "service": PUBLIC_ENGINE_ID,
    }
    if retryable is not None:
        detail["retryable"] = retryable
    if upgrade_hint:
        detail["upgrade_hint"] = sanitize_public_text(upgrade_hint)
    if request_id:
        detail["request_id"] = request_id
    return detail
