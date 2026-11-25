"""FastAPI application factory, lifecycle management, white-label middleware & DI container."""

from __future__ import annotations

import logging
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
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
    build_storage,
)
from neon_sync.registry import EngineRegistry

from .branding import sanitize_public_text
from .routes import router as v1_router
from .stream_routes import LiveStreamRegistry, stream_router

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_INDEX = _STATIC_DIR / "index.html"
_REQ_ID_SAFE_RE = re.compile(r"^[a-zA-Z0-9._-]{4,64}$")
_INCOMING_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_PREFIX_SCHEME_RE = re.compile(r"^(https?):/+(.+)$", re.IGNORECASE)


def _extract_incoming_http_url(*values: str | None, max_length: int = 4096) -> str | None:
    """Extract the first http(s) URL from query/share fields for same-origin UI handoff."""
    for raw in values:
        if not raw:
            continue
        match = _INCOMING_HTTP_URL_RE.search(raw.strip())
        if not match:
            continue
        candidate = match.group(0).rstrip(").,;!?\"'}]")
        if len(candidate) > max_length:
            continue
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
            return candidate
    return None


def _extract_prefix_target_url(raw_path: str, raw_query: str, *, max_length: int = 4096) -> str | None:
    """Reconstruct a target http(s) URL from `/<url>` prefix paths (preserving query strings)."""
    match = _PREFIX_SCHEME_RE.match(raw_path.lstrip("/"))
    if not match:
        return None
    scheme = match.group(1).lower()
    remainder = match.group(2).lstrip("/")
    if not remainder:
        return None
    candidate = f"{scheme}://{remainder}"
    if raw_query:
        candidate = f"{candidate}?{raw_query}"
    if len(candidate) > max_length:
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return candidate
    return None


class _NeonSyncStdoutFormatter(logging.Formatter):
    """Sanitized server stdout formatter branded as Neon Sync."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        lvl = f"{record.levelname:<5}"
        msg = sanitize_public_text(record.getMessage())
        return f"[{ts}] [NEON-SYNC] [{lvl}] {msg}"


def configure_neon_logging() -> logging.Logger:
    """Attach a clean, branded Neon Sync stdout logger (idempotent)."""
    logger = logging.getLogger("neon_sync.server")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_NeonSyncStdoutFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


@dataclass
class AppContainer:
    settings: Settings
    registry: EngineRegistry
    storage: ArtifactStorage
    job_store: JobStore
    orchestrator: ExtractionOrchestrator
    job_manager: JobManager
    stream_registry: LiveStreamRegistry
    probe_limiter: SlidingWindowRateLimiter
    download_limiter: SlidingWindowRateLimiter


class NeonSyncWhiteLabelMiddleware:
    """Pure-ASGI white-label header wrapper hiding server/engine internals."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

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
        started = time.monotonic()

        async def _send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                elapsed_ms = (time.monotonic() - started) * 1000.0
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower()
                    not in {
                        b"server",
                        b"x-request-id",
                        b"x-neon-sync-version",
                        b"x-powered-by",
                        b"x-response-time-ms",
                    }
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
            await send(message)

        await self.app(scope, receive, _send_wrapper)


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
) -> FastAPI:
    cfg = settings or get_settings()
    reg = registry or build_default_registry(cfg)
    storage = build_storage(cfg)
    job_store: JobStore = SQLiteJobStore(cfg.job_db_path) if cfg.job_db_path else InMemoryJobStore()
    orchestrator = ExtractionOrchestrator(cfg, reg, storage)
    job_manager = JobManager(cfg, orchestrator, store=job_store)
    stream_registry = LiveStreamRegistry()

    container = AppContainer(
        settings=cfg,
        registry=reg,
        storage=storage,
        job_store=job_store,
        orchestrator=orchestrator,
        job_manager=job_manager,
        stream_registry=stream_registry,
        probe_limiter=SlidingWindowRateLimiter(cfg.rate_limit_probes_per_minute, 60.0),
        download_limiter=SlidingWindowRateLimiter(cfg.rate_limit_downloads_per_hour, 3600.0),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger = configure_neon_logging()
        try:
            recovered = container.job_manager.recover_interrupted_jobs()
            await container.registry.refresh_health()
            logger.info("Neon Sync v%s initialized (%s)", __version__, cfg.environment)
            if recovered:
                logger.info("Recovered %s interrupted job records", recovered)
            yield
        finally:
            await container.job_manager.shutdown()
            if isinstance(job_store, SQLiteJobStore):
                job_store.close()
            if isinstance(storage, LocalArtifactStorage):
                storage.close()

    # Disable public OpenAPI/Swagger/ReDoc schemas so internal API definitions stay hidden
    app = FastAPI(
        title="Neon Sync",
        version=__version__,
        description="Neon Sync Media Extraction & Live Streaming Engine.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.container = container
    app.add_middleware(NeonSyncWhiteLabelMiddleware)
    app.include_router(v1_router, prefix="/v1")
    app.include_router(stream_router, prefix="/v1")
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def web_ui() -> HTMLResponse:
        if _STATIC_INDEX.exists():
            return HTMLResponse(_STATIC_INDEX.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Neon Sync</h1>")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def pwa_manifest() -> FileResponse:
        path = _STATIC_DIR / "manifest.webmanifest"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(path, media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    async def pwa_service_worker() -> FileResponse:
        path = _STATIC_DIR / "sw.js"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(
            path,
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    async def apple_touch_icon() -> FileResponse:
        path = _STATIC_DIR / "icon-192.png"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(path, media_type="image/png")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        path = _STATIC_DIR / "icon.svg"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(path, media_type="image/svg+xml")

    @app.get("/share", include_in_schema=False)
    async def share_target(
        url: str | None = None,
        text: str | None = None,
        title: str | None = None,
    ) -> RedirectResponse:
        """PWA Web Share Target receiver: redirects safely to same-origin `/?url=...`."""
        target = _extract_incoming_http_url(url, text, title, max_length=cfg.max_url_length)
        dest = f"/?url={quote(target, safe='')}" if target else "/"
        return RedirectResponse(url=dest, status_code=302)

    @app.get("/https:{target_rest:path}", include_in_schema=False)
    @app.get("/http:{target_rest:path}", include_in_schema=False)
    async def url_prefix_capture(target_rest: str, request: Request) -> RedirectResponse:
        """Instant URL prefix shortcut: `https://host/https://example.com/v` -> `/?url=...`."""
        target = _extract_prefix_target_url(
            request.url.path,
            request.url.query,
            max_length=cfg.max_url_length,
        )
        if not target:
            raise HTTPException(status_code=404, detail="Not Found")
        return RedirectResponse(url=f"/?url={quote(target, safe='')}", status_code=302)

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
        server_header=False,
    )


if __name__ == "__main__":
    run()
