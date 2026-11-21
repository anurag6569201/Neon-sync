"""FastAPI application factory, lifecycle management, and dependency container."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI

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
    ExtractionOrchestrator,
    JobManager,
    LocalArtifactStorage,
    SlidingWindowRateLimiter,
)
from neon_sync.registry import EngineRegistry

from .routes import router as v1_router


@dataclass
class AppContainer:
    settings: Settings
    registry: EngineRegistry
    storage: LocalArtifactStorage
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
    storage = LocalArtifactStorage(cfg)
    orchestrator = ExtractionOrchestrator(cfg, reg, storage)
    job_manager = JobManager(cfg, orchestrator)

    container = AppContainer(
        settings=cfg,
        registry=reg,
        storage=storage,
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
