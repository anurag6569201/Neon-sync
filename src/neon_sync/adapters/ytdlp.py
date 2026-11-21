"""Tier-2 Video, Audio, Playlist & Subtitle Extractor via isolated yt-dlp CLI subprocess.

Security & Licensing:
- Invoked strictly as an isolated CLI subprocess (isolates GPL-2.0 `mutagen` & crashes).
- Uses `--ignore-config --no-exec` and `--` before URLs to prevent flag injection.
- Routes all network traffic through Safe Egress Proxy via `--proxy`.
- Detects outdated extractor errors (nsig / challenge solving) to trigger ops alerts.
"""

from __future__ import annotations

import json
import mimetypes
import re
import time
from datetime import UTC, datetime

from neon_sync.config import Settings
from neon_sync.models import (
    AuthorInfo,
    AuthRequirements,
    ChapterInfo,
    ContentType,
    CostProfile,
    DownloadInfo,
    DownloadPlan,
    DownloadStatus,
    EngineCapability,
    EngineManifest,
    EngineProvenance,
    ErrorCode,
    FormatInfo,
    HealthReport,
    InstallationStatus,
    InvocationMode,
    JobStatus,
    MatchScore,
    MediaItem,
    MediaMetadata,
    NormalizedMediaOutput,
    ProbeOptions,
    ProgressEvent,
    ProtocolType,
    SourceInfo,
    SubtitleTrack,
    SupportedDomains,
    ThumbnailInfo,
    TranscriptInfo,
    build_ytdlp_format_selector,
)
from neon_sync.security import (
    CancellationToken,
    SubprocessCancelledError,
    SubprocessTimeoutError,
    detect_magic_mime,
    find_binary,
    run_safe_subprocess,
    sanitize_filename,
)

from .base import AdapterError, DownloadArtifact, ExtractorAdapter, ProgressCallback, UrlContext

VIDEO_PRIORITY_DOMAINS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "vimeo.com",
        "dailymotion.com",
        "twitch.tv",
        "tiktok.com",
        "soundcloud.com",
        "bandcamp.com",
        "bilibili.com",
        "rumble.com",
        "streamable.com",
        "loom.com",
        "facebook.com",
        "fb.watch",
        "instagram.com",
        "x.com",
        "twitter.com",
        "reddit.com",
        "redd.it",
        "bluesky.app",
        "bsky.app",
        "kick.com",
        "ted.com",
    }
)

_OUTDATED_HINTS = (
    "nsig",
    "signature extraction",
    "challenge solving",
    "unable to decode",
    "player response",
    "please report this issue",
    "yt-dlp -u",
    "no supported javascript runtime",
)

_PROGRESS_PREFIX = "__NEON_PROGRESS__:"


def classify_ytdlp_error(stderr: str, engine_id: str = "yt-dlp") -> AdapterError:
    """Map yt-dlp stderr messages into normalized structured AdapterErrors."""
    low = (stderr or "").lower()
    tail = (stderr or "yt-dlp failed").strip()[-500:]

    if any(k in low for k in ("drm protected", "widevine", "playready", "fairplay", "this video is drm")):
        return AdapterError(ErrorCode.ERR_DRM_PROTECTED, tail, engine_id=engine_id)
    if any(
        k in low
        for k in (
            "sign in to confirm",
            "login required",
            "members-only",
            "private video",
            "use --cookies",
            "authentication required",
            "age-restricted",
        )
    ):
        return AdapterError(ErrorCode.ERR_AUTH_REQUIRED, tail, engine_id=engine_id)
    if any(k in low for k in ("geo-restricted", "not available in your country", "geo blocked")):
        return AdapterError(ErrorCode.ERR_GEO_BLOCKED, tail, engine_id=engine_id)
    if any(k in low for k in ("video unavailable", "404", "not found", "has been removed", "410")):
        return AdapterError(ErrorCode.ERR_NOT_FOUND, tail, engine_id=engine_id)
    if any(k in low for k in ("http error 429", "too many requests", "rate limit")):
        return AdapterError(
            ErrorCode.ERR_RATE_LIMITED, tail, engine_id=engine_id, retryable=True
        )
    if any(k in low for k in ("captcha", "cloudflare", "turnstile")):
        return AdapterError(ErrorCode.ERR_CHALLENGE_REQUIRED, tail, engine_id=engine_id)
    if any(hint in low for hint in _OUTDATED_HINTS):
        return AdapterError(
            ErrorCode.ERR_EXTRACTOR_OUTDATED,
            tail,
            engine_id=engine_id,
            retryable=False,
            outdated=True,
        )
    if any(k in low for k in ("unsupported url", "no video formats found", "no media found")):
        return AdapterError(ErrorCode.ERR_UNSUPPORTED_URL, tail, engine_id=engine_id)
    return AdapterError(ErrorCode.ERR_INTERNAL, tail, engine_id=engine_id, retryable=True)


