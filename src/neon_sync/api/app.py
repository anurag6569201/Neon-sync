"""FastAPI application factory, lifecycle management, Neon Sync telemetry middleware & DI container."""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from neon_sync import __version__
from neon_sync.adapters import (
    Aria2Adapter,
    DirectHttpAdapter,
    FfmpegAdapter,
    GalleryDlAdapter,
    NM3u8DlReAdapter,
    PlaywrightFallbackAdapter,
    StreamlinkAdapter,
    YtDlpAdapter,
)
from neon_sync.agent import NeonSyncAgent
from neon_sync.auth import AuthDatabase, AuthService
from neon_sync.automation import AutomationEngine, AutomationStore
from neon_sync.config import Settings, get_settings
from neon_sync.pipeline import (
    ArtifactStorage,
    ExtractionOrchestrator,
    InMemoryJobStore,
    JobManager,
    JobStore,
    LocalArtifactStorage,
    SlidingWindowRateLimiter,
    SQLiteJobStore,
    TierManager,
    build_storage,
)
from neon_sync.registry import EngineRegistry

from .auth_routes import auth_router
from .automation_routes import automation_router
from .branding import sanitize_public_text
from .routes import router as v1_router
from .stream_routes import LiveStreamRegistry, stream_router
from .telemetry import (
    ApiLogEntry,
    ApiTelemetryHub,
    classify_api_event,
    configure_neon_logging,
    redact_headers,
    redact_payload,
)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_INDEX = _STATIC_DIR / "index.html"
_REQ_ID_SAFE_RE = re.compile(r"^[a-zA-Z0-9._-]{4,64}$")
_MAX_CAPTURE_BYTES = 32 * 1024


@dataclass
class AppContainer:
    settings: Settings
    registry: EngineRegistry
    storage: ArtifactStorage
    job_store: JobStore
    auth_db: AuthDatabase
    auth: AuthService
    orchestrator: ExtractionOrchestrator
    job_manager: JobManager
    tier_manager: TierManager
    agent: NeonSyncAgent
    automations: AutomationEngine
    telemetry: ApiTelemetryHub
    stream_registry: LiveStreamRegistry
    probe_limiter: SlidingWindowRateLimiter
    download_limiter: SlidingWindowRateLimiter


def _summarize_api_call(
    method: str,
    path: str,
    status_code: int,
    req_json: Any,
    resp_json: Any,
) -> tuple[str, str | None]:
    """Generate a concise, developer-friendly Neon Sync log summary and optional job_id."""
    job_id: str | None = None
    if isinstance(resp_json, dict):
        if isinstance(resp_json.get("job_id"), str):
            job_id = resp_json["job_id"]
        if status_code >= 400:
            detail = resp_json.get("detail")
            if isinstance(detail, dict):
                code = detail.get("code", "ERR_REQUEST")
                msg = detail.get("message", "")
                return sanitize_public_text(f"{code}: {msg}" if msg else str(code)), job_id
            if isinstance(detail, str):
                return sanitize_public_text(detail), job_id

    if path.startswith("/v1/jobs/") and job_id is None:
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[2].startswith("job_"):
            job_id = parts[2]

    if path == "/v1/stream/resolve" and status_code < 400 and isinstance(resp_json, dict):
        title = resp_json.get("title") or "stream"
        tracks = len(resp_json.get("tracks") or [])
        return sanitize_public_text(f'Resolved live stream "{title}" ({tracks} playable tracks)'), job_id

    if path == "/v1/probe" and status_code < 400 and isinstance(resp_json, dict):
        title = resp_json.get("title") or resp_json.get("url") or "source"
        ct = resp_json.get("content_type", "media")
        fmts = len(resp_json.get("available_formats") or [])
        return sanitize_public_text(f'Probed "{title}" ({ct}, {fmts} formats)'), job_id

    if path == "/v1/jobs" and method == "POST" and status_code < 400:
        mode = req_json.get("mode", "auto") if isinstance(req_json, dict) else "auto"
        return f"Queued extraction job ({mode} mode)", job_id

    if path == "/v1/jobs/batch" and status_code < 400 and isinstance(resp_json, list):
        return f"Queued batch of {len(resp_json)} extraction jobs", job_id

    if path == "/v1/agent/run" and status_code < 400:
        prompt = req_json.get("prompt", "") if isinstance(req_json, dict) else ""
        short_p = prompt[:72] + ("…" if len(prompt) > 72 else "")
        return f'Streaming Neon AI workflow: "{short_p}"' if short_p else "Streaming Neon AI workflow", job_id

    if path.startswith("/v1/artifacts/") and status_code < 400:
        filename = path.rsplit("/", 1)[-1]
        return f"Delivered verified artifact {filename}", job_id

    if path == "/v1/status":
        return "Platform status & runtime capabilities checked", job_id

    if path == "/v1/health":
        return "Service health check OK", job_id

    if path == "/v1/formats":
        return "Returned format & mastering preset catalog", job_id

    if path == "/v1/tier":
        tier = resp_json.get("tier", "guest") if isinstance(resp_json, dict) else "guest"
        return f"Resolved account tier: {tier}", job_id

    return f"{method} {path} -> {status_code}", job_id


