"""End-to-end 11-stage Universal Media Extraction Orchestrator."""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

from neon_sync.adapters import AdapterError, FfmpegAdapter, ProgressCallback, UrlContext
from neon_sync.config import Settings
from neon_sync.models import (
    ContentType,
    DownloadInfo,
    DownloadPlan,
    DownloadRequest,
    DownloadStatus,
    ErrorCode,
    ErrorInfo,
    JobStatus,
    NormalizedMediaOutput,
    ProbeOptions,
    ProbeRequest,
    ProgressEvent,
    WarningInfo,
)
from neon_sync.registry import EngineRegistry
from neon_sync.security import (
    CancellationToken,
    DiskQuotaError,
    UrlValidationError,
    check_free_disk_floor,
    validate_and_canonicalize_url,
)

from .router import UrlRouter
from .storage import LocalArtifactStorage


class ExtractionOrchestrator:
    """Coordinates validation, caching, routing, download, FFmpeg post-processing & storage."""

    def __init__(
        self,
        settings: Settings,
        registry: EngineRegistry,
        storage: LocalArtifactStorage,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._storage = storage
        self._router = UrlRouter(registry)
        self._probe_cache: dict[tuple[str, str | None, int, bool], tuple[float, NormalizedMediaOutput]] = {}

    async def probe(
        self,
        request: ProbeRequest,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> NormalizedMediaOutput:
        validated = validate_and_canonicalize_url(
            request.url,
            allow_private=self._settings.allow_private_addresses,
            max_length=self._settings.max_url_length,
        )

        cache_key = (
            validated.canonical_url,
            request.preferred_engine,
            request.max_items,
            request.allow_browser_fallback,
        )
        now = time.monotonic()
        if not request.bypass_cache and cache_key in self._probe_cache:
            ts, cached = self._probe_cache[cache_key]
            if (now - ts) < self._settings.probe_cache_ttl_seconds:
                return cached.model_copy(deep=True)
            self._probe_cache.pop(cache_key, None)

        job_id = f"probe_{uuid.uuid4().hex[:16]}"
        ctx = UrlContext(validated=validated, job_id=job_id)
        options = ProbeOptions(
            timeout_seconds=self._settings.probe_timeout_seconds,
            max_items=request.max_items,
            include_subtitles=request.include_subtitles,
            preferred_engine=request.preferred_engine,
            allow_browser_fallback=(
                request.allow_browser_fallback and self._settings.enable_browser_fallback
            ),
        )
        result = await self._router.probe_with_fallback(ctx, options, cancel_token=cancel_token)
        self._probe_cache[cache_key] = (now, result.model_copy(deep=True))
        return result

    async def _emit(self, cb: ProgressCallback | None, event: ProgressEvent) -> None:
        if cb is None:
            return
        res = cb(event)
        if hasattr(res, "__await__"):
            await res

    async def execute_download(
        self,
        job_id: str,
        request: DownloadRequest,
        *,
        progress_cb: ProgressCallback | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> NormalizedMediaOutput:
        # Stage 1-2: Validate & canonicalize URL
        validated = validate_and_canonicalize_url(
            request.url,
            allow_private=self._settings.allow_private_addresses,
            max_length=self._settings.max_url_length,
        )

        # Pre-flight disk floor check
        try:
            check_free_disk_floor(self._settings.scratch_dir, self._settings.min_free_disk_bytes)
        except DiskQuotaError as exc:
            raise AdapterError(ErrorCode.ERR_QUOTA_EXCEEDED, str(exc), engine_id="orchestrator") from exc

        ctx = UrlContext(validated=validated, job_id=job_id, mode=request.mode)
        await self._emit(
            progress_cb,
            ProgressEvent(
                job_id=job_id,
                status=JobStatus.PROBING,
                stage="probing",
                percent=5.0,
                message="Validating URL and probing candidate engines",
            ),
        )

        # Stage 3-6: Route & Probe
        probe_opts = ProbeOptions(
            timeout_seconds=self._settings.probe_timeout_seconds,
            max_items=request.max_items,
            include_subtitles=request.extract_subtitles,
            preferred_engine=request.preferred_engine,
            allow_browser_fallback=(
                request.allow_browser_fallback and self._settings.enable_browser_fallback
            ),
        )
        result = await self._router.probe_with_fallback(ctx, probe_opts, cancel_token=cancel_token)

        # If Tier-3 browser fallback discovered a direct media/manifest URL, hand off to Tier-1/2
        if result.engine.used_browser_fallback and result.available_formats:
            chosen_fmt = next(
                (f for f in result.available_formats if f.format_id == request.format_id and f.direct_media_url),
                next((f for f in result.available_formats if f.direct_media_url), None),
            )
            if chosen_fmt and chosen_fmt.direct_media_url:
                handoff_validated = validate_and_canonicalize_url(
                    chosen_fmt.direct_media_url,
                    allow_private=self._settings.allow_private_addresses,
                    max_length=self._settings.max_url_length,
                )
                ctx = UrlContext(
                    validated=handoff_validated,
                    job_id=job_id,
                    mode=request.mode,
                    head_content_type=chosen_fmt.mime_type,
                    head_content_length=chosen_fmt.size_bytes,
                )

        # Pre-flight size quota check if known
        if result.size_bytes is not None and result.size_bytes > self._settings.max_job_bytes:
            raise AdapterError(
                ErrorCode.ERR_QUOTA_EXCEEDED,
                f"Content size ({result.size_bytes} bytes) exceeds max job quota ({self._settings.max_job_bytes} bytes).",
                engine_id="orchestrator",
            )

        downloader = self._router.select_downloader(
            ctx,
            probe_result=result,
            preferred_engine=request.preferred_engine,
        )
        if downloader is None:
            raise AdapterError(
                ErrorCode.ERR_UNSUPPORTED_URL,
                "No available download engine found for this content.",
                engine_id="orchestrator",
            )

        # Stage 8: Isolated per-job scratch directory download
        scratch_job_dir = (self._settings.scratch_dir / job_id).resolve()
        scratch_job_dir.mkdir(parents=True, exist_ok=True)
        dl_start = time.monotonic()

        try:
            await self._emit(
                progress_cb,
                ProgressEvent(
                    job_id=job_id,
                    status=JobStatus.DOWNLOADING,
                    stage="downloading",
                    percent=10.0,
                    message=f"Downloading with {downloader.id}",
                ),
            )

            plan = DownloadPlan(
                job_id=job_id,
                output_dir=scratch_job_dir,
                format_id=request.format_id,
                mode=request.mode,
                audio_format=request.audio_format,
                extract_thumbnail=request.extract_thumbnail,
                extract_subtitles=request.extract_subtitles,
                clip_start_seconds=request.clip_start_seconds,
                clip_end_seconds=request.clip_end_seconds,
                max_bytes=self._settings.max_job_bytes,
                max_items=request.max_items,
                timeout_seconds=self._settings.download_timeout_seconds,
            )

            artifact = await downloader.download(
                ctx,
                plan,
                progress_cb=progress_cb,
                cancel_token=cancel_token,
            )

            # Stage 9: Optional zero-egress FFmpeg validation & post-processing
            final_path = artifact.local_path
            final_filename = artifact.filename
            final_mime = artifact.mime_type or "application/octet-stream"
            ffmpeg = self._registry.get_postprocessor("ffmpeg")

            if isinstance(ffmpeg, FfmpegAdapter) and ffmpeg.manifest().installation_status == "installed":
                await self._emit(
                    progress_cb,
                    ProgressEvent(
                        job_id=job_id,
                        status=JobStatus.POST_PROCESSING,
                        stage="post_processing",
                        percent=85.0,
                        message="Validating and post-processing media with FFmpeg",
                    ),
                )
                if request.clip_start_seconds is not None and result.content_type in {
                    ContentType.VIDEO,
                    ContentType.AUDIO,
                }:
                    clipped_path = scratch_job_dir / f"clipped{final_path.suffix}"
                    final_path = await ffmpeg.clip_media(
                        final_path,
                        clipped_path,
                        start_seconds=request.clip_start_seconds,
                        end_seconds=request.clip_end_seconds,
                        max_output_bytes=self._settings.max_job_bytes,
                        cancel_token=cancel_token,
                    )
                    result.engine.post_processor = f"ffmpeg-{ffmpeg.manifest().version}"
                elif (
                    request.mode == "audio"
                    and request.audio_format
                    and request.audio_format != "best"
                    and final_path.suffix.lstrip(".").lower() != request.audio_format
                ):
                    audio_path = scratch_job_dir / f"{Path(final_filename).stem}.{request.audio_format}"
                    final_path = await ffmpeg.extract_audio(
                        final_path,
                        audio_path,
                        audio_format=request.audio_format,
                        max_output_bytes=self._settings.max_job_bytes,
                        cancel_token=cancel_token,
                    )
                    final_filename = audio_path.name
                    final_mime = f"audio/{request.audio_format}"
                    result.engine.post_processor = f"ffmpeg-{ffmpeg.manifest().version}"

                if result.content_type in {ContentType.VIDEO, ContentType.AUDIO}:
                    try:
                        probe_info = await ffmpeg.probe_file(final_path, cancel_token=cancel_token)
                        if probe_info.get("duration_seconds") and result.duration_seconds is None:
                            result.duration_seconds = probe_info["duration_seconds"]
                        result.metadata.extra["ffprobe"] = probe_info
                    except AdapterError as exc:
                        result.warnings.append(
                            WarningInfo(
                                code="WARN_FFPROBE_SKIPPED",
                                message=exc.message,
                                engine="ffmpeg",
                            )
                        )

            # Stage 10-11: Content-addressed storage & normalized output
            await self._emit(
                progress_cb,
                ProgressEvent(
                    job_id=job_id,
                    status=JobStatus.UPLOADING,
                    stage="storing",
                    percent=95.0,
                    message="Finalizing artifact storage",
                ),
            )
            stored = self._storage.store(
                job_id=job_id,
                source_path=final_path,
                filename=final_filename,
                mime_type=final_mime,
            )

            dl_ms = int((time.monotonic() - dl_start) * 1000)
            result.engine.download_duration_ms = dl_ms
            if downloader.id != result.engine.engine_used:
                result.engine.fallback_chain.append(downloader.id)
            result.warnings.extend(artifact.warnings)
            result.download = DownloadInfo(
                status=DownloadStatus.COMPLETED,
                selected_format_id=request.format_id or (
                    result.available_formats[0].format_id if result.available_formats else None
                ),
                artifact_url=stored.download_url,
                filename=stored.filename,
                mime_type=stored.mime_type,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                expires_at=stored.expires_at,
            )
            return result
        finally:
            # Always clean up per-job scratch directory immediately
            shutil.rmtree(scratch_job_dir, ignore_errors=True)

    @staticmethod
    def build_error_output(
        *,
        job_id: str,
        raw_url: str,
        exc: Exception,
    ) -> NormalizedMediaOutput:
        if isinstance(exc, UrlValidationError):
            err = ErrorInfo(code=exc.code, message=exc.message, retryable=False, engine="validator")
        elif isinstance(exc, AdapterError):
            err = ErrorInfo(
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                engine=exc.engine_id,
            )
        else:
            err = ErrorInfo(code=ErrorCode.ERR_INTERNAL, message=str(exc), retryable=False)

        return NormalizedMediaOutput(
            job_id=job_id,
            original_url=raw_url,
            url=raw_url,
            source={"domain": "unknown"},
            content_type=ContentType.UNKNOWN,
            download=DownloadInfo(status=DownloadStatus.FAILED),
            engine={"engine_used": err.engine or "orchestrator", "engine_version": "0.1.0"},
            errors=[err],
        )