def _map_protocol(proto: str | None) -> ProtocolType:
    p = (proto or "").lower()
    if "m3u8" in p or "hls" in p:
        return ProtocolType.HLS
    if "dash" in p or "mpd" in p:
        return ProtocolType.DASH
    if "ism" in p:
        return ProtocolType.ISM
    if "rtmp" in p:
        return ProtocolType.RTMP
    if p == "http":
        return ProtocolType.HTTP
    if p == "https":
        return ProtocolType.HTTPS
    return ProtocolType.OTHER


def _parse_upload_date(raw: dict) -> datetime | None:
    ts = raw.get("timestamp") or raw.get("release_timestamp")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=UTC)
    ud = raw.get("upload_date")
    if isinstance(ud, str) and len(ud) == 8 and ud.isdigit():
        try:
            return datetime.strptime(ud, "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def normalize_ytdlp_json(
    raw: dict,
    ctx: UrlContext,
    *,
    version: str,
    probe_ms: int,
) -> NormalizedMediaOutput:
    """Convert raw yt-dlp --dump-single-json payload into NormalizedMediaOutput."""
    is_playlist = raw.get("_type") in {"playlist", "multi_video"}
    is_live = bool(raw.get("is_live") or raw.get("live_status") == "is_live")

    formats: list[FormatInfo] = []
    for f in raw.get("formats") or []:
        if not isinstance(f, dict) or not f.get("format_id"):
            continue
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        has_video = bool(vcodec and vcodec != "none")
        has_audio = bool(acodec and acodec != "none")
        ext = str(f.get("ext") or "mp4")
        width = f.get("width") if isinstance(f.get("width"), int) and f["width"] > 0 else None
        height = f.get("height") if isinstance(f.get("height"), int) and f["height"] > 0 else None
        resolution = (
            f"{width}x{height}"
            if width and height
            else ("audio_only" if has_audio and not has_video else f.get("resolution"))
        )
        size = f.get("filesize") or f.get("filesize_approx")
        size_int = int(size) if isinstance(size, (int, float)) and size >= 0 else None
        mime = mimetypes.guess_type(f"x.{ext}")[0] or (
            f"video/{ext}" if has_video else f"audio/{ext}"
        )
        formats.append(
            FormatInfo(
                format_id=str(f["format_id"]),
                label=f.get("format_note") or f.get("format"),
                container=ext,
                mime_type=mime,
                resolution=resolution,
                width=width,
                height=height,
                fps=float(f["fps"]) if isinstance(f.get("fps"), (int, float)) and f["fps"] >= 0 else None,
                video_codec=None if (not vcodec or vcodec == "none") else str(vcodec),
                audio_codec=None if (not acodec or acodec == "none") else str(acodec),
                video_bitrate_kbps=float(f["vbr"]) if isinstance(f.get("vbr"), (int, float)) and f["vbr"] >= 0 else None,
                audio_bitrate_kbps=float(f["abr"]) if isinstance(f.get("abr"), (int, float)) and f["abr"] >= 0 else None,
                size_bytes=size_int,
                size_is_approx=(f.get("filesize") is None and f.get("filesize_approx") is not None),
                protocol=_map_protocol(f.get("protocol")),
                has_video=has_video,
                has_audio=has_audio,
                requires_muxing=(has_video and not has_audio),
                direct_media_url=f.get("url") if isinstance(f.get("url"), str) else None,
            )
        )

    subtitles: list[SubtitleTrack] = []
    for is_auto, key in ((False, "subtitles"), (True, "automatic_captions")):
        tracks = raw.get(key) or {}
        if isinstance(tracks, dict):
            for lang, variants in tracks.items():
                if not isinstance(variants, list) or not variants:
                    continue
                fmts = sorted({str(v.get("ext")) for v in variants if isinstance(v, dict) and v.get("ext")})
                first_url = next(
                    (v.get("url") for v in variants if isinstance(v, dict) and v.get("url")),
                    None,
                )
                first_name = next(
                    (v.get("name") for v in variants if isinstance(v, dict) and v.get("name")),
                    None,
                )
                subtitles.append(
                    SubtitleTrack(
                        language=str(lang),
                        name=first_name,
                        is_auto_generated=is_auto,
                        formats=fmts,
                        url=first_url,
                    )
                )

    items: list[MediaItem] = []
    if is_playlist:
        for idx, entry in enumerate(raw.get("entries") or [], start=1):
            if not isinstance(entry, dict):
                continue
            entry_url = entry.get("webpage_url") or entry.get("url") or ctx.url
            items.append(
                MediaItem(
                    index=idx,
                    external_id=str(entry["id"]) if entry.get("id") else None,
                    url=str(entry_url),
                    title=entry.get("title"),
                    content_type="video",
                    duration_seconds=entry.get("duration") if isinstance(entry.get("duration"), (int, float)) else None,
                    thumbnail_url=entry.get("thumbnail"),
                )
            )

    chapters: list[ChapterInfo] = []
    for ch in raw.get("chapters") or []:
        if isinstance(ch, dict) and "start_time" in ch and "end_time" in ch:
            chapters.append(
                ChapterInfo(
                    title=str(ch.get("title") or "Chapter"),
                    start_seconds=float(ch["start_time"]),
                    end_seconds=float(ch["end_time"]),
                )
            )

    if is_playlist:
        content_type = ContentType.PLAYLIST
    elif is_live:
        content_type = ContentType.LIVESTREAM
    elif formats and all(not f.has_video and f.has_audio for f in formats):
        content_type = ContentType.AUDIO
    else:
        content_type = ContentType.VIDEO

    thumb = ThumbnailInfo(url=raw["thumbnail"]) if isinstance(raw.get("thumbnail"), str) else None
    author_name = raw.get("uploader") or raw.get("channel") or raw.get("creator")
    author = (
        AuthorInfo(
            name=str(author_name),
            id=str(raw["channel_id"]) if raw.get("channel_id") else None,
            url=raw.get("uploader_url") or raw.get("channel_url"),
        )
        if author_name
        else None
    )

    primary_size = raw.get("filesize") or raw.get("filesize_approx")
    if primary_size is None and formats:
        sized = [f.size_bytes for f in formats if f.size_bytes is not None]
        primary_size = max(sized) if sized else None

    transcript = None
    if subtitles:
        manual = [s for s in subtitles if not s.is_auto_generated]
        chosen = manual[0] if manual else subtitles[0]
        transcript = TranscriptInfo(
            available=True,
            language=chosen.language,
            source="manual_captions" if not chosen.is_auto_generated else "auto_captions",
        )

    extractor_key = raw.get("extractor_key") or raw.get("extractor")
    return NormalizedMediaOutput(
        job_id=ctx.job_id,
        original_url=ctx.validated.original_url,
        url=str(raw.get("webpage_url") or ctx.url),
        source=SourceInfo(
            domain=ctx.domain,
            platform=str(raw.get("extractor") or ctx.domain).lower(),
            extractor_key=str(extractor_key) if extractor_key else None,
            external_id=str(raw["id"]) if raw.get("id") is not None else None,
        ),
        content_type=content_type,
        is_live=is_live,
        title=raw.get("title"),
        description=raw.get("description"),
        thumbnail=thumb,
        duration_seconds=(
            float(raw["duration"])
            if isinstance(raw.get("duration"), (int, float)) and raw["duration"] >= 0
            else None
        ),
        size_bytes=int(primary_size) if isinstance(primary_size, (int, float)) and primary_size >= 0 else None,
        author=author,
        available_formats=formats,
        subtitles=subtitles,
        transcript=transcript,
        items=items,
        metadata=MediaMetadata(
            published_at=_parse_upload_date(raw),
            view_count=raw.get("view_count") if isinstance(raw.get("view_count"), int) else None,
            like_count=raw.get("like_count") if isinstance(raw.get("like_count"), int) else None,
            tags=[str(t) for t in (raw.get("tags") or [])[:50]],
            item_count_total=raw.get("playlist_count") or (len(items) if is_playlist else 1),
            item_count_returned=len(items) if is_playlist else 1,
            chapters=chapters,
        ),
        download=DownloadInfo(status=DownloadStatus.NOT_REQUESTED),
        engine=EngineProvenance(
            engine_used="yt-dlp",
            engine_version=version,
            fallback_chain=["yt-dlp"],
            probe_duration_ms=probe_ms,
        ),
    )


