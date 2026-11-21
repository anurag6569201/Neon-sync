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
    EngineManifest,
    ErrorCode,
    FormatCatalog,
    InstallationStatus,
    JobRecord,
    NormalizedMediaOutput,
    ProbeRequest,
    get_format_catalog,
)
from neon_sync.pipeline import RateLimitExceededError
from neon_sync.security import UrlValidationError, build_content_disposition

if TYPE_CHECKING:
    from .app import AppContainer

router = APIRouter()


def _container(request: Request) -> AppContainer:
    return request.app.state.container


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
    c = _container(request)
    manifests = c.registry.list_manifests()
    installed = [m.id for m in manifests if m.installation_status == InstallationStatus.INSTALLED]
    missing = [m.id for m in manifests if m.installation_status != InstallationStatus.INSTALLED]
    return {
        "status": "ok",
        "version": __version__,
        "environment": c.settings.environment,
        "engines_installed": installed,
        "engines_missing": missing,
    }


@router.get("/formats", response_model=FormatCatalog, tags=["system"])
async def list_format_catalog() -> FormatCatalog:
    """Return the declarative catalog of supported qualities, codecs, formats & presets."""
    return get_format_catalog()


@router.get("/engines", response_model=list[EngineManifest], tags=["engines"])
async def list_engines(request: Request) -> list[EngineManifest]:
    return _container(request).registry.list_manifests()


@router.post("/engines/refresh", response_model=list[EngineManifest], tags=["engines"])
async def refresh_engines(request: Request) -> list[EngineManifest]:
    c = _container(request)
    await c.registry.refresh_health()
    return c.registry.list_manifests()


@router.post("/probe", response_model=NormalizedMediaOutput, tags=["extraction"])
async def probe_url(body: ProbeRequest, request: Request) -> NormalizedMediaOutput:
    c = _container(request)
    try:
        c.probe_limiter.check_and_consume(_client_key(request))
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": ErrorCode.ERR_RATE_LIMITED.value, "message": str(exc)},
        ) from exc

    try:
        return await c.orchestrator.probe(body)
    except UrlValidationError as exc:
        raise HTTPException(
            status_code=_error_status_code(exc.code),
            detail={"code": exc.code.value, "message": exc.message},
        ) from exc
    except AdapterError as exc:
        raise HTTPException(
            status_code=_error_status_code(exc.code),
            detail={
                "code": exc.code.value,
                "message": exc.message,
                "engine": exc.engine_id,
                "retryable": exc.retryable,
            },
        ) from exc


@router.post(
    "/jobs",
    response_model=JobRecord,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["jobs"],
)
async def create_job(body: DownloadRequest, request: Request) -> JobRecord:
    c = _container(request)
    try:
        c.download_limiter.check_and_consume(_client_key(request))
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": ErrorCode.ERR_RATE_LIMITED.value, "message": str(exc)},
        ) from exc

    # Fast ingress validation before enqueuing
    from neon_sync.security import validate_and_canonicalize_url

    try:
        validate_and_canonicalize_url(
            body.url,
            allow_private=c.settings.allow_private_addresses,
            max_length=c.settings.max_url_length,
        )
    except UrlValidationError as exc:
        raise HTTPException(
            status_code=_error_status_code(exc.code),
            detail={"code": exc.code.value, "message": exc.message},
        ) from exc

    return await c.job_manager.submit(body)


@router.post(
    "/jobs/batch",
    response_model=list[JobRecord],
    status_code=status.HTTP_202_ACCEPTED,
    tags=["jobs"],
)
async def create_batch_jobs(body: BatchDownloadRequest, request: Request) -> list[JobRecord]:
    """Validate and enqueue up to 10 URLs sharing the same advanced extraction settings."""
    c = _container(request)
    from neon_sync.security import validate_and_canonicalize_url

    client_key = _client_key(request)
    # Validate all URLs before consuming rate budget or enqueuing partial batches
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
                detail={"code": exc.code.value, "message": f"{u}: {exc.message}"},
            ) from exc
        cleaned_urls.append(u)

    if not cleaned_urls:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": ErrorCode.ERR_INVALID_URL.value, "message": "At least one URL is required."},
        )

    shared = body.model_dump(exclude={"urls"})
    records: list[JobRecord] = []
    for url in cleaned_urls:
        try:
            c.download_limiter.check_and_consume(client_key)
        except RateLimitExceededError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"code": ErrorCode.ERR_RATE_LIMITED.value, "message": str(exc)},
            ) from exc
        records.append(await c.job_manager.submit(DownloadRequest(url=url, **shared)))
    return records


@router.get("/jobs", response_model=list[JobRecord], tags=["jobs"])
async def list_jobs(request: Request, limit: int = 50) -> list[JobRecord]:
    return _container(request).job_manager.list_jobs(limit=max(1, min(limit, 200)))


@router.get("/jobs/{job_id}", response_model=JobRecord, tags=["jobs"])
async def get_job(job_id: str, request: Request) -> JobRecord:
    record = _container(request).job_manager.get_job(job_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return record


@router.post("/jobs/{job_id}/cancel", response_model=JobRecord, tags=["jobs"])
async def cancel_job(job_id: str, request: Request) -> JobRecord:
    record = await _container(request).job_manager.cancel(job_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return record


@router.get("/jobs/{job_id}/events", tags=["jobs"])
async def stream_job_events(job_id: str, request: Request) -> StreamingResponse:
    c = _container(request)
    if c.job_manager.get_job(job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    async def _sse_stream() -> AsyncIterator[str]:
        async for event in c.job_manager.subscribe(job_id):
            yield f"event: progress\ndata: {event.model_dump_json()}\n\n"

    return StreamingResponse(
        _sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/artifacts/{job_id}/{filename}", tags=["artifacts"])
async def download_artifact(job_id: str, filename: str, request: Request) -> FileResponse:
    c = _container(request)
    artifact = c.storage.resolve(job_id, filename)
    if artifact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found or expired")

    return FileResponse(
        path=artifact.disk_path,
        media_type=artifact.mime_type,
        headers={
            "Content-Disposition": build_content_disposition(artifact.filename),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, max-age=300",
        },
    )
