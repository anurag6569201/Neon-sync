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
from neon_sync.auth import AuthDatabase, AuthService
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
from .branding import sanitize_public_text
from .routes import router as v1_router
from .stream_routes import LiveStreamRegistry, stream_router

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_INDEX = _STATIC_DIR / "index.html"
_REQ_ID_SAFE_RE = re.compile(r"^[a-zA-Z0-9._-]{4,64}$")


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
    auth_db: AuthDatabase
    auth: AuthService
    orchestrator: ExtractionOrchestrator
    job_manager: JobManager
    tier_manager: TierManager
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
    auth_service: AuthService | None = None,
) -> FastAPI:
    cfg = settings or get_settings()
    reg = registry or build_default_registry(cfg)
    storage = build_storage(cfg)
    job_store: JobStore = SQLiteJobStore(cfg.job_db_path) if cfg.job_db_path else InMemoryJobStore()
    auth_db = auth_service.db if auth_service is not None else AuthDatabase(cfg.auth_db_path)
    auth = auth_service or AuthService(cfg, auth_db)
    orchestrator = ExtractionOrchestrator(cfg, reg, storage)
    job_manager = JobManager(cfg, orchestrator, store=job_store)
    tier_manager = TierManager(cfg)
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
        stream_registry=stream_registry,
        probe_limiter=SlidingWindowRateLimiter(cfg.rate_limit_probes_per_minute, 60.0),
        download_limiter=SlidingWindowRateLimiter(cfg.rate_limit_downloads_per_hour, 3600.0),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger = configure_neon_logging()
        await container.registry.refresh_health()
        logger.info("Neon Sync v%s initialized (%s)", __version__, cfg.environment)
        yield
        await container.job_manager.shutdown()
        if isinstance(job_store, SQLiteJobStore):
            job_store.close()
        if isinstance(storage, LocalArtifactStorage):
            storage.close()
        auth_db.close()

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
    app.include_router(auth_router, prefix="/v1")
    app.include_router(stream_router, prefix="/v1")
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def web_ui() -> HTMLResponse:
        if _STATIC_INDEX.exists():
            return HTMLResponse(_STATIC_INDEX.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Neon Sync</h1>")

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