class YtDlpAdapter(ExtractorAdapter):
    """Primary video/audio/playlist/subtitle engine running strictly as a subprocess."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._version = "unknown"

    @property
    def id(self) -> str:
        return "yt-dlp"

    def manifest(self) -> EngineManifest:
        installed = find_binary(self._settings.ytdlp_bin) is not None
        return EngineManifest(
            id=self.id,
            name="yt-dlp",
            version=self._version,
            tier=2,
            supported_content_types=["video", "audio", "playlist", "livestream", "hls", "dash"],
            supported_domains=SupportedDomains(
                mode="wildcard",
                domains=sorted(VIDEO_PRIORITY_DOMAINS),
                site_count=1800,
            ),
            protocols=["http", "https", "hls", "dash", "ism", "rtmp"],
            capabilities=[
                EngineCapability.EXTRACT_METADATA,
                EngineCapability.ENUMERATE_FORMATS,
                EngineCapability.EXTRACT_MEDIA_URL,
                EngineCapability.DOWNLOAD_MEDIA,
                EngineCapability.DOWNLOAD_PLAYLIST,
                EngineCapability.EXTRACT_SUBTITLES,
                EngineCapability.EXTRACT_THUMBNAIL,
                EngineCapability.EXTRACT_AUDIO,
                EngineCapability.SUPPORT_AUTHENTICATED_CONTENT,
            ],
            auth_requirements=AuthRequirements(
                supports_cookies=True,
                supports_oauth=True,
                supports_headers=True,
            ),
            cost_profile=CostProfile(avg_probe_ms=1500, memory_mb=128, requires_browser=False),
            priority=900,
            installation_status=(
                InstallationStatus.INSTALLED if installed else InstallationStatus.MISSING
            ),
            license="Unlicense (bundled mutagen dep: GPL-2.0)",
            invocation_mode=InvocationMode.SUBPROCESS,
        )

    def quick_match(self, ctx: UrlContext) -> MatchScore:
        path_lower = ctx.path.lower()
        ct = (ctx.head_content_type or "").split(";")[0].strip().lower()

        if ctx.mode == "image":
            return MatchScore(engine_id=self.id, confidence=10, reason="Image mode prefers gallery-dl.")

        if path_lower.endswith((".m3u8", ".mpd")) or ct in {
            "application/vnd.apple.mpegurl",
            "application/x-mpegurl",
            "application/dash+xml",
        }:
            return MatchScore(
                engine_id=self.id,
                confidence=95,
                reason="HLS/DASH stream manifest detected.",
                predicted_content_type="video",
            )

        if ctx.domain in VIDEO_PRIORITY_DOMAINS:
            return MatchScore(
                engine_id=self.id,
                confidence=95,
                reason=f"Known high-priority video/audio platform '{ctx.domain}'.",
                predicted_content_type="video",
            )

        if ct in {"text/html", "application/xhtml+xml"} or not ct:
            return MatchScore(
                engine_id=self.id,
                confidence=55,
                reason="Candidate for yt-dlp site/generic extractor.",
                predicted_content_type="video",
            )

        return MatchScore(
            engine_id=self.id,
            confidence=15,
            reason="Non-HTML direct resource prefers direct-http/aria2.",
        )

    def _exe(self) -> str:
        return find_binary(self._settings.ytdlp_bin) or self._settings.ytdlp_bin

    def build_probe_argv(self, url: str, options: ProbeOptions) -> list[str]:
        argv = [
            self._exe(),
            "--ignore-config",
            "--no-exec",
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
            "--flat-playlist",
            "--playlist-end",
            str(min(options.max_items, self._settings.max_playlist_items)),
        ]
        if self._settings.egress_proxy_url:
            argv.extend(["--proxy", self._settings.egress_proxy_url])
        argv.extend(["--", url])
        return argv

    def build_download_argv(self, url: str, plan: DownloadPlan) -> list[str]:
        argv = [
            self._exe(),
            "--ignore-config",
            "--no-exec",
            "--no-warnings",
            "--newline",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--concurrent-fragments",
            "6",
            "--max-filesize",
            str(plan.max_bytes),
            "--playlist-end",
            str(min(plan.max_items, self._settings.max_playlist_items)),
            "--progress-template",
            f"download:{_PROGRESS_PREFIX}%(progress._percent_str)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.speed)s|%(progress.eta)s",
            "-o",
            str(plan.output_dir / "%(id)s.%(ext)s"),
        ]

        selector = build_ytdlp_format_selector(
            mode=plan.mode,
            format_id=plan.format_id,
            video_quality=plan.video_quality,
            video_codec=plan.video_codec,
            video_container=plan.video_container,
            fps_limit=plan.fps_limit,
        )
        argv.extend(["-f", selector])

        if plan.mode == "audio":
            if plan.audio_format and plan.audio_format != "best":
                argv.extend(["-x", "--audio-format", plan.audio_format])
                if plan.audio_bitrate != "auto":
                    argv.extend(["--audio-quality", plan.audio_bitrate.upper()])
        elif plan.video_container != "auto":
            argv.extend(["--merge-output-format", plan.video_container])

        if plan.playlist_items:
            argv.extend(["--playlist-items", plan.playlist_items])

        if plan.extract_subtitles or plan.embed_subtitles or plan.generate_transcript:
            langs = ",".join(plan.subtitle_languages or ["en"])
            argv.append("--write-subs")
            if plan.subtitle_mode in {"prefer_manual", "include_auto"}:
                argv.append("--write-auto-subs")
            argv.extend(["--sub-langs", langs, "--sub-format", f"{plan.subtitle_format}/best"])
            if plan.embed_subtitles and plan.mode != "audio":
                argv.append("--embed-subs")

        if plan.embed_metadata:
            argv.append("--embed-metadata")
        if plan.embed_thumbnail:
            argv.append("--embed-thumbnail")
        elif plan.extract_thumbnail:
            argv.append("--write-thumbnail")

        if self._settings.egress_proxy_url:
            argv.extend(["--proxy", self._settings.egress_proxy_url])
        argv.extend(["--", url])
        return argv

    async def probe(
        self,
        ctx: UrlContext,
        options: ProbeOptions,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> NormalizedMediaOutput:
        t0 = time.monotonic()
        argv = self.build_probe_argv(ctx.url, options)
        try:
            res = await run_safe_subprocess(
                argv,
                timeout=float(options.timeout_seconds),
                proxy_url=self._settings.egress_proxy_url,
                cancel_token=cancel_token,
            )
        except SubprocessTimeoutError as exc:
            raise AdapterError(
                ErrorCode.ERR_TIMEOUT, str(exc), engine_id=self.id, retryable=True
            ) from exc
        except SubprocessCancelledError as exc:
            raise AdapterError(ErrorCode.ERR_INTERNAL, str(exc), engine_id=self.id) from exc

        if res.returncode != 0:
            raise classify_ytdlp_error(res.stderr or res.stdout, engine_id=self.id)

        try:
            raw = json.loads(res.stdout.strip())
        except json.JSONDecodeError as exc:
            raise AdapterError(
                ErrorCode.ERR_INTERNAL,
                f"yt-dlp returned invalid JSON: {exc}",
                engine_id=self.id,
            ) from exc

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return normalize_ytdlp_json(raw, ctx, version=self._version, probe_ms=elapsed_ms)

    async def download(
        self,
        ctx: UrlContext,
        plan: DownloadPlan,
        *,
        progress_cb: ProgressCallback | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadArtifact:
        plan.output_dir.mkdir(parents=True, exist_ok=True)
        argv = self.build_download_argv(ctx.url, plan)

        async def _on_line(line: str) -> None:
            if not progress_cb or not line.startswith(_PROGRESS_PREFIX):
                return
            payload = line.removeprefix(_PROGRESS_PREFIX).split("|")
            if len(payload) < 5:
                return
            pct_match = re.search(r"([0-9]+(?:\.[0-9]+)?)", payload[0])
            pct = float(pct_match.group(1)) if pct_match else None
            dl_bytes = int(payload[1]) if payload[1].isdigit() else None
            tot_bytes = int(payload[2]) if payload[2].isdigit() else None
            try:
                speed = float(payload[3]) if payload[3] not in {"NA", "None", ""} else None
            except ValueError:
                speed = None
            try:
                eta = float(payload[4]) if payload[4] not in {"NA", "None", ""} else None
            except ValueError:
                eta = None
            evt = ProgressEvent(
                job_id=plan.job_id,
                status=JobStatus.DOWNLOADING,
                stage="downloading",
                percent=min(100.0, pct) if pct is not None else None,
                bytes_downloaded=dl_bytes,
                bytes_total=tot_bytes,
                speed_bps=speed,
                eta_seconds=eta,
                message="Downloading media via yt-dlp",
            )
            res = progress_cb(evt)
            if hasattr(res, "__await__"):
                await res

        try:
            res = await run_safe_subprocess(
                argv,
                cwd=plan.output_dir,
                timeout=float(plan.timeout_seconds),
                proxy_url=self._settings.egress_proxy_url,
                cancel_token=cancel_token,
                on_stdout_line=_on_line,
            )
        except SubprocessTimeoutError as exc:
            raise AdapterError(
                ErrorCode.ERR_TIMEOUT, str(exc), engine_id=self.id, retryable=True
            ) from exc
        except SubprocessCancelledError as exc:
            raise AdapterError(ErrorCode.ERR_INTERNAL, str(exc), engine_id=self.id) from exc

        if res.returncode != 0:
            raise classify_ytdlp_error(res.stderr or res.stdout, engine_id=self.id)

        files = sorted(
            (p for p in plan.output_dir.iterdir() if p.is_file() and not p.name.endswith(".part")),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        if not files:
            raise AdapterError(
                ErrorCode.ERR_INTERNAL,
                "yt-dlp completed but produced no output files.",
                engine_id=self.id,
            )

        primary = files[0]
        size = primary.stat().st_size
        magic_mime, is_exec = detect_magic_mime(primary)
        if is_exec:
            primary.unlink(missing_ok=True)
            raise AdapterError(
                ErrorCode.ERR_VALIDATION_FAILED,
                f"Blocked dangerous executable file ({magic_mime}).",
                engine_id=self.id,
            )

        return DownloadArtifact(
            local_path=primary,
            filename=sanitize_filename(primary.name),
            mime_type=magic_mime or mimetypes.guess_type(primary.name)[0] or "application/octet-stream",
            size_bytes=size,
            extra_files=files[1:],
        )

    async def health_check(self) -> HealthReport:
        exe = find_binary(self._settings.ytdlp_bin)
        if not exe:
            return HealthReport(
                engine_id=self.id,
                installation_status=InstallationStatus.MISSING,
                version="missing",
                healthy=False,
                detail="yt-dlp not found on PATH.",
            )
        res = await run_safe_subprocess([exe, "--version"], timeout=5.0)
        version = res.stdout.strip() or "unknown"
        self._version = version
        deno = find_binary(self._settings.deno_bin)
        detail = f"yt-dlp {version}" + (
            " (Deno JS runtime available)" if deno else " (WARNING: Deno not found; YouTube EJS may fail)"
        )
        return HealthReport(
            engine_id=self.id,
            installation_status=InstallationStatus.INSTALLED,
            version=version,
            healthy=(res.returncode == 0),
            detail=detail,
        )
