"""Versioned REST & SSE API routes (/v1/*) for Neon Sync."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse

from neon_sync import __version__
from neon_sync.adapters import AdapterError
from neon_sync.agent import AgentRequest, extract_url_from_prompt
from neon_sync.cloud import get_cloud_integrations_status
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
    RateLimitExceededError,
    TierEntitlements,
    TierLimitError,
)
from neon_sync.security import (
    UrlValidationError,
    build_content_disposition,
    validate_and_canonicalize_url,
)

from .auth_routes import resolve_request_auth
from .branding import (
    PlatformStatusResponse,
    brand_agent_payload,
    brand_error_detail,
    brand_job_record,
    brand_media_output,
    brand_progress_event,
    build_public_capabilities,
    sanitize_public_text,
)
from .telemetry import LogsPageResponse

if TYPE_CHECKING:
    from .app import AppContainer

router = APIRouter()


def _container(request: Request) -> AppContainer:
    return request.app.state.container


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _client_key(request: Request) -> str:
    auth_ctx = resolve_request_auth(request)
    if auth_ctx.user is not None:
        return f"user:{auth_ctx.user.id}"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _tier_context(request: Request) -> tuple[str | None, str | None, str | None]:
    pro_key = request.headers.get("x-neon-pro-key")
    byok_key = request.headers.get("x-gemini-api-key")
    auth_ctx = resolve_request_auth(request)
    account_tier = auth_ctx.user.tier if auth_ctx.user is not None else None
    return (
        pro_key.strip() if pro_key else None,
        byok_key.strip() if byok_key else None,
        account_tier,
    )


def _raise_tier_limit(exc: TierLimitError, request: Request) -> None:
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=brand_error_detail(
            exc.code,
            exc.message,
            upgrade_hint=exc.upgrade_hint,
            request_id=_request_id(request),
        ),
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


def _build_platform_status(c: AppContainer) -> PlatformStatusResponse:
    manifests = c.registry.list_manifests()
    capabilities = build_public_capabilities(manifests)
    ready_count = sum(1 for cap in capabilities if cap.status == "operational")
    recent = len(c.job_manager.list_jobs(limit=200))
    metrics = c.telemetry.metrics(active_jobs=c.job_manager.active_count, recent_jobs=recent)
    core_ok = any(cap.id == "neon-direct-transfer" and cap.status == "operational" for cap in capabilities)
    return PlatformStatusResponse(
        status="operational" if core_ok else "degraded",
        service="neon-sync",
        version=__version__,
        environment=c.settings.environment,
        capabilities_ready=ready_count,
        capabilities_total=len(capabilities),
        capabilities=capabilities,
        cloud_integrations=get_cloud_integrations_status(c.settings),
        metrics=metrics,
        security={
            "ssrf_guard": "active",
            "egress_isolation": "active" if c.settings.egress_proxy_url else "standard",
            "artifact_signing": "sha256",
            "turnstile_shield": "active" if c.settings.cf_turnstile_secret else "standby",
            "sandbox_headers": "strict",
        },
    )


@router.get("/health", tags=["system"])
async def health(request: Request) -> dict:
    """Return lightweight Neon Sync service health and capability availability."""
    c = _container(request)
    st = _build_platform_status(c)
    return {
        "status": "ok" if st.status == "operational" else "degraded",
        "service": "neon-sync",
        "version": __version__,
        "environment": c.settings.environment,
        "uptime_seconds": st.metrics.uptime_seconds,
        "capabilities_ready": st.capabilities_ready,
        "capabilities_total": st.capabilities_total,
        "capabilities": {cap.id: cap.status for cap in st.capabilities},
    }


@router.get("/status", response_model=PlatformStatusResponse, tags=["system"])
async def platform_status(request: Request) -> PlatformStatusResponse:
    """Return detailed Neon Sync runtime capability health, security posture & live API metrics."""
    return _build_platform_status(_container(request))


@router.post("/status/refresh", response_model=PlatformStatusResponse, tags=["system"])
async def refresh_platform_status(request: Request) -> PlatformStatusResponse:
    """Re-verify Neon Sync runtime capability health and return updated status."""
    c = _container(request)
    await c.registry.refresh_health()
    return _build_platform_status(c)


@router.get("/logs", response_model=LogsPageResponse, tags=["developer"])
async def list_api_logs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=300),
    level: str | None = Query(default=None),
    status_group: str | None = Query(default=None, alias="status"),
    service: str | None = Query(default=None),
    q: str | None = Query(default=None),
) -> LogsPageResponse:
    """Return recent structured Neon Sync API request & pipeline console logs."""
    c = _container(request)
    entries = c.telemetry.list_logs(
        limit=limit,
        level=level,
        status_group=status_group,
        service=service,
        search=q,
    )
    recent_jobs = len(c.job_manager.list_jobs(limit=200))
    return LogsPageResponse(
        total_buffered=c.telemetry.total_buffered,
        returned=len(entries),
        metrics=c.telemetry.metrics(active_jobs=c.job_manager.active_count, recent_jobs=recent_jobs),
        logs=entries,
    )


@router.delete("/logs", tags=["developer"])
async def clear_api_logs(request: Request) -> dict:
    """Clear buffered Neon Sync API console logs."""
    cleared = _container(request).telemetry.clear()
    return {"status": "ok", "service": "neon-sync", "cleared": cleared}


@router.get("/logs/stream", tags=["developer"])
async def stream_api_logs(request: Request) -> StreamingResponse:
    """Stream live Neon Sync API & pipeline console logs over Server-Sent Events (SSE)."""
    c = _container(request)

    async def _log_sse() -> AsyncIterator[str]:
        yield 'event: ready\ndata: {"service":"neon-sync","status":"streaming"}\n\n'
        async for entry in c.telemetry.subscribe():
            if await request.is_disconnected():
                break
            yield f"event: log\ndata: {entry.model_dump_json()}\n\n"

    return StreamingResponse(
        _log_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/formats", response_model=FormatCatalog, tags=["system"])
async def list_format_catalog() -> FormatCatalog:
    """Return the declarative catalog of supported qualities, codecs, formats & presets."""
    return get_format_catalog()


@router.get("/tier", response_model=TierEntitlements, tags=["account"])
async def get_tier_entitlements(request: Request) -> TierEntitlements:
    """Return current Guest trial balance or Pro/BYOK/Account entitlements for this client."""
    c = _container(request)
    pro_key, byok_key, account_tier = _tier_context(request)
    return c.tier_manager.get_entitlements(
        _client_key(request),
        pro_key=pro_key,
        byok_key=byok_key,
        account_tier=account_tier,
    )


@router.post("/agent/run", tags=["agent"])
async def run_agent(body: AgentRequest, request: Request) -> StreamingResponse:
    """Run Neon Sync AI Agent over a natural-language media request and stream live SSE steps."""
    c = _container(request)
    client_key = _client_key(request)
    pro_key, byok_key, account_tier = _tier_context(request)

    extracted_url = extract_url_from_prompt(body.prompt, body.url)
    if not extracted_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=brand_error_detail(
                ErrorCode.ERR_INVALID_URL.value,
                "Include a valid http(s) URL in your prompt or link field.",
                request_id=_request_id(request),
            ),
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
            detail=brand_error_detail(
                exc.code.value,
                exc.message,
                request_id=_request_id(request),
            ),
        ) from exc

    try:
        c.tier_manager.check_and_consume_agent(
            client_key,
            pro_key=pro_key,
            byok_key=byok_key,
            account_tier=account_tier,
        )
    except TierLimitError as exc:
        _raise_tier_limit(exc, request)

    async def _agent_sse() -> AsyncIterator[str]:
        async for event in c.agent.run_stream(
            body,
            client_key=client_key,
            pro_key=pro_key,
            byok_gemini_key=byok_key,
            account_tier=account_tier,
        ):
            branded = event.model_copy(
                update={
                    "message": sanitize_public_text(event.message),
                    "data": brand_agent_payload(event.data),
                }
            )
            yield f"event: {branded.type}\ndata: {branded.model_dump_json()}\n\n"

    return StreamingResponse(
        _agent_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/probe", response_model=NormalizedMediaOutput, tags=["extraction"])
async def probe_url(body: ProbeRequest, request: Request) -> NormalizedMediaOutput:
    """Inspect a URL and return normalized media metadata, streams, subtitles & thumbnails."""
    c = _container(request)
    client_key = _client_key(request)
    pro_key, byok_key, account_tier = _tier_context(request)
    if body.allow_browser_fallback and c.settings.enforce_saas_tiers:
        ent = c.tier_manager.get_entitlements(
            client_key,
            pro_key=pro_key,
            byok_key=byok_key,
            account_tier=account_tier,
        )
        if not ent.allow_browser_fallback:
            _raise_tier_limit(
                TierLimitError(
                    "ERR_PRO_FEATURE_REQUIRED",
                    "Dynamic Web Renderer probe is a Pro feature.",
                    upgrade_hint="Unlock Pro to enable JS-rendered media discovery.",
                ),
                request,
            )
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
    pro_key, byok_key, account_tier = _tier_context(request)

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
        c.tier_manager.check_and_consume_download(
            client_key,
            body,
            pro_key=pro_key,
            byok_key=byok_key,
            account_tier=account_tier,
        )
    except TierLimitError as exc:
        _raise_tier_limit(exc, request)

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
    pro_key, byok_key, account_tier = _tier_context(request)

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

    ent = c.tier_manager.get_entitlements(
        client_key,
        pro_key=pro_key,
        byok_key=byok_key,
        account_tier=account_tier,
    )
    if c.settings.enforce_saas_tiers and len(cleaned_urls) > ent.max_batch_urls:
        _raise_tier_limit(
            TierLimitError(
                "ERR_PRO_FEATURE_REQUIRED",
                f"Guest batch queue supports up to {ent.max_batch_urls} URLs at once (Pro supports up to 10).",
                upgrade_hint="Unlock Pro for 10x concurrent batch downloads.",
            ),
            request,
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
                account_tier=account_tier,
            )
        except TierLimitError as exc:
            _raise_tier_limit(exc, request)
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
