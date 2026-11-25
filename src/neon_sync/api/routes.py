"""Versioned REST & SSE API routes (/v1/*) for Neon Sync."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Annotated, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from neon_sync import __version__
from neon_sync.adapters import AdapterError
from neon_sync.models import (
    BatchDownloadRequest,
    DownloadRequest,
    ErrorCode,
    FormatCatalog,
    JobRecord,
    NormalizedMediaOutput,
    ProbeRequest,
    get_format_catalog,
)
from neon_sync.pipeline import (
    QueueCapacityError,
    RateLimitExceededError,
    convert_json3_to_vtt,
    normalize_subtitle_to_transcript,
)
from neon_sync.security import (
    UrlValidationError,
    build_content_disposition,
    validate_and_canonicalize_url,
)

from .branding import (
    brand_error_detail,
    brand_job_record,
    brand_media_output,
    brand_progress_event,
    build_public_capabilities,
)

if TYPE_CHECKING:
    from .app import AppContainer

router = APIRouter()
logger = logging.getLogger(__name__)


def _container(request: Request) -> AppContainer:
    return request.app.state.container


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _error_status_code(code: ErrorCode) -> int:
    return {
        ErrorCode.ERR_INVALID_URL: status.HTTP_400_BAD_REQUEST,
        ErrorCode.ERR_SSRF_BLOCKED: status.HTTP_403_FORBIDDEN,
        ErrorCode.ERR_AUTH_REQUIRED: status.HTTP_403_FORBIDDEN,
        ErrorCode.ERR_DRM_PROTECTED: status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode.ERR_UNSUPPORTED_URL: status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode.ERR_NOT_FOUND: status.HTTP_404_NOT_FOUND,
        ErrorCode.ERR_GEO_BLOCKED: status.HTTP_451_UNAVAILABLE_FOR_LEGAL_REASONS,
        ErrorCode.ERR_RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
        ErrorCode.ERR_QUOTA_EXCEEDED: status.HTTP_413_CONTENT_TOO_LARGE,
        ErrorCode.ERR_TIMEOUT: status.HTTP_504_GATEWAY_TIMEOUT,
        ErrorCode.ERR_VALIDATION_FAILED: status.HTTP_422_UNPROCESSABLE_CONTENT,
    }.get(code, status.HTTP_502_BAD_GATEWAY)


@router.get("/health", tags=["system"])
async def health(request: Request) -> dict:
    """Return minimal white-labeled Neon Sync readiness status."""
    c = _container(request)
    manifests = c.registry.list_manifests()
    capabilities = build_public_capabilities(manifests)
    ready_count = sum(1 for cap in capabilities if cap.status == "operational")
    core_ok = any(cap.id == "neon-direct-transfer" and cap.status == "operational" for cap in capabilities)
    return {
        "status": "ok" if core_ok else "degraded",
        "service": "neon-sync",
        "version": __version__,
        "capabilities_ready": ready_count,
        "capabilities_total": len(capabilities),
        "capabilities": {cap.id: cap.status for cap in capabilities},
    }


@router.get("/formats", response_model=FormatCatalog, tags=["system"])
async def list_format_catalog() -> FormatCatalog:
    """Return the declarative catalog of supported qualities, codecs, formats & presets."""
    return get_format_catalog()


@router.get("/capabilities", tags=["system"])
async def runtime_capabilities(request: Request) -> dict:
    """Describe actual runtime coverage and limits without requiring an account."""
    c = _container(request)
    capabilities = build_public_capabilities(c.registry.list_manifests())
    ready_count = sum(cap.status == "operational" for cap in capabilities)
    return {
        "status": "ready" if ready_count == len(capabilities) else (
            "limited" if ready_count else "unavailable"
        ),
        "capabilities": [cap.model_dump(mode="json") for cap in capabilities],
        "limits": {
            "batch_urls": 10,
            "max_file_bytes": c.settings.max_job_bytes,
            "artifact_ttl_seconds": c.settings.artifact_ttl_seconds,
            "concurrent_downloads": c.settings.max_concurrent_downloads,
            "downloads_per_hour": c.settings.rate_limit_downloads_per_hour,
            "playlist_items": c.settings.max_playlist_items,
            "gallery_items": c.settings.max_gallery_items,
            "live_duration_seconds": c.settings.max_livestream_duration_seconds,
        },
        "support_note": (
            "No Neon Sync account or paid plan is required. Available features depend on "
            "this installation and the source. Private, sign-in-only, DRM-protected, "
            "or region-restricted media may be unavailable."
        ),
    }


@router.post("/probe", response_model=NormalizedMediaOutput, tags=["extraction"])
async def probe_url(body: ProbeRequest, request: Request) -> NormalizedMediaOutput:
    """Inspect a URL and return normalized media metadata, streams, subtitles & thumbnails."""
    c = _container(request)
    client_key = _client_key(request)
    try:
        c.probe_limiter.check_and_consume(client_key)
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=brand_error_detail(
                ErrorCode.ERR_RATE_LIMITED.value,
                str(exc),
                retryable=True,
                request_id=_request_id(request),
            ),
        ) from exc

    try:
        result = await c.orchestrator.probe(body)
        return brand_media_output(result)
    except UrlValidationError as exc:
        raise HTTPException(
            status_code=_error_status_code(exc.code),
            detail=brand_error_detail(
                exc.code.value,
                exc.message,
                request_id=_request_id(request),
            ),
        ) from exc
    except AdapterError as exc:
        raise HTTPException(
            status_code=_error_status_code(exc.code),
            detail=brand_error_detail(
                exc.code.value,
                exc.message,
                retryable=exc.retryable,
                request_id=_request_id(request),
            ),
        ) from exc


class SizeProbeRequest(BaseModel):
    urls: list[Annotated[str, Field(min_length=4, max_length=4096)]] = Field(min_length=1, max_length=5)


@router.post("/probe/sizes", tags=["extraction"])
async def probe_sizes(body: SizeProbeRequest, request: Request) -> StreamingResponse:
    """Stream individual metadata results so playlist totals can update progressively."""
    c = _container(request)
    try:
        c.probe_limiter.check_and_consume(_client_key(request))
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=429, headers={"Retry-After": "60"},
            detail=brand_error_detail(ErrorCode.ERR_RATE_LIMITED.value, str(exc), retryable=True),
        ) from exc

    async def inspect(url: str) -> dict:
        try:
            result = await c.orchestrator.probe(ProbeRequest(
                url=url, max_items=1, include_subtitles=False, allow_browser_fallback=False,
            ))
            return {"url": url, "probe": brand_media_output(result).model_dump(mode="json")}
        except (UrlValidationError, AdapterError) as exc:
            return {"url": url, "error": brand_error_detail(
                exc.code.value, exc.message, retryable=getattr(exc, "retryable", False),
            )}
        except Exception:
            logger.exception("Size metadata probe failed")
            return {"url": url, "error": brand_error_detail(
                ErrorCode.ERR_INTERNAL.value, "Size metadata could not be loaded. Try again.",
                retryable=True,
            )}

    async def results() -> AsyncIterator[str]:
        tasks = [asyncio.create_task(inspect(url)) for url in dict.fromkeys(body.urls)]
        try:
            for task in asyncio.as_completed(tasks):
                yield json.dumps(await task) + "\n"
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return StreamingResponse(results(), media_type="application/x-ndjson", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    })


class SubtitlePreviewRequest(BaseModel):
    url: str = Field(min_length=4, max_length=4096)
    language: str = Field(default="en", min_length=1, max_length=24)
    subtitle_mode: Literal["prefer_manual", "manual_only", "include_auto"] = "prefer_manual"


_CUE_TIME_RE = re.compile(
    r"(?P<start>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*(?P<end>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})"
)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _parse_vtt_preview_cues(vtt_text: str, max_cues: int = 250) -> list[dict[str, str]]:
    """Parse VTT/SRT text into lightweight timestamped cue rows for instant UI inspection."""
    cues: list[dict[str, str]] = []
    lines = vtt_text.replace("\r\n", "\n").splitlines()
    i = 0
    last_text = ""
    while i < len(lines) and len(cues) < max_cues:
        m = _CUE_TIME_RE.search(lines[i])
        if not m:
            i += 1
            continue
        start = m.group("start").replace(",", ".").split(".")[0].removeprefix("00:")
        end = m.group("end").replace(",", ".").split(".")[0].removeprefix("00:")
        i += 1
        buf: list[str] = []
        while i < len(lines) and lines[i].strip():
            cleaned = _TAG_STRIP_RE.sub("", lines[i]).strip()
            if cleaned:
                buf.append(cleaned)
            i += 1
        text = " ".join(buf).strip()
        if text and text != last_text:
            cues.append({"start": start, "end": end, "text": text})
            last_text = text
    return cues


@router.post("/preview/subtitles", tags=["extraction"])
async def preview_subtitles(body: SubtitlePreviewRequest, request: Request) -> dict:
    """Fetch & parse a lightweight live caption preview (zero disk artifact, SSRF-safe)."""
    c = _container(request)
    client_key = _client_key(request)
    try:
        c.probe_limiter.check_and_consume(client_key)
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=brand_error_detail(
                ErrorCode.ERR_RATE_LIMITED.value,
                str(exc),
                retryable=True,
                request_id=_request_id(request),
            ),
        ) from exc

    try:
        probed = await c.orchestrator.probe(ProbeRequest(url=body.url, include_subtitles=True))
    except (UrlValidationError, AdapterError) as exc:
        raise HTTPException(
            status_code=_error_status_code(exc.code),
            detail=brand_error_detail(
                exc.code.value,
                exc.message,
                request_id=_request_id(request),
            ),
        ) from exc

    req_lang = body.language.strip().lower()
    candidates = [
        t
        for t in probed.subtitles
        if t.url and (body.subtitle_mode != "manual_only" or not t.is_auto_generated)
    ]
    chosen = None
    for want_auto in (False, True):
        if want_auto and body.subtitle_mode == "manual_only":
            continue
        pool = [t for t in candidates if t.is_auto_generated == want_auto]
        chosen = next((t for t in pool if t.language.lower() == req_lang), None) or next(
            (
                t
                for t in pool
                if t.language.lower().startswith(f"{req_lang}-")
                or t.language.lower().startswith(f"{req_lang}_")
            ),
            None,
        )
        if chosen is not None:
            break

    if chosen is None or not chosen.url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=brand_error_detail(
                ErrorCode.ERR_NOT_FOUND.value,
                f"No previewable caption track found for '{body.language}'.",
                request_id=_request_id(request),
            ),
        )

    try:
        validated = validate_and_canonicalize_url(
            chosen.url,
            allow_private=c.settings.allow_private_addresses,
            max_length=c.settings.max_url_length,
        )
    except UrlValidationError as exc:
        raise HTTPException(
            status_code=_error_status_code(exc.code),
            detail=brand_error_detail(exc.code.value, exc.message, request_id=_request_id(request)),
        ) from exc

    client_kwargs: dict = {
        "timeout": httpx.Timeout(12.0),
        "follow_redirects": True,
        "max_redirects": 4,
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        },
    }
    if c.settings.egress_proxy_url:
        client_kwargs["proxy"] = c.settings.egress_proxy_url

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(validated.canonical_url)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=brand_error_detail(
                ErrorCode.ERR_INTERNAL.value,
                f"Caption preview fetch failed: {exc}",
                retryable=True,
                request_id=_request_id(request),
            ),
        ) from exc

    if resp.status_code == 429:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=brand_error_detail(
                ErrorCode.ERR_RATE_LIMITED.value,
                "Caption source temporarily rate-limited preview; try again in a few seconds.",
                retryable=True,
                request_id=_request_id(request),
            ),
        )
    if resp.status_code != 200 or not resp.text.strip():
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=brand_error_detail(
                ErrorCode.ERR_NOT_FOUND.value,
                f"Caption preview unavailable (HTTP {resp.status_code}).",
                request_id=_request_id(request),
            ),
        )

    raw_text = resp.text
    vtt_text = convert_json3_to_vtt(raw_text) if raw_text.lstrip().startswith("{") else raw_text
    cues = _parse_vtt_preview_cues(vtt_text, max_cues=250)
    plain_text = normalize_subtitle_to_transcript(vtt_text)
    return {
        "language": chosen.language,
        "name": chosen.name,
        "is_auto_generated": chosen.is_auto_generated,
        "cue_count": len(cues),
        "cues": cues,
        "plain_text": plain_text[:25_000],
    }


@router.post(
    "/jobs",
    response_model=JobRecord,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["jobs"],
)
async def create_job(body: DownloadRequest, request: Request) -> JobRecord:
    """Queue an asynchronous Neon Sync media extraction & mastering job."""
    c = _container(request)
    client_key = _client_key(request)

    try:
        validate_and_canonicalize_url(
            body.url,
            allow_private=c.settings.allow_private_addresses,
            max_length=c.settings.max_url_length,
        )
    except UrlValidationError as exc:
        raise HTTPException(
            status_code=_error_status_code(exc.code),
            detail=brand_error_detail(
                exc.code.value,
                exc.message,
                request_id=_request_id(request),
            ),
        ) from exc

    existing = c.job_manager.find_idempotent_job(body)
    if existing is not None:
        return brand_job_record(existing)
    try:
        c.job_manager.ensure_capacity()
        c.download_limiter.check_and_consume(client_key)
    except QueueCapacityError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=brand_error_detail("ERR_QUEUE_FULL", str(exc), retryable=True),
            headers={"Retry-After": "10"},
        ) from exc
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=brand_error_detail(
                ErrorCode.ERR_RATE_LIMITED.value,
                str(exc),
                retryable=True,
                request_id=_request_id(request),
            ),
        ) from exc

    try:
        record = await c.job_manager.submit(body)
    except QueueCapacityError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=brand_error_detail("ERR_QUEUE_FULL", str(exc), retryable=True),
            headers={"Retry-After": "10"},
        ) from exc
    return brand_job_record(record)


@router.post(
    "/jobs/batch",
    response_model=list[JobRecord],
    status_code=status.HTTP_202_ACCEPTED,
    tags=["jobs"],
)
async def create_batch_jobs(body: BatchDownloadRequest, request: Request) -> list[JobRecord]:
    """Validate and enqueue up to 10 URLs sharing the same Neon Sync extraction settings."""
    c = _container(request)
    client_key = _client_key(request)

    cleaned_urls: list[str] = []
    for raw_url in body.urls:
        u = raw_url.strip()
        if not u:
            continue
        try:
            validate_and_canonicalize_url(
                u,
                allow_private=c.settings.allow_private_addresses,
                max_length=c.settings.max_url_length,
            )
        except UrlValidationError as exc:
            raise HTTPException(
                status_code=_error_status_code(exc.code),
                detail=brand_error_detail(
                    exc.code.value,
                    f"{u}: {exc.message}",
                    request_id=_request_id(request),
                ),
            ) from exc
        cleaned_urls.append(u)

    if not cleaned_urls:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=brand_error_detail(
                ErrorCode.ERR_INVALID_URL.value,
                "At least one URL is required.",
                request_id=_request_id(request),
            ),
        )

    shared = body.model_dump(exclude={"urls"})
    # Validate every request and reserve the entire budget before creating work.
    requests = [DownloadRequest(url=url, **shared) for url in cleaned_urls]
    try:
        c.job_manager.ensure_capacity(len(requests))
        c.download_limiter.check_and_consume(client_key, count=len(requests))
    except QueueCapacityError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=brand_error_detail("ERR_QUEUE_FULL", str(exc), retryable=True),
            headers={"Retry-After": "10"},
        ) from exc
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=brand_error_detail(
                ErrorCode.ERR_RATE_LIMITED.value,
                str(exc),
                retryable=True,
                request_id=_request_id(request),
            ),
        ) from exc
    return [brand_job_record(await c.job_manager.submit(req)) for req in requests]


@router.get("/jobs", response_model=list[JobRecord], tags=["jobs"])
async def list_jobs(request: Request, limit: int = 50) -> list[JobRecord]:
    """List recent Neon Sync extraction jobs."""
    jobs = _container(request).job_manager.list_jobs(limit=max(1, min(limit, 200)))
    return [brand_job_record(j) for j in jobs]


@router.get("/jobs/{job_id}", response_model=JobRecord, tags=["jobs"])
async def get_job(job_id: str, request: Request) -> JobRecord:
    """Retrieve a single Neon Sync job record and verified output artifact metadata."""
    record = _container(request).job_manager.get_job(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=brand_error_detail("ERR_NOT_FOUND", "Job not found", request_id=_request_id(request)),
        )
    return brand_job_record(record)


@router.post("/jobs/{job_id}/cancel", response_model=JobRecord, tags=["jobs"])
async def cancel_job(job_id: str, request: Request) -> JobRecord:
    """Cancel an active or queued Neon Sync extraction job."""
    record = await _container(request).job_manager.cancel(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=brand_error_detail("ERR_NOT_FOUND", "Job not found", request_id=_request_id(request)),
        )
    return brand_job_record(record)


@router.get("/jobs/{job_id}/events", tags=["jobs"])
async def stream_job_events(job_id: str, request: Request) -> StreamingResponse:
    """Stream real-time Neon Sync job progress updates over Server-Sent Events (SSE)."""
    c = _container(request)
    if c.job_manager.get_job(job_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=brand_error_detail("ERR_NOT_FOUND", "Job not found", request_id=_request_id(request)),
        )

    async def _sse_stream() -> AsyncIterator[str]:
        async for event in c.job_manager.subscribe(job_id):
            branded = brand_progress_event(event)
            yield f"event: progress\ndata: {branded.model_dump_json()}\n\n"

    return StreamingResponse(
        _sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


_SAFE_INLINE_MIMES = frozenset(
    {
        "video/mp4",
        "video/webm",
        "video/quicktime",
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "audio/ogg",
        "audio/opus",
        "audio/wav",
        "audio/flac",
        "audio/webm",
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "image/avif",
        "application/pdf",
        "text/plain",
        "text/markdown",
    }
)


@router.get("/artifacts/{job_id}/{filename}", tags=["artifacts"])
async def download_artifact(
    job_id: str,
    filename: str,
    request: Request,
    inline: bool = False,
) -> FileResponse:
    """Download or inline-preview a verified Neon Sync output artifact."""
    c = _container(request)
    artifact = c.storage.resolve(job_id, filename)
    if artifact is None or artifact.disk_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=brand_error_detail(
                "ERR_NOT_FOUND",
                "Artifact not found or expired",
                request_id=_request_id(request),
            ),
        )

    base_mime = (artifact.mime_type or "").split(";")[0].strip().lower()
    disposition = (
        f'inline; filename="{artifact.filename}"'
        if (inline and base_mime in _SAFE_INLINE_MIMES)
        else build_content_disposition(artifact.filename)
    )

    return FileResponse(
        path=artifact.disk_path,
        media_type=artifact.mime_type,
        headers={
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; media-src 'self'; img-src 'self'; sandbox",
            "Cache-Control": "private, max-age=300",
        },
    )
