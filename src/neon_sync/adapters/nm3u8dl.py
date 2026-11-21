"""Tier-2 High-Throughput HLS / DASH / ISM Stream Adapter via N_m3u8DL-RE (MIT).

Features:
- Pre-validates raw HLS (.m3u8) and DASH (.mpd) manifests before launching binary:
  rejects DRM-protected manifests (SAMPLE-AES / Widevine / PlayReady / FairPlay)
  and dangerous local-file/SSRF directives (`file://`, `concat:`, `subfile:`).
- Multi-threaded segmented downloading routed through Safe Egress Proxy.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from neon_sync.config import Settings
from neon_sync.models import (
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
)
from neon_sync.security import (
    CancellationToken,
    SubprocessCancelledError,
    SubprocessTimeoutError,
    find_binary,
    run_safe_subprocess,
    sanitize_filename,
)

from .base import AdapterError, DownloadArtifact, ExtractorAdapter, ProgressCallback, UrlContext

_MANIFEST_EXTS = (".m3u8", ".mpd", ".ism", ".ism/manifest")
_MANIFEST_MIMES = frozenset(
    {
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "audio/mpegurl",
        "application/dash+xml",
    }
)

_DANGEROUS_MANIFEST_RE = re.compile(r"(?:file|concat|subfile|gopher|ftp|dict)://|concat:|subfile:", re.IGNORECASE)
_HLS_DRM_RE = re.compile(
    r"#EXT-X-KEY:[^\n]*(?:SAMPLE-AES|com\.apple\.streamingkeydelivery|edef8ba9-79d6-4ace-a3c8-27dcd51d21ed)",
    re.IGNORECASE,
)
_DASH_DRM_RE = re.compile(
    r"<ContentProtection[^>]*(?:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed|9a04f079-9840-4286-ab92-e65be0885f95|cenc|widevine|playready)",
    re.IGNORECASE,
)
_HLS_VARIANT_RE = re.compile(
    r"#EXT-X-STREAM-INF:([^\n]+)\n([^\n#]+)",
    re.IGNORECASE,
)
_HLS_RES_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)")
_HLS_BW_RE = re.compile(r"BANDWIDTH=(\d+)")


def inspect_manifest_security_and_variants(
    text: str,
    manifest_url: str,
    *,
    max_segments: int = 5000,
) -> dict:
    """Validate manifest safety (block SSRF/LFR directives & DRM) and extract HLS/DASH info."""
    if _DANGEROUS_MANIFEST_RE.search(text):
        raise AdapterError(
            ErrorCode.ERR_SSRF_BLOCKED,
            "Manifest contains forbidden protocol directive (file/concat/subfile/etc.).",
            engine_id="n-m3u8dl-re",
        )
    if _HLS_DRM_RE.search(text) or _DASH_DRM_RE.search(text):
        raise AdapterError(
            ErrorCode.ERR_DRM_PROTECTED,
            "Stream manifest is protected by DRM (Widevine/PlayReady/FairPlay/SAMPLE-AES).",
            engine_id="n-m3u8dl-re",
        )

    is_hls = "#EXTM3U" in text.upper()
    is_dash = "<MPD" in text.upper()
    if not is_hls and not is_dash:
        raise AdapterError(
            ErrorCode.ERR_UNSUPPORTED_URL,
            "URL did not return a valid HLS (#EXTM3U) or DASH (<MPD) manifest.",
            engine_id="n-m3u8dl-re",
        )

    segment_count = text.count("#EXTINF:") if is_hls else text.count("<Segment")
    if segment_count > max_segments:
        raise AdapterError(
            ErrorCode.ERR_QUOTA_EXCEEDED,
            f"Manifest exceeds maximum allowed segment count ({segment_count} > {max_segments}).",
            engine_id="n-m3u8dl-re",
        )

    is_live = False
    if is_hls and "#EXTINF:" in text and "#EXT-X-ENDLIST" not in text or is_dash and 'type="dynamic"' in text.lower():
        is_live = True

    formats: list[FormatInfo] = []
    if is_hls:
        for idx, m in enumerate(_HLS_VARIANT_RE.finditer(text), start=1):
            attrs, uri = m.group(1), m.group(2).strip()
            res_m = _HLS_RES_RE.search(attrs)
            bw_m = _HLS_BW_RE.search(attrs)
            w = int(res_m.group(1)) if res_m else None
            h = int(res_m.group(2)) if res_m else None
            bw_kbps = round(int(bw_m.group(1)) / 1000.0, 1) if bw_m else None
            formats.append(
                FormatInfo(
                    format_id=f"hls-{idx}",
                    label=f"{h}p HLS" if h else f"HLS Variant {idx}",
                    container="mp4",
                    mime_type="application/vnd.apple.mpegurl",
                    resolution=f"{w}x{h}" if (w and h) else None,
                    width=w,
                    height=h,
                    video_bitrate_kbps=bw_kbps,
                    protocol=ProtocolType.HLS,
                    has_video=True,
                    has_audio=True,
                    direct_media_url=urljoin(manifest_url, uri),
                )
            )
        if not formats:
            formats.append(
                FormatInfo(
                    format_id="hls-best",
                    label="HLS Stream",
                    container="mp4",
                    mime_type="application/vnd.apple.mpegurl",
                    protocol=ProtocolType.HLS,
                    has_video=True,
                    has_audio=True,
                    direct_media_url=manifest_url,
                )
            )
    else:
        formats.append(
            FormatInfo(
                format_id="dash-best",
                label="MPEG-DASH Stream",
                container="mp4",
                mime_type="application/dash+xml",
                protocol=ProtocolType.DASH,
                has_video=True,
                has_audio=True,
                requires_muxing=True,
                direct_media_url=manifest_url,
            )
        )

    return {
        "protocol": "hls" if is_hls else "dash",
        "is_live": is_live,
        "formats": formats,
        "segment_count": segment_count,
    }


class NM3u8DlReAdapter(ExtractorAdapter):
    """High-speed multi-threaded HLS/DASH/ISM downloader using N_m3u8DL-RE (MIT)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._version = "unknown"

    @property
    def id(self) -> str:
        return "n-m3u8dl-re"

    def _exe(self) -> str:
        return find_binary(self._settings.nm3u8dl_bin) or self._settings.nm3u8dl_bin

    def manifest(self) -> EngineManifest:
        installed = find_binary(self._settings.nm3u8dl_bin) is not None
        return EngineManifest(
            id=self.id,
            name="N_m3u8DL-RE Segmented Stream Engine",
            version=self._version,
            tier=2,
            supported_content_types=["hls", "dash", "video", "livestream"],
            supported_domains=SupportedDomains(mode="wildcard"),
            protocols=["http", "https", "hls", "dash", "ism"],
            capabilities=[
                EngineCapability.EXTRACT_METADATA,
                EngineCapability.ENUMERATE_FORMATS,
                EngineCapability.DOWNLOAD_MEDIA,
                EngineCapability.RECORD_LIVESTREAM,
            ],
            cost_profile=CostProfile(avg_probe_ms=400, memory_mb=80, requires_browser=False),
            priority=910,
            installation_status=(
                InstallationStatus.INSTALLED if installed else InstallationStatus.MISSING
            ),
            license="MIT",
            invocation_mode=InvocationMode.SUBPROCESS,
        )

    def quick_match(self, ctx: UrlContext) -> MatchScore:
        path = urlsplit(ctx.url).path.lower()
        ct = (ctx.head_content_type or "").split(";")[0].strip().lower()
        if path.endswith(_MANIFEST_EXTS) or ct in _MANIFEST_MIMES:
            return MatchScore(
                engine_id=self.id,
                confidence=97,
                reason="Direct HLS/DASH/ISM manifest detected.",
                predicted_content_type="video",
            )
        return MatchScore(engine_id=self.id, confidence=0, reason="Not a direct stream manifest.")

    def build_download_argv(
        self,
        url: str,
        output_dir: Path,
        save_name: str,
        *,
        is_live: bool = False,
    ) -> list[str]:
        argv = [
            self._exe(),
            url,
            "--save-dir",
            str(output_dir),
            "--tmp-dir",
            str(output_dir / "tmp"),
            "--save-name",
            save_name,
            "--thread-count",
            "8",
            "--auto-select",
            "--no-log",
            "--binary-merge",
            "false",
            "-M",
            "format=mp4",
        ]
        if is_live:
            limit = self._settings.max_livestream_duration_seconds
            h, rem = divmod(limit, 3600)
            m, s = divmod(rem, 60)
            argv.extend(["--live-record-limit", f"{h:02d}:{m:02d}:{s:02d}"])
        if self._settings.egress_proxy_url:
            argv.extend(["--custom-proxy", self._settings.egress_proxy_url])
        return argv

    async def probe(
        self,
        ctx: UrlContext,
        options: ProbeOptions,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> NormalizedMediaOutput:
        t0 = time.monotonic()
        async with httpx.AsyncClient(
            proxy=self._settings.egress_proxy_url,
            follow_redirects=True,
            max_redirects=self._settings.max_redirects,
            timeout=httpx.Timeout(float(options.timeout_seconds), connect=10.0),
        ) as client:
            try:
                resp = await client.get(ctx.url)
                if resp.status_code == 404:
                    raise AdapterError(ErrorCode.ERR_NOT_FOUND, "Manifest not found (404).", engine_id=self.id)
                if resp.status_code >= 400:
                    raise AdapterError(
                        ErrorCode.ERR_INTERNAL,
                        f"Manifest fetch returned HTTP {resp.status_code}.",
                        engine_id=self.id,
                        retryable=resp.status_code >= 500,
                    )
                text = resp.text[: 2 * 1024 * 1024]
            except AdapterError:
                raise
            except httpx.HTTPError as exc:
                raise AdapterError(
                    ErrorCode.ERR_INTERNAL, f"Manifest fetch failed: {exc}", engine_id=self.id, retryable=True
                ) from exc

        info = inspect_manifest_security_and_variants(text, str(resp.url))
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        name = sanitize_filename(Path(urlsplit(str(resp.url)).path).stem or ctx.domain)

        return NormalizedMediaOutput(
            job_id=ctx.job_id,
            original_url=ctx.validated.original_url,
            url=str(resp.url),
            source=SourceInfo(domain=ctx.domain, platform=info["protocol"], extractor_key="N_m3u8DL-RE"),
            content_type=ContentType.LIVESTREAM if info["is_live"] else ContentType.VIDEO,
            is_live=info["is_live"],
            title=name,
            available_formats=info["formats"],
            metadata=MediaMetadata(extra={"segment_count": info["segment_count"]}),
            download=DownloadInfo(status=DownloadStatus.NOT_REQUESTED),
            engine=EngineProvenance(
                engine_used=self.id,
                engine_version=self._version,
                fallback_chain=[self.id],
                probe_duration_ms=elapsed_ms,
            ),
        )

    async def download(
        self,
        ctx: UrlContext,
        plan: DownloadPlan,
        *,
        progress_cb: ProgressCallback | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadArtifact:
        if find_binary(self._settings.nm3u8dl_bin) is None:
            raise AdapterError(
                ErrorCode.ERR_INTERNAL, "N_m3u8DL-RE binary is not installed.", engine_id=self.id
            )
        plan.output_dir.mkdir(parents=True, exist_ok=True)
        save_name = f"stream_{uuid.uuid4().hex[:12]}"
        argv = self.build_download_argv(ctx.url, plan.output_dir, save_name)

        try:
            res = await run_safe_subprocess(
                argv,
                cwd=plan.output_dir,
                timeout=float(plan.timeout_seconds),
                proxy_url=self._settings.egress_proxy_url,
                cancel_token=cancel_token,
            )
        except SubprocessTimeoutError as exc:
            raise AdapterError(ErrorCode.ERR_TIMEOUT, str(exc), engine_id=self.id, retryable=True) from exc
        except SubprocessCancelledError as exc:
            raise AdapterError(ErrorCode.ERR_INTERNAL, str(exc), engine_id=self.id) from exc

        if res.returncode != 0:
            raise AdapterError(
                ErrorCode.ERR_INTERNAL,
                f"N_m3u8DL-RE failed (rc={res.returncode}): {(res.stderr or res.stdout)[-300:]}",
                engine_id=self.id,
                retryable=True,
            )

        candidates = sorted(
            (p for p in plan.output_dir.glob(f"{save_name}*") if p.is_file()),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        if not candidates:
            raise AdapterError(
                ErrorCode.ERR_INTERNAL, "N_m3u8DL-RE produced no output file.", engine_id=self.id
            )
        primary = candidates[0]
        size = primary.stat().st_size
        if size > plan.max_bytes:
            primary.unlink(missing_ok=True)
            raise AdapterError(
                ErrorCode.ERR_QUOTA_EXCEEDED,
                f"Stream output ({size} bytes) exceeded quota ({plan.max_bytes} bytes).",
                engine_id=self.id,
            )

        return DownloadArtifact(
            local_path=primary,
            filename=sanitize_filename(f"{ctx.domain}_stream{primary.suffix}"),
            mime_type="video/mp4" if primary.suffix == ".mp4" else "video/mp2t",
            size_bytes=size,
        )

    async def health_check(self) -> HealthReport:
        exe = find_binary(self._settings.nm3u8dl_bin)
        if not exe:
            return HealthReport(
                engine_id=self.id,
                installation_status=InstallationStatus.MISSING,
                version="missing",
                healthy=False,
                detail="N_m3u8DL-RE not found on PATH.",
            )
        res = await run_safe_subprocess([exe, "--version"], timeout=5.0)
        version = res.stdout.strip() or "unknown"
        self._version = version
        return HealthReport(
            engine_id=self.id,
            installation_status=InstallationStatus.INSTALLED,
            version=version,
            healthy=(res.returncode == 0),
            detail=f"N_m3u8DL-RE {version}",
        )