class NeonSyncTelemetryMiddleware:
    """Pure-ASGI Neon Sync request tracing, white-label headers & live console telemetry."""

    def __init__(self, app: ASGIApp, container: AppContainer) -> None:
        self.app = app
        self.container = container

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET").upper()
        raw_headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        incoming_req_id = raw_headers.get("x-request-id", "").strip()
        request_id = (
            incoming_req_id
            if _REQ_ID_SAFE_RE.match(incoming_req_id)
            else f"ns_req_{uuid.uuid4().hex[:12]}"
        )
        scope.setdefault("state", {})["request_id"] = request_id

        is_v1 = path.startswith("/v1/")
        skip_log = not is_v1 or path.startswith(("/v1/logs", "/v1/stream/hls/", "/v1/stream/sub/"))
        started = time.monotonic()

        # Capture small JSON request bodies without breaking downstream consumers
        req_chunks: list[bytes] = []
        req_size = 0
        req_ct = raw_headers.get("content-type", "")
        capture_req = (not skip_log) and method in {"POST", "PUT", "PATCH"} and "application/json" in req_ct

        async def _receive_wrapper() -> Message:
            nonlocal req_size
            message = await receive()
            if capture_req and message["type"] == "http.request":
                chunk = message.get("body", b"")
                if chunk and req_size < _MAX_CAPTURE_BYTES:
                    req_chunks.append(chunk[: _MAX_CAPTURE_BYTES - req_size])
                    req_size += len(chunk)
            return message

        status_code = 500
        resp_content_type = ""
        resp_chunks: list[bytes] = []
        resp_size = 0

        async def _send_wrapper(message: Message) -> None:
            nonlocal status_code, resp_content_type, resp_size
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                elapsed_ms = (time.monotonic() - started) * 1000.0
                headers = list(message.get("headers", []))
                for k, v in headers:
                    if k.lower() == b"content-type":
                        resp_content_type = v.decode("latin-1").lower()
                # Remove generic Server header if present & inject Neon Sync headers
                headers = [
                    (k, v)
                    for k, v in headers
                    if k.lower()
                    not in {b"x-request-id", b"x-neon-sync-version", b"x-powered-by", b"x-response-time-ms"}
                ]
                headers.extend(
                    [
                        (b"x-request-id", request_id.encode("ascii")),
                        (b"x-neon-sync-version", __version__.encode("ascii")),
                        (b"x-powered-by", b"Neon Sync"),
                        (b"x-response-time-ms", f"{elapsed_ms:.1f}".encode("ascii")),
                    ]
                )
                message = {**message, "headers": headers}
            elif (
                not skip_log
                and message["type"] == "http.response.body"
                and "application/json" in resp_content_type
            ):
                chunk = message.get("body", b"")
                if chunk and resp_size < _MAX_CAPTURE_BYTES:
                    resp_chunks.append(chunk[: _MAX_CAPTURE_BYTES - resp_size])
                    resp_size += len(chunk)
            await send(message)

        try:
            await self.app(scope, _receive_wrapper, _send_wrapper)
        finally:
            if not skip_log:
                duration_ms = round((time.monotonic() - started) * 1000.0, 1)
                req_json: Any = None
                if req_chunks:
                    try:
                        req_json = json.loads(b"".join(req_chunks).decode("utf-8", errors="replace"))
                    except Exception:  # noqa: BLE001
                        req_json = None

                resp_json: Any = None
                if resp_chunks:
                    try:
                        resp_json = json.loads(b"".join(resp_chunks).decode("utf-8", errors="replace"))
                    except Exception:  # noqa: BLE001
                        resp_json = None

                event_slug, svc = classify_api_event(method, path, status_code)
                summary, job_id = _summarize_api_call(method, path, status_code, req_json, resp_json)
                level = "ERROR" if status_code >= 500 else ("WARN" if status_code >= 400 else "INFO")
                pro_key = raw_headers.get("x-neon-pro-key")
                byok_key = raw_headers.get("x-gemini-api-key")
                tier = self.container.tier_manager.resolve_tier(pro_key, byok_key)
                client_tuple = scope.get("client")
                client_ip = raw_headers.get("x-forwarded-for", "").split(",")[0].strip() or (
                    client_tuple[0] if client_tuple else "local"
                )

                self.container.telemetry.record(
                    ApiLogEntry(
                        request_id=request_id,
                        level=level,
                        service=svc,
                        event=event_slug,
                        method=method,
                        path=path,
                        status_code=status_code,
                        duration_ms=duration_ms,
                        client=client_ip,
                        tier=tier,
                        job_id=job_id,
                        summary=summary,
                        request_headers=redact_headers(raw_headers),
                        request_body=redact_payload(req_json) if req_json is not None else None,
                        response_preview=redact_payload(resp_json) if resp_json is not None else None,
                    ),
                    count_as_http=True,
                )


