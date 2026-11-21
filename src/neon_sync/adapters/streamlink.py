"""Tier-2 Live Stream Extractor & Bounded Recorder via Streamlink CLI subprocess.

License: BSD-2-Clause (permissive; still executed via isolated subprocess for
memory/timeout containment, safe egress proxying, and process-group cancellation).
"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path

from neon_sync.config import Settings
from neon_sync.models import (
    AuthorInfo,
    AuthRequirements,
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
    MatchScore,
    MediaMetadata,
    NormalizedMediaOutput,
    ProbeOptions,
    ProtocolType,
    SourceInfo,
    SupportedDomains,
    WarningInfo,
)
from neon_sync.security import (
    CancellationToken,
    SubprocessCancelledError,
    SubprocessTimeoutError,
    ensure_safe_path,
    run_safe_subprocess,
    sanitize_filename,
)

from .base import AdapterError, DownloadArtifact, ExtractorAdapter, ProgressCallback, UrlContext

LIVESTREAM_DOMAINS: frozenset[str] = frozenset(
    {
        "twitch.tv",
        "kick.com",
        "live.bilibili.com",
        "chzzk.naver.com",
        "sooplive.co.kr",
        "afreecatv.com",
        "pluto.tv",
        "dailymotion.com",
        "vimeo.com",
        "picarto.tv",
        "trovo.live",
        "rumble.com",
    }
)

_RES_RE = re.compile(r"(?P<height>\d{3,4})p(?P<fps>\d{2,3})?")


def _format_duration_hms(seconds: int) -> str:
    hrs, rem = divmod(max(1, seconds), 3600)
    mins, secs = divmod(rem, 60)
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


def normalize_streamlink_json(
    raw: dict,
    ctx: UrlContext,
    *,
    version: str,
    probe_ms: int,
) -> NormalizedMediaOutput:
    """Normalize `streamlink --json <url>` payload into NormalizedMediaOutput."""
    if raw.get("error"):
        err_msg = str(raw["error"])
        low = err_msg.lower()
        if "no plugin can handle" in low or "no playable streams" in low:
            raise AdapterError(ErrorCode.ERR_UNSUPPORTED_URL, err_msg, engine_id="streamlink")
        if "geo" in low or "region" in low:
            raise AdapterError(ErrorCode.ERR_GEO_BLOCKED, err_msg, engine_id="streamlink")
        if "404" in low or "offline" in low or "not found" in low:
            raise AdapterError(ErrorCode.ERR_NOT_FOUND, err_msg, engine_id="streamlink")
        raise AdapterError(ErrorCode.ERR_INTERNAL, err_msg, engine_id="streamlink", retryable=True)

    streams_map = raw.get("streams") or {}
    if not isinstance(streams_map, dict) or not streams_map:
        raise AdapterError(
            ErrorCode.ERR_UNSUPPORTED_URL,
            "Streamlink found no playable streams at this URL.",
            engine_id="streamlink",
        )

    meta = raw.get("metadata") or {}
    plugin = str(raw.get("plugin") or ctx.domain)
    formats: list[FormatInfo] = []

    for name, info in streams_map.items():
        if name in {"best", "worst"} or not isinstance(info, dict):
            continue
        stype = str(info.get("type") or "hls").lower()
        proto = (
            ProtocolType.HLS
            if "hls" in stype
            else ProtocolType.DASH
            if "dash" in stype
            else ProtocolType.HTTPS
        )
        m = _RES_RE.search(name)
        height = int(m.group("height")) if m else None
        fps = float(m.group("fps")) if (m and m.group("fps")) else None
        is_audio = "audio" in name.lower()
        formats.append(
            FormatInfo(
                format_id=name,
                label=f"{name} ({stype.upper()})",
                container="ts" if proto == ProtocolType.HLS else "mp4",
                mime_type="video/mp2t" if proto == ProtocolType.HLS else "video/mp4",
                resolution=f"{height}p" if height else ("audio_only" if is_audio else name),
                height=height,
                fps=fps,
                protocol=proto,
                has_video=not is_audio,
                has_audio=True,
                requires_muxing=(stype == "muxed-stream"),
                direct_media_url=info.get("url") if isinstance(info.get("url"), str) else None,
            )
        )

    # Preserve fallback "best" format if stream only exposed aliases
    if not formats and "best" in streams_map and isinstance(streams_map["best"], dict):
        best = streams_map["best"]
        formats.append(
            FormatInfo(
                format_id="best",
                label="Best Live Stream",
                container="ts",
                mime_type="video/mp2t",
                protocol=ProtocolType.HLS,
                has_video=True,
                has_audio=True,
                direct_media_url=best.get("url"),
            )
        )

    author_name = meta.get("author")
    title = meta.get("title") or (f"{author_name} ({plugin} live)" if author_name else f"{plugin} live stream")

    return NormalizedMediaOutput(
        job_id=ctx.job_id,
        original_url=ctx.validated.original_url,
        url=ctx.url,
        source=SourceInfo(
            domain=ctx.domain,
            platform=plugin.lower(),
            extractor_key=plugin,
            external_id=str(meta["id"]) if meta.get("id") is not None else None,
        ),
        content_type=ContentType.LIVESTREAM,
        is_live=True,
        title=str(title),
        author=AuthorInfo(name=str(author_name)) if author_name else None,
        available_formats=formats,
        metadata=MediaMetadata(
            extra={"category": meta.get("category")} if meta.get("category") else {}
        ),
        download=DownloadInfo(status=DownloadStatus.NOT_REQUESTED),
        engine=EngineProvenance(
            engine_used="streamlink",
            engine_version=version,
            fallback_chain=["streamlink"],
            probe_duration_ms=probe_ms,
        ),
    )


class StreamlinkAdapter(ExtractorAdapter):
    """Live stream extractor and duration-capped recorder (BSD-2-Clause)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._version = "unknown"

    @property
    def id(self) -> str:
        return "streamlink"

    def manifest(self) -> EngineManifest:
        installed = shutil.which(self._settings.streamlink_bin) is not None
        return EngineManifest(
            id=self.id,
            name="Streamlink Live Stream Engine",
            version=self._version,
            tier=2,
            supported_content_types=["livestream", "hls", "dash", "video"],
            supported_domains=SupportedDomains(
                mode="pattern_list",
                domains=sorted(LIVESTREAM_DOMAINS),
                site_count=136,
            ),
            protocols=["http", "https", "hls", "dash"],
            capabilities=[
                EngineCapability.EXTRACT_METADATA,
                EngineCapability.ENUMERATE_FORMATS,
                EngineCapability.EXTRACT_MEDIA_URL,
                EngineCapability.RECORD_LIVESTREAM,
                EngineCapability.DOWNLOAD_MEDIA,
                EngineCapability.SUPPORT_AUTHENTICATED_CONTENT,
            ],
            auth_requirements=AuthRequirements(supports_cookies=True, supports_headers=True),
            cost_profile=CostProfile(avg_probe_ms=1100, memory_mb=96, requires_browser=False),
            priority=920,
            installation_status=(
                InstallationStatus.INSTALLED if installed else InstallationStatus.MISSING
            ),
            license="BSD-2-Clause",
            invocation_mode=InvocationMode.SUBPROCESS,
        )

    def quick_match(self, ctx: UrlContext) -> MatchScore:
        path = ctx.path.lower()
        if ctx.domain in {"twitch.tv", "kick.com", "chzzk.naver.com", "trovo.live", "picarto.tv"}:
            # Twitch clips/VODs are better handled by yt-dlp; live channel pages go to streamlink
            if "/videos/" in path or "/clip/" in path:
                return MatchScore(
                    engine_id=self.id,
                    confidence=45,
                    reason="VOD/clip path prefers yt-dlp.",
                    predicted_content_type="video",
                )
            return MatchScore(
                engine_id=self.id,
                confidence=96,
                reason=f"Primary live-streaming platform '{ctx.domain}'.",
                predicted_content_type="livestream",
            )
        if ctx.domain in LIVESTREAM_DOMAINS or "/live" in path:
            return MatchScore(
                engine_id=self.id,
                confidence=82,
                reason=f"Live-capable platform/path on '{ctx.domain}'.",
                predicted_content_type="livestream",
            )
        return MatchScore(engine_id=self.id, confidence=0, reason="Not a known live-stream target.")

    def build_probe_argv(self, url: str) -> list[str]:
        argv = [
            self._settings.streamlink_bin,
            "--json",
            "--no-config",
            "--http-timeout",
            "15",
        ]
        if self._settings.egress_proxy_url:
            argv.extend(["--http-proxy", self._settings.egress_proxy_url])
        argv.extend(["--", url])
        return argv

    def build_record_argv(
        self,
        url: str,
        quality: str,
        output_path: Path,
        max_duration_seconds: int,
    ) -> list[str]:
        argv = [
            self._settings.streamlink_bin,
            "--no-config",
            "--force",
            "--hls-duration",
            _format_duration_hms(max_duration_seconds),
            "--output",
            str(output_path),
        ]
        if self._settings.egress_proxy_url:
            argv.extend(["--http-proxy", self._settings.egress_proxy_url])
        argv.extend(["--", url, quality])
        return argv

    async def probe(
        self,
        ctx: UrlContext,
        options: ProbeOptions,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> NormalizedMediaOutput:
        t0 = time.monotonic()
        argv = self.build_probe_argv(ctx.url)
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

        try:
            raw = json.loads(res.stdout.strip())
        except json.JSONDecodeError as exc:
            raise AdapterError(
                ErrorCode.ERR_UNSUPPORTED_URL if res.returncode != 0 else ErrorCode.ERR_INTERNAL,
                (res.stderr or res.stdout or str(exc))[-400:],
                engine_id=self.id,
            ) from exc

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return normalize_streamlink_json(raw, ctx, version=self._version, probe_ms=elapsed_ms)

    async def download(
        self,
        ctx: UrlContext,
        plan: DownloadPlan,
        *,
        progress_cb: ProgressCallback | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadArtifact:
        plan.output_dir.mkdir(parents=True, exist_ok=True)
        quality = plan.format_id or "best"
        max_dur = self._settings.max_livestream_duration_seconds
        if plan.clip_end_seconds and plan.clip_start_seconds is not None:
            requested = int(max(1, plan.clip_end_seconds - plan.clip_start_seconds))
            max_dur = min(max_dur, requested)

        disk_name = f"{uuid.uuid4().hex}.ts"
        output_path = ensure_safe_path(plan.output_dir, plan.output_dir / disk_name)
        argv = self.build_record_argv(ctx.url, quality, output_path, max_dur)

        try:
            res = await run_safe_subprocess(
                argv,
                cwd=plan.output_dir,
                timeout=float(min(plan.timeout_seconds, max_dur + 60)),
                proxy_url=self._settings.egress_proxy_url,
                cancel_token=cancel_token,
            )
        except SubprocessTimeoutError as exc:
            output_path.unlink(missing_ok=True)
            raise AdapterError(
                ErrorCode.ERR_TIMEOUT, str(exc), engine_id=self.id, retryable=True
            ) from exc
        except SubprocessCancelledError as exc:
            output_path.unlink(missing_ok=True)
            raise AdapterError(ErrorCode.ERR_INTERNAL, str(exc), engine_id=self.id) from exc

        if res.returncode != 0 or not output_path.exists():
            output_path.unlink(missing_ok=True)
            raise AdapterError(
                ErrorCode.ERR_INTERNAL,
                f"Streamlink recording failed (rc={res.returncode}): {(res.stderr or res.stdout)[-300:]}",
                engine_id=self.id,
                retryable=True,
            )

        size = output_path.stat().st_size
        if size > plan.max_bytes:
            output_path.unlink(missing_ok=True)
            raise AdapterError(
                ErrorCode.ERR_QUOTA_EXCEEDED,
                f"Live recording ({size} bytes) exceeded quota ({plan.max_bytes} bytes).",
                engine_id=self.id,
            )

        return DownloadArtifact(
            local_path=output_path,
            filename=sanitize_filename(f"{ctx.domain}_live.ts"),
            mime_type="video/mp2t",
            size_bytes=size,
            warnings=[
                WarningInfo(
                    code="WARN_LIVESTREAM_CAPPED",
                    message=f"Live recording capped at {max_dur}s server limit.",
                    engine=self.id,
                )
            ],
        )

    async def health_check(self) -> HealthReport:
        exe = shutil.which(self._settings.streamlink_bin)
        if not exe:
            return HealthReport(
                engine_id=self.id,
                installation_status=InstallationStatus.MISSING,
                version="missing",
                healthy=False,
                detail="streamlink not found on PATH.",
            )
        res = await run_safe_subprocess([exe, "--version"], timeout=5.0)
        line = res.stdout.strip() or "unknown"
        version = line.replace("streamlink", "").strip() or "unknown"
        self._version = version
        return HealthReport(
            engine_id=self.id,
            installation_status=InstallationStatus.INSTALLED,
            version=version,
            healthy=(res.returncode == 0),
            detail=line,
        )
