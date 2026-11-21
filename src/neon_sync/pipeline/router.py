"""Capability-driven URL Router and Fallback Escalation Planner."""

from __future__ import annotations

import logging

from neon_sync.adapters import (
    AdapterError,
    DirectHttpAdapter,
    ExtractorAdapter,
    UrlContext,
)
from neon_sync.adapters.gallery_dl import GALLERY_PRIMARY_DOMAINS
from neon_sync.adapters.ytdlp import VIDEO_PRIORITY_DOMAINS
from neon_sync.models import (
    EngineCapability,
    ErrorCode,
    NormalizedMediaOutput,
    ProbeOptions,
    WarningInfo,
)
from neon_sync.models.media import HARD_STOP_ERROR_CODES
from neon_sync.registry import CandidateEngine, EngineRegistry
from neon_sync.security import CancellationToken

log = logging.getLogger("neon_sync.router")

_KNOWN_FAST_DOMAINS = VIDEO_PRIORITY_DOMAINS | GALLERY_PRIMARY_DOMAINS


class UrlRouter:
    """Selects optimal candidate engines and manages disciplined fallback escalation."""

    def __init__(self, registry: EngineRegistry) -> None:
        self._registry = registry

    async def enrich_context_if_needed(self, ctx: UrlContext) -> None:
        """Issue one lightweight HEAD request for unknown domains to guide engine routing."""
        if ctx.head_content_type is not None:
            return
        if ctx.domain in _KNOWN_FAST_DOMAINS:
            return

        direct = self._registry.get_extractor("direct-http")
        if isinstance(direct, DirectHttpAdapter):
            await direct.head_inspect(ctx, timeout=8.0)

    def rank_for_probe(
        self,
        ctx: UrlContext,
        *,
        preferred_engine: str | None = None,
        allow_browser_fallback: bool = False,
    ) -> list[CandidateEngine]:
        return self._registry.select_candidates(
            ctx,
            required_capabilities={EngineCapability.EXTRACT_METADATA},
            preferred_engine=preferred_engine,
            allow_browser_fallback=allow_browser_fallback,
        )

    def select_downloader(
        self,
        ctx: UrlContext,
        *,
        probe_result: NormalizedMediaOutput,
        preferred_engine: str | None = None,
    ) -> ExtractorAdapter | None:
        """Select the best download engine (e.g., upgrade large direct files to aria2)."""
        if preferred_engine:
            explicit = self._registry.get_extractor(preferred_engine)
            if explicit is not None:
                return explicit

        used_id = probe_result.engine.engine_used
        # Upgrade large direct files from direct-http to aria2 when aria2 is installed & healthy
        if used_id == "direct-http" and probe_result.size_bytes is not None:
            ctx.head_content_length = probe_result.size_bytes
            candidates = self._registry.select_candidates(
                ctx,
                required_capabilities={EngineCapability.DOWNLOAD_DIRECT_FILE},
            )
            if candidates:
                return candidates[0].adapter

        probe_engine = self._registry.get_extractor(used_id)
        if probe_engine is not None:
            caps = set(probe_engine.manifest().capabilities)
            if caps & {
                EngineCapability.DOWNLOAD_MEDIA,
                EngineCapability.DOWNLOAD_DIRECT_FILE,
                EngineCapability.DOWNLOAD_GALLERY,
            }:
                return probe_engine

        candidates = self._registry.select_candidates(
            ctx,
            required_capabilities={EngineCapability.DOWNLOAD_MEDIA},
        )
        return candidates[0].adapter if candidates else None

    async def probe_with_fallback(
        self,
        ctx: UrlContext,
        options: ProbeOptions,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> NormalizedMediaOutput:
        """Probe candidate engines in ranked order with hard-stops on fatal/policy errors."""
        await self.enrich_context_if_needed(ctx)
        candidates = self.rank_for_probe(
            ctx,
            preferred_engine=options.preferred_engine,
            allow_browser_fallback=options.allow_browser_fallback,
        )
        if not candidates:
            raise AdapterError(
                ErrorCode.ERR_UNSUPPORTED_URL,
                "No installed, healthy engine can handle this URL.",
                engine_id="router",
            )

        attempted: list[str] = []
        warnings: list[WarningInfo] = []
        last_error: AdapterError | None = None

        for candidate in candidates:
            adapter = candidate.adapter
            # Never degrade a known video/gallery platform to direct-http HTML download when mode is media
            if (
                adapter.id == "direct-http"
                and ctx.domain in _KNOWN_FAST_DOMAINS
                and ctx.mode in {"video", "audio", "image"}
            ):
                continue

            attempted.append(adapter.id)
            max_attempts = 2 if candidate.match.confidence >= 90 else 1
            for attempt in range(max_attempts):
                try:
                    result = await adapter.probe(ctx, options, cancel_token=cancel_token)
                    self._registry.record_outcome(adapter.id, ctx.domain, success=True)
                    result.engine.fallback_chain = list(attempted)
                    if warnings:
                        result.warnings.extend(warnings)
                    return result
                except AdapterError as exc:
                    last_error = exc
                    if exc.retryable and attempt + 1 < max_attempts:
                        log.info(
                            "Retrying %s on %s after transient error (%s)",
                            adapter.id,
                            ctx.domain,
                            exc.code.value,
                        )
                        continue
                    # Do not penalize circuit breaker when URL is simply unsupported by this engine
                    if exc.code != ErrorCode.ERR_UNSUPPORTED_URL:
                        self._registry.record_outcome(
                            adapter.id,
                            ctx.domain,
                            success=False,
                            outdated=exc.outdated,
                        )

                    # Hard stop immediately on DRM, auth, SSRF, 404, geo-block, or quota errors
                    if exc.code in HARD_STOP_ERROR_CODES:
                        log.info("Hard-stopping fallback chain on %s from %s", exc.code, adapter.id)
                        raise

                    warnings.append(
                        WarningInfo(
                            code="WARN_ENGINE_FALLBACK",
                            message=f"Engine '{adapter.id}' skipped ({exc.code.value}): {exc.message[:160]}",
                            engine=adapter.id,
                        )
                    )
                    break

        if last_error is not None:
            raise last_error
        raise AdapterError(
            ErrorCode.ERR_UNSUPPORTED_URL,
            "All candidate engines failed to extract metadata.",
            engine_id="router",
        )
