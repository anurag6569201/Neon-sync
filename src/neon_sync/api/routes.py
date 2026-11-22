"""Versioned REST & SSE API routes (/v1/*) for Neon Sync."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse

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
from neon_sync.pipeline import RateLimitExceededError
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

    try:
        c.download_limiter.check_and_consume(client_key)
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

    record = await c.job_manager.submit(body)
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
    records: list[JobRecord] = []
    for url in cleaned_urls:
        dl_req = DownloadRequest(url=url, **shared)
        try:
            c.download_limiter.check_and_consume(client_key)
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
        records.append(brand_job_record(await c.job_manager.submit(dl_req)))
    return records


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
