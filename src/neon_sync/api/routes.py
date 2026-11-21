"""Versioned REST & SSE API routes (/v1/*) for Neon Sync."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse

from neon_sync import __version__
from neon_sync.adapters import AdapterError
from neon_sync.agent import AgentRequest, extract_url_from_prompt
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
from neon_sync.pipeline import (
    RateLimitExceededError,
    TierEntitlements,
    TierLimitError,
)
from neon_sync.security import (
    UrlValidationError,
    build_content_disposition,
    validate_and_canonicalize_url,
)

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


def _tier_keys(request: Request) -> tuple[str | None, str | None]:
    pro_key = request.headers.get("x-neon-pro-key")
    byok_key = request.headers.get("x-gemini-api-key")
    return (pro_key.strip() if pro_key else None), (byok_key.strip() if byok_key else None)


def _raise_tier_limit(exc: TierLimitError) -> None:
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "code": exc.code,
            "message": exc.message,
            "upgrade_hint": exc.upgrade_hint,
        },
    ) from exc


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


@router.get("/tier", response_model=TierEntitlements, tags=["saas"])
async def get_tier_entitlements(request: Request) -> TierEntitlements:
    """Return current Guest trial balance or Pro/BYOK entitlements for this client."""
    c = _container(request)
    pro_key, byok_key = _tier_keys(request)
    return c.tier_manager.get_entitlements(
        _client_key(request),
        pro_key=pro_key,
        byok_key=byok_key,
    )


@router.post("/agent/run", tags=["agent"])
async def run_agent(body: AgentRequest, request: Request) -> StreamingResponse:
    """Run Neon Sync AI Agent over a natural-language media request and stream live SSE steps."""
    c = _container(request)
    client_key = _client_key(request)
    pro_key, byok_key = _tier_keys(request)

    extracted_url = extract_url_from_prompt(body.prompt, body.url)
    if not extracted_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": ErrorCode.ERR_INVALID_URL.value,
                "message": "Include a valid http(s) URL in your prompt or link field.",
            },
        )
    try:
        validate_and_canonicalize_url(
            extracted_url,
            allow_private=c.settings.allow_private_addresses,
            max_length=c.settings.max_url_length,
        )
    except UrlValidationError as exc:
        raise HTTPException(
            status_code=_error_status_code(exc.code),
            detail={"code": exc.code.value, "message": exc.message},
        ) from exc

    try:
        c.tier_manager.check_and_consume_agent(
            client_key,
            pro_key=pro_key,
            byok_key=byok_key,
        )
    except TierLimitError as exc:
        _raise_tier_limit(exc)

    async def _agent_sse() -> AsyncIterator[str]:
        async for event in c.agent.run_stream(
            body,
            client_key=client_key,
            pro_key=pro_key,
            byok_gemini_key=byok_key,
        ):
            yield f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"

    return StreamingResponse(
        _agent_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
    client_key = _client_key(request)
    pro_key, byok_key = _tier_keys(request)
    if body.allow_browser_fallback and c.settings.enforce_saas_tiers:
        ent = c.tier_manager.get_entitlements(client_key, pro_key=pro_key, byok_key=byok_key)
        if not ent.allow_browser_fallback:
            _raise_tier_limit(
                TierLimitError(
                    "ERR_PRO_FEATURE_REQUIRED",
                    "Tier-3 Playwright headless browser probe is a Pro feature.",
                    upgrade_hint="Unlock Pro to enable JS-rendered browser extraction.",
                )
            )
    try:
        c.probe_limiter.check_and_consume(client_key)
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
    client_key = _client_key(request)
    pro_key, byok_key = _tier_keys(request)

    # Fast ingress validation before consuming rate/tier quotas
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

    try:
        c.tier_manager.check_and_consume_download(
            client_key,
            body,
            pro_key=pro_key,
            byok_key=byok_key,
        )
    except TierLimitError as exc:
        _raise_tier_limit(exc)

    try:
        c.download_limiter.check_and_consume(client_key)
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": ErrorCode.ERR_RATE_LIMITED.value, "message": str(exc)},
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
    client_key = _client_key(request)
    pro_key, byok_key = _tier_keys(request)

    # Validate all URLs before consuming rate/tier budget or enqueuing partial batches
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

    ent = c.tier_manager.get_entitlements(client_key, pro_key=pro_key, byok_key=byok_key)
    if c.settings.enforce_saas_tiers and len(cleaned_urls) > ent.max_batch_urls:
        _raise_tier_limit(
            TierLimitError(
                "ERR_PRO_FEATURE_REQUIRED",
                f"Guest batch queue supports up to {ent.max_batch_urls} URLs at once (Pro supports up to 10).",
                upgrade_hint="Unlock Pro for 10x concurrent batch downloads.",
            )
        )

    shared = body.model_dump(exclude={"urls"})
    records: list[JobRecord] = []
    for url in cleaned_urls:
        dl_req = DownloadRequest(url=url, **shared)
        try:
            c.tier_manager.check_and_consume_download(
                client_key,
                dl_req,
                pro_key=pro_key,
                byok_key=byok_key,
            )
        except TierLimitError as exc:
            _raise_tier_limit(exc)
        try:
            c.download_limiter.check_and_consume(client_key)
        except RateLimitExceededError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"code": ErrorCode.ERR_RATE_LIMITED.value, "message": str(exc)},
            ) from exc
        records.append(await c.job_manager.submit(dl_req))
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
    c = _container(request)
    artifact = c.storage.resolve(job_id, filename)
    if artifact is None or artifact.disk_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found or expired")

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
