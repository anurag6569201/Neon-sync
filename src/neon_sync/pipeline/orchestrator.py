"""End-to-end 11-stage Universal Media Extraction Orchestrator."""

from __future__ import annotations

import asyncio
import re
import shutil
import time
import uuid
from pathlib import Path

import httpx

from neon_sync.adapters import (
    AdapterError,
    DownloadArtifact,
    FfmpegAdapter,
    ProgressCallback,
    UrlContext,
)
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
    TranscriptInfo,
    WarningInfo,
)
from neon_sync.registry import EngineRegistry
from neon_sync.security import (
    CancellationToken,
    DiskQuotaError,
    UrlValidationError,
    check_free_disk_floor,
    sanitize_filename,
    validate_and_canonicalize_url,
)

from .router import UrlRouter
from .storage import ArtifactStorage
from .transcripts import (
    convert_json3_to_vtt,
    convert_vtt_to_srt,
    normalize_subtitle_to_transcript,
)

_SRT_TS_RE = re.compile(r"(\d{2}:\d{2}:\d{2}),(\d{3})")


class ExtractionOrchestrator:
    """Coordinates validation, caching, routing, download, FFmpeg post-processing & storage."""

    def __init__(
        self,
        settings: Settings,
        registry: EngineRegistry,
        storage: ArtifactStorage,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._storage = storage
        self._router = UrlRouter(registry)
        self._probe_cache: dict[tuple[str, str | None, int, bool], tuple[float, NormalizedMediaOutput]] = {}
        self._inflight_probes: dict[tuple[str, str | None, int, bool], asyncio.Task[NormalizedMediaOutput]] = {}

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

        pref_engine = getattr(request, "preferred_engine", None)
        cache_key = (
            validated.canonical_url,
            pref_engine,
            request.max_items,
            request.allow_browser_fallback,
        )
        now = time.monotonic()
        if not request.bypass_cache and cache_key in self._probe_cache:
            ts, cached = self._probe_cache[cache_key]
            if (now - ts) < self._settings.probe_cache_ttl_seconds:
                return cached.model_copy(deep=True)
            self._probe_cache.pop(cache_key, None)

        # Coalesce concurrent probes for the exact same canonical URL (e.g. Probe + Watch Live)
        if not request.bypass_cache and cancel_token is None and cache_key in self._inflight_probes:
            shared_result = await asyncio.shield(self._inflight_probes[cache_key])
            return shared_result.model_copy(deep=True)

        job_id = f"probe_{uuid.uuid4().hex[:16]}"
        ctx = UrlContext(validated=validated, job_id=job_id)
        options = ProbeOptions(
            timeout_seconds=self._settings.probe_timeout_seconds,
            max_items=request.max_items,
            include_subtitles=request.include_subtitles,
            preferred_engine=pref_engine,
            allow_browser_fallback=(
                request.allow_browser_fallback and self._settings.enable_browser_fallback
            ),
        )
        task = asyncio.create_task(
            self._router.probe_with_fallback(ctx, options, cancel_token=cancel_token)
        )
        if cancel_token is None:
            self._inflight_probes[cache_key] = task
        try:
            result = await task
        finally:
            if self._inflight_probes.get(cache_key) is task:
                self._inflight_probes.pop(cache_key, None)

        self._probe_cache[cache_key] = (time.monotonic(), result.model_copy(deep=True))
        return result

    async def _emit(self, cb: ProgressCallback | None, event: ProgressEvent) -> None:
        if cb is None:
            return
        res = cb(event)
        if hasattr(res, "__await__"):
            await res

    async def _try_fetch_subtitle_direct(
        self,
        result: NormalizedMediaOutput,
        request: DownloadRequest,
        scratch_job_dir: Path,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadArtifact | None:
        """Fast-path direct subtitle download using signed track URLs from the probe result.

        Avoids re-scraping YouTube/video platforms via a second subprocess (the main cause of
        timedtext HTTP 429 rate-limits when extracting captions-only). Always enforces SSRF
        validation, egress proxy routing, exponential retry backoff on 429/5xx, and local
        JSON3/VTT/SRT conversion. Returns None on any miss/error to seamlessly fall back.
        """
        if not request.subtitles_only or not result.subtitles:
            return None

        req_langs = [lang.strip().lower() for lang in (request.subtitle_languages or ["en"]) if lang.strip()]
        if not req_langs:
            req_langs = ["en"]

        # Filter by subtitle_mode (manual_only vs prefer_manual / include_auto)
        candidates = [
            t
            for t in result.subtitles
            if t.url and (request.subtitle_mode != "manual_only" or not t.is_auto_generated)
        ]
        if not candidates:
            return None

        # Match requested languages in priority order: manual exact -> manual prefix -> auto exact -> auto prefix
        chosen = None
        for want_auto in (False, True):
            if want_auto and request.subtitle_mode == "manual_only":
                continue
            pool = [t for t in candidates if t.is_auto_generated == want_auto]
            for req_lang in req_langs:
                exact = next((t for t in pool if t.language.lower() == req_lang), None)
                if exact is not None:
                    chosen = exact
                    break
                prefix = next(
                    (
                        t
                        for t in pool
                        if t.language.lower().startswith(f"{req_lang}-")
                        or t.language.lower().startswith(f"{req_lang}_")
                    ),
                    None,
                )
                if prefix is not None:
                    chosen = prefix
                    break
            if chosen is not None:
                break

        if chosen is None or not chosen.url:
            return None

        try:
            validated_sub = validate_and_canonicalize_url(
                chosen.url,
                allow_private=self._settings.allow_private_addresses,
                max_length=self._settings.max_url_length,
            )
        except UrlValidationError:
            return None

        client_kwargs: dict = {
            "timeout": httpx.Timeout(15.0),
            "follow_redirects": True,
            "max_redirects": 4,
            "headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "text/vtt,text/plain,application/json,*/*;q=0.8",
            },
        }
        if self._settings.egress_proxy_url:
            client_kwargs["proxy"] = self._settings.egress_proxy_url

        raw_text: str | None = None
        backoffs = (1.0, 2.5)
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                for attempt in range(3):
                    if cancel_token is not None and cancel_token.is_cancelled:
                        return None
                    resp = await client.get(validated_sub.canonical_url)
                    if resp.status_code == 200 and resp.content:
                        raw_text = resp.text
                        break
                    if resp.status_code in {429, 500, 502, 503, 504} and attempt < len(backoffs):
                        retry_after = resp.headers.get("retry-after", "").strip()
                        delay = backoffs[attempt]
                        if retry_after.isdigit():
                            delay = min(5.0, max(0.5, float(retry_after)))
                        await asyncio.sleep(delay)
                        continue
                    return None
        except (httpx.HTTPError, OSError, ValueError):
            return None

        if not raw_text or not raw_text.strip():
            return None
        head = raw_text.lstrip()[:32].lower()
        if head.startswith(("<!doctype", "<html")):
            return None

        # Normalize JSON3 / VTT / SRT into the requested subtitle format locally (zero extra network hits)
        text = raw_text
        if text.lstrip().startswith("{"):
            text = convert_json3_to_vtt(text)

        target_fmt = (request.subtitle_format or "vtt").lower()
        if target_fmt == "srt":
            if text.lstrip().startswith("WEBVTT"):
                text = convert_vtt_to_srt(text)
            target_ext = "srt"
            mime = "application/x-subrip; charset=utf-8"
        elif target_fmt == "ass":
            target_ext = "ass" if "[Script Info]" in text else "vtt"
            mime = "text/plain; charset=utf-8" if target_ext == "ass" else "text/vtt; charset=utf-8"
        else:
            if not text.lstrip().startswith("WEBVTT") and "-->" in text:
                text = "WEBVTT\n\n" + _SRT_TS_RE.sub(r"\1.\2", text)
            target_ext = "vtt"
            mime = "text/vtt; charset=utf-8"

        if not text.strip():
            return None

        stem = sanitize_filename(
            result.source.external_id or result.title or "subtitles",
            fallback="subtitles",
            max_len=80,
        )
        lang_tag = sanitize_filename(chosen.language, fallback="sub", max_len=24)
        out_path = scratch_job_dir / f"{stem}.{lang_tag}.{target_ext}"
        out_path.write_text(text, encoding="utf-8")

        return DownloadArtifact(
            local_path=out_path,
            filename=out_path.name,
            mime_type=mime,
            size_bytes=out_path.stat().st_size,
        )

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
                message="Validating URL and analyzing media source",
            ),
        )

        # Stage 3-6: Route & Probe (reuse fresh cached probe when available to avoid double-probing!)
        pref_engine = getattr(request, "preferred_engine", None)
        cache_key = (
            validated.canonical_url,
            pref_engine,
            request.max_items,
            request.allow_browser_fallback,
        )
        now = time.monotonic()
        cached_entry = self._probe_cache.get(cache_key)
        if cached_entry is not None and (now - cached_entry[0]) < self._settings.probe_cache_ttl_seconds:
            result = cached_entry[1].model_copy(deep=True)
            result.job_id = job_id
        else:
            probe_opts = ProbeOptions(
                timeout_seconds=self._settings.probe_timeout_seconds,
                max_items=request.max_items,
                include_subtitles=request.extract_subtitles,
                preferred_engine=pref_engine,
                allow_browser_fallback=(
                    request.allow_browser_fallback and self._settings.enable_browser_fallback
                ),
            )
            result = await self._router.probe_with_fallback(ctx, probe_opts, cancel_token=cancel_token)
            self._probe_cache[cache_key] = (now, result.model_copy(deep=True))

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

        # Pre-flight size quota check if known.
        # Do NOT reject lightweight partial extractions (subtitles, audio-only, clips, lower quality,
        # or explicitly selected smaller formats) simply because the full-length 4K master exceeds quota.
        is_partial_extraction = (
            request.subtitles_only
            or request.mode == "audio"
            or request.clip_start_seconds is not None
            or request.video_quality != "best"
        )
        preflight_size = result.size_bytes
        if request.format_id and result.available_formats:
            selected_fmt = next(
                (f for f in result.available_formats if f.format_id == request.format_id),
                None,
            )
            preflight_size = selected_fmt.size_bytes if selected_fmt is not None else None

        if (
            not is_partial_extraction
            and preflight_size is not None
            and preflight_size > self._settings.max_job_bytes
        ):
            raise AdapterError(
                ErrorCode.ERR_QUOTA_EXCEEDED,
                f"Content size ({preflight_size} bytes) exceeds max job quota ({self._settings.max_job_bytes} bytes).",
                engine_id="orchestrator",
            )

        downloader = self._router.select_downloader(
            ctx,
            probe_result=result,
            preferred_engine=pref_engine,
        )
        if downloader is None:
            raise AdapterError(
                ErrorCode.ERR_UNSUPPORTED_URL,
                "Neon Sync could not find a compatible media stream for this URL.",
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
                    message="Downloading media stream via Neon Sync",
                ),
            )

            plan = DownloadPlan(
                job_id=job_id,
                output_dir=scratch_job_dir,
                format_id=request.format_id,
                mode=request.mode,
                video_quality=request.video_quality,
                video_container=request.video_container,
                video_codec=request.video_codec,
                fps_limit=request.fps_limit,
                video_only=request.video_only,
                remux_faststart=request.remux_faststart,
                audio_format=request.audio_format,
                audio_bitrate=request.audio_bitrate,
                normalize_audio=request.normalize_audio,
                image_format=request.image_format,
                image_quality=request.image_quality,
                image_max_width=request.image_max_width,
                gallery_archive_format=request.gallery_archive_format,
                gallery_range_start=request.gallery_range_start,
                gallery_range_end=request.gallery_range_end,
                extract_thumbnail=request.extract_thumbnail,
                embed_thumbnail=request.embed_thumbnail,
                embed_metadata=request.embed_metadata,
                extract_subtitles=request.extract_subtitles,
                subtitles_only=request.subtitles_only,
                embed_subtitles=request.embed_subtitles,
                subtitle_languages=request.subtitle_languages,
                subtitle_mode=request.subtitle_mode,
                subtitle_format=request.subtitle_format,
                generate_transcript=request.generate_transcript,
                clip_start_seconds=request.clip_start_seconds,
                clip_end_seconds=request.clip_end_seconds,
                playlist_items=request.playlist_items,
                livestream_max_duration=request.livestream_max_duration,
                custom_filename_prefix=request.custom_filename_prefix,
                max_bytes=self._settings.max_job_bytes,
                max_items=request.max_items,
                timeout_seconds=self._settings.download_timeout_seconds,
            )

            artifact: DownloadArtifact | None = None
            if request.subtitles_only:
                artifact = await self._try_fetch_subtitle_direct(
                    result,
                    request,
                    scratch_job_dir,
                    cancel_token=cancel_token,
                )

            if artifact is None:
                artifact = await downloader.download(
                    ctx,
                    plan,
                    progress_cb=progress_cb,
                    cancel_token=cancel_token,
                )

            # yt-dlp applies --download-sections natively; avoid double-clipping with FFmpeg
            already_clipped = (
                downloader.id == "yt-dlp"
                and request.clip_start_seconds is not None
                and not request.subtitles_only
            )

            # Stage 9: Zero-egress FFmpeg validation & advanced post-processing
            final_path = artifact.local_path
            final_filename = artifact.filename
            final_mime = artifact.mime_type or "application/octet-stream"
            ffmpeg = self._registry.get_postprocessor("ffmpeg")
            is_archive_output = final_path.suffix.lower() in {".zip", ".tar"}

            if (
                not request.subtitles_only
                and not is_archive_output
                and isinstance(ffmpeg, FfmpegAdapter)
                and ffmpeg.manifest().installation_status == "installed"
            ):
                await self._emit(
                    progress_cb,
                    ProgressEvent(
                        job_id=job_id,
                        status=JobStatus.POST_PROCESSING,
                        stage="post_processing",
                        percent=85.0,
                        message="Optimizing and verifying media with Neon Sync",
                    ),
                )
                is_image_output = final_mime.startswith("image/") or result.content_type == ContentType.IMAGE
                if is_image_output and (
                    request.image_format != "original" or request.image_max_width is not None
                ):
                    target_fmt = (
                        request.image_format
                        if request.image_format != "original"
                        else (final_path.suffix.lstrip(".").lower() or "jpg")
                    )
                    if target_fmt == "jpeg":
                        target_fmt = "jpg"
                    img_out = scratch_job_dir / f"{Path(final_filename).stem}.{target_fmt}"
                    if img_out.resolve() == final_path.resolve():
                        img_out = scratch_job_dir / f"{Path(final_filename).stem}_processed.{target_fmt}"
                    final_path = await ffmpeg.process_image(
                        final_path,
                        img_out,
                        image_format=target_fmt,
                        quality=request.image_quality,
                        max_width=request.image_max_width,
                        cancel_token=cancel_token,
                    )
                    final_filename = f"{Path(final_filename).stem}.{target_fmt}"
                    final_mime = "image/jpeg" if target_fmt == "jpg" else f"image/{target_fmt}"
                    result.engine.post_processor = f"ffmpeg-{ffmpeg.manifest().version}"
                else:
                    # Run clipping and audio normalization/conversion sequentially (not mutually exclusive)
                    if (
                        request.clip_start_seconds is not None
                        and not already_clipped
                        and result.content_type in {ContentType.VIDEO, ContentType.AUDIO}
                    ):
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

                    if request.mode == "audio" and (
                        request.normalize_audio
                        or (
                            request.audio_format
                            and request.audio_format != "best"
                            and final_path.suffix.lstrip(".").lower() != request.audio_format
                        )
                    ):
                        target_af = (
                            request.audio_format
                            if (request.audio_format and request.audio_format != "best")
                            else (final_path.suffix.lstrip(".").lower() or "mp3")
                        )
                        audio_path = scratch_job_dir / f"{Path(final_filename).stem}_norm.{target_af}"
                        final_path = await ffmpeg.extract_audio(
                            final_path,
                            audio_path,
                            audio_format=target_af,
                            audio_bitrate=request.audio_bitrate,
                            normalize_audio=request.normalize_audio,
                            max_output_bytes=self._settings.max_job_bytes,
                            cancel_token=cancel_token,
                        )
                        final_filename = f"{Path(final_filename).stem}.{target_af}"
                        final_mime = f"audio/{target_af}"
                        result.engine.post_processor = f"ffmpeg-{ffmpeg.manifest().version}"
                    elif (
                        request.remux_faststart
                        and result.content_type == ContentType.VIDEO
                        and final_path.suffix.lower() in {".mp4", ".mov"}
                    ):
                        fast_path = scratch_job_dir / f"{Path(final_filename).stem}_fast{final_path.suffix}"
                        try:
                            final_path = await ffmpeg.remux_faststart(
                                final_path,
                                fast_path,
                                max_output_bytes=self._settings.max_job_bytes,
                                cancel_token=cancel_token,
                            )
                            result.engine.post_processor = f"ffmpeg-{ffmpeg.manifest().version}"
                        except AdapterError as exc:
                            result.warnings.append(
                                WarningInfo(
                                    code="WARN_FASTSTART_SKIPPED",
                                    message=exc.message,
                                    engine="ffmpeg",
                                )
                            )

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

            # Optional clean transcript generation from downloaded subtitle files
            sub_candidates = [*artifact.extra_files, final_path] if request.subtitles_only else list(artifact.extra_files)
            if request.generate_transcript and sub_candidates:
                sub_file = next(
                    (p for p in sub_candidates if p.suffix.lower() in {".vtt", ".srt"}),
                    None,
                )
                if sub_file is not None:
                    raw_sub = sub_file.read_text(encoding="utf-8", errors="replace")
                    clean_txt = normalize_subtitle_to_transcript(raw_sub)
                    if clean_txt:
                        txt_path = scratch_job_dir / f"{Path(final_filename).stem}_transcript.txt"
                        txt_path.write_text(clean_txt, encoding="utf-8")
                        stored_txt = self._storage.store(
                            job_id=job_id,
                            source_path=txt_path,
                            filename=txt_path.name,
                            mime_type="text/plain; charset=utf-8",
                        )
                        result.transcript = TranscriptInfo(
                            available=True,
                            language=(request.subtitle_languages or ["en"])[0],
                            source="manual_captions" if request.subtitle_mode == "manual_only" else "auto_captions",
                            artifact_url=stored_txt.download_url,
                        )

            # Use human-readable media title for the delivered filename when available
            ext = Path(final_filename).suffix or final_path.suffix or ".bin"
            if (
                result.title
                and result.engine.engine_used in {"yt-dlp", "streamlink", "n-m3u8dl-re", "playwright"}
                and ext.lower() != ".zip"
                and ext.lower() != ".tar"
            ):
                human_stem = sanitize_filename(result.title, fallback=Path(final_filename).stem, max_len=90)
                final_filename = f"{human_stem}{ext}"

            # Optional sanitized custom filename prefix
            if request.custom_filename_prefix.strip():
                prefix = sanitize_filename(request.custom_filename_prefix.strip(), fallback="")
                if prefix:
                    final_filename = f"{prefix}_{final_filename}"

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
