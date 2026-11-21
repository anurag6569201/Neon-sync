"""FastAPI application factory, lifecycle management, and dependency container."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

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
    SlidingWindowRateLimiter,
    SQLiteJobStore,
    build_storage,
)
from neon_sync.registry import EngineRegistry

from .routes import router as v1_router

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_INDEX = _STATIC_DIR / "index.html"


@dataclass
class AppContainer:
    settings: Settings
    registry: EngineRegistry
    storage: ArtifactStorage
    job_store: JobStore
    orchestrator: ExtractionOrchestrator
    job_manager: JobManager
    probe_limiter: SlidingWindowRateLimiter
    download_limiter: SlidingWindowRateLimiter


def build_default_registry(settings: Settings) -> EngineRegistry:
    """Register all extraction & post-processing adapters (Phase 1 + Phase 2)."""
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

    container = AppContainer(
        settings=cfg,
        registry=reg,
        storage=storage,
        job_store=job_store,
        orchestrator=orchestrator,
        job_manager=job_manager,
        probe_limiter=SlidingWindowRateLimiter(cfg.rate_limit_probes_per_minute, 60.0),
        download_limiter=SlidingWindowRateLimiter(cfg.rate_limit_downloads_per_hour, 3600.0),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await container.registry.refresh_health()
        yield
        await container.job_manager.shutdown()
        if isinstance(job_store, SQLiteJobStore):
            job_store.close()

    app = FastAPI(
        title=cfg.app_name,
        version=__version__,
        description=(
            "Modular Universal Media Extraction Engine powered by a capability-driven "
            "Engine Registry, subprocess-isolated adapters, and Safe Egress Proxy."
        ),
        lifespan=lifespan,
    )
    app.state.container = container
    app.include_router(v1_router, prefix="/v1")
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
    uvicorn.run(
        "neon_sync.api.app:create_app",
        factory=True,
        host=cfg.api_host,
        port=cfg.api_port,
        reload=(cfg.environment == "development"),
    )


if __name__ == "__main__":
    run()