def build_default_registry(settings: Settings) -> EngineRegistry:
    """Register all internal extraction & media mastering adapters."""
    registry = EngineRegistry()
    registry.register_extractor(DirectHttpAdapter(settings))
    registry.register_extractor(Aria2Adapter(settings))
    registry.register_extractor(YtDlpAdapter(settings))
    registry.register_extractor(GalleryDlAdapter(settings))
    registry.register_extractor(StreamlinkAdapter(settings))
    registry.register_extractor(NM3u8DlReAdapter(settings))
    registry.register_extractor(PlaywrightFallbackAdapter(settings))
    registry.register_postprocessor(FfmpegAdapter(settings))
    return registry


def create_app(
    settings: Settings | None = None,
    *,
    registry: EngineRegistry | None = None,
    auth_service: AuthService | None = None,
) -> FastAPI:
    cfg = settings or get_settings()
    reg = registry or build_default_registry(cfg)
    storage = build_storage(cfg)
    job_store: JobStore = SQLiteJobStore(cfg.job_db_path) if cfg.job_db_path else InMemoryJobStore()
    auth_db = auth_service.db if auth_service is not None else AuthDatabase(cfg.auth_db_path)
    auth = auth_service or AuthService(cfg, auth_db)
    telemetry = ApiTelemetryHub(max_entries=300)
    orchestrator = ExtractionOrchestrator(cfg, reg, storage)
    job_manager = JobManager(
        cfg,
        orchestrator,
        store=job_store,
        on_pipeline_event=telemetry.record_pipeline_event,
    )
    tier_manager = TierManager(cfg)
    agent = NeonSyncAgent(cfg, orchestrator, job_manager, tier_manager)
    auto_store = AutomationStore(cfg.automations_db_path)
    automations = AutomationEngine(cfg, auto_store, orchestrator, job_manager, agent)
    stream_registry = LiveStreamRegistry()

    container = AppContainer(
        settings=cfg,
        registry=reg,
        storage=storage,
        job_store=job_store,
        auth_db=auth_db,
        auth=auth,
        orchestrator=orchestrator,
        job_manager=job_manager,
        tier_manager=tier_manager,
        agent=agent,
        automations=automations,
        telemetry=telemetry,
        stream_registry=stream_registry,
        probe_limiter=SlidingWindowRateLimiter(cfg.rate_limit_probes_per_minute, 60.0),
        download_limiter=SlidingWindowRateLimiter(cfg.rate_limit_downloads_per_hour, 3600.0),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_neon_logging()
        await container.registry.refresh_health()
        container.automations.start_scheduler()
        container.telemetry.record(
            ApiLogEntry(
                request_id="ns_sys_boot",
                level="INFO",
                service="neon-sync-core",
                event="system.startup.ready",
                method="SYSTEM",
                path="/v1/status",
                status_code=200,
                summary=f"Neon Sync v{__version__} runtime initialized ({cfg.environment})",
            ),
            count_as_http=False,
        )
        yield
        await container.automations.stop_scheduler()
        await container.job_manager.shutdown()
        if isinstance(job_store, SQLiteJobStore):
            job_store.close()
        if isinstance(storage, LocalArtifactStorage):
            storage.close()
        auto_store.close()
        auth_db.close()

    app = FastAPI(
        title="Neon Sync API",
        version=__version__,
        description=(
            "Unified Neon Sync Media Extraction, Live Streaming, Autonomous AI Agents, "
            "Automations, and Authentication Platform API."
        ),
        lifespan=lifespan,
    )
    app.state.container = container
    app.add_middleware(NeonSyncTelemetryMiddleware, container=container)
    app.include_router(v1_router, prefix="/v1")
    app.include_router(auth_router, prefix="/v1")
    app.include_router(stream_router, prefix="/v1")
    app.include_router(automation_router, prefix="/v1")
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def web_ui() -> HTMLResponse:
        if _STATIC_INDEX.exists():
            return HTMLResponse(_STATIC_INDEX.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Neon Sync API</h1><p>Open <a href='/docs'>/docs</a></p>")

    return app


def run() -> None:
    cfg = get_settings()
    configure_neon_logging()
    uvicorn.run(
        "neon_sync.api.app:create_app",
        factory=True,
        host=cfg.api_host,
        port=cfg.api_port,
        reload=(cfg.environment == "development"),
        access_log=False,
    )


if __name__ == "__main__":
    run()
