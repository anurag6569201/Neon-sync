"""Tier-3 Sandboxed Playwright Browser Fallback Adapter.

Strictly a discovery/extraction sensor (never downloads large files inside Chromium):
- Launches ephemeral isolated BrowserContext per URL routed through Safe Egress Proxy.
- Injects EME DRM detection hook (`navigator.requestMediaKeySystemAccess`) to fail fast
  with ERR_DRM_PROTECTED on Widevine/FairPlay/PlayReady streams.
- Observes network responses to sniff HLS (.m3u8), DASH (.mpd), progressive MP4/WebM/MP3,
  and subtitles (.vtt/.srt) while filtering out raw .ts/.m4s segments and ad trackers.
- Traverses DOM, open Shadow DOM, iframes, Open Graph, and JSON-LD metadata.
- Hands discovered media/manifest URLs back to Tier-1/2 downloaders (N_m3u8DL-RE/yt-dlp/direct-http).
"""

from __future__ import annotations

import asyncio
import importlib.util
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from neon_sync.config import Settings
from neon_sync.models import (
    AuthorInfo,
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
    SubtitleTrack,
    SupportedDomains,
    ThumbnailInfo,
)
from neon_sync.security import CancellationToken, validate_and_canonicalize_url
from neon_sync.security.url_validator import UrlValidationError

from .base import AdapterError, DownloadArtifact, ExtractorAdapter, ProgressCallback, UrlContext

# Raw stream chunks and init segments should never be presented as standalone downloads
_SEGMENT_RE = re.compile(
    r"\.(?:ts|m4s|cmfv|cmfa|m4f|key)(?:\?|$)|init\.(?:mp4|m4s)|/range/\d+-\d+|[?&](?:range|bytestart|byterange)=",
    re.IGNORECASE,
)

_AD_TRACKER_HOSTS = (
    "doubleclick.net",
    "googlesyndication.com",
    "google-analytics.com",
    "googletagmanager.com",
    "facebook.net",
    "adservice.",
    "imasdk.googleapis.com",
)

_JS_SHELL_HINTS = (
    'id="root"',
    'id="app"',
    'id="__next"',
    "__NEXT_DATA__",
    "<noscript>",
    "hls.js",
    "dash.all.min.js",
    "video.js",
    "jwplayer",
    "plyr",
    "shaka-player",
)

_EME_DRM_INIT_SCRIPT = """
(() => {
  try {
    window.__neonSyncDrm = '';
    if (navigator.requestMediaKeySystemAccess) {
      const orig = navigator.requestMediaKeySystemAccess.bind(navigator);
      navigator.requestMediaKeySystemAccess = function(keySystem, configs) {
        window.__neonSyncDrm = String(keySystem || 'EME');
        return orig(keySystem, configs);
      };
    }
  } catch (_) {}
})();
"""

_INSPECT_DOM_JS = """
(() => {
  const roots = [document];
  const mediaEls = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.tagName === 'VIDEO' || el.tagName === 'AUDIO') mediaEls.push(el);
      if (el.shadowRoot) { roots.push(el.shadowRoot); walk(el.shadowRoot); }
    }
  };
  try { walk(document); } catch (_) {}

  const urls = [];
  for (const el of mediaEls) {
    const src = el.currentSrc || el.getAttribute('src') || el.getAttribute('data-src') || '';
    if (src && !src.startsWith('blob:') && !src.startsWith('data:')) {
      urls.push({ url: src, kind: el.tagName === 'AUDIO' ? 'audio' : 'video' });
    }
    for (const s of el.querySelectorAll('source')) {
      const ss = s.getAttribute('src') || '';
      if (ss && !ss.startsWith('blob:') && !ss.startsWith('data:')) {
        urls.push({ url: ss, kind: el.tagName === 'AUDIO' ? 'audio' : 'video' });
      }
    }
    try { el.muted = true; el.play().catch(() => {}); } catch (_) {}
  }

  if (mediaEls.length === 0) {
    const sel = 'button[aria-label*="play" i], .vjs-big-play-button, .plyr__control--overlaid, .jw-display-icon-container';
    for (const r of roots) {
      const btn = r.querySelector(sel);
      if (btn) { try { btn.click(); } catch (_) {} break; }
    }
  }

  const meta = (attr, key) => {
    const el = document.querySelector(`meta[${attr}="${key}"]`);
    return el ? (el.getAttribute('content') || '').trim() : '';
  };

  const jsonLd = [];
  for (const sc of document.querySelectorAll('script[type*="ld+json"]')) {
    try { jsonLd.push(JSON.parse(sc.textContent || '')); } catch (_) {}
  }

  return {
    title: meta('property', 'og:title') || document.title || '',
    description: meta('property', 'og:description') || meta('name', 'description') || '',
    thumbnail: meta('property', 'og:image') || meta('name', 'twitter:image') || '',
    author: meta('name', 'author') || '',
    drm: window.__neonSyncDrm || '',
    mediaUrls: urls,
    jsonLd,
  };
})();
"""


@dataclass(frozen=True)
class SniffedCandidate:
    url: str
    kind: str  # "hls" | "dash" | "video" | "audio" | "subtitle"
    mime_type: str
    score: int
    size_bytes: int | None = None


def is_segment_url(url: str) -> bool:
    """Return True if URL points to a raw HLS/DASH fragment rather than a full media/manifest."""
    return bool(_SEGMENT_RE.search(url))


def detect_js_shell_html(html: str) -> bool:
    """Heuristic check indicating a page likely requires JS rendering for media discovery."""
    low = html.lower()
    return any(hint.lower() in low for hint in _JS_SHELL_HINTS)


def classify_sniffed_request(
    url: str,
    content_type: str | None = None,
    content_length: int | None = None,
) -> SniffedCandidate | None:
    """Classify an observed browser network response into a ranked media/manifest candidate."""
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    if any(ad in host for ad in _AD_TRACKER_HOSTS):
        return None
    if is_segment_url(url):
        return None

    ct = (content_type or "").split(";")[0].strip().lower()
    ext = Path(parsed.path).suffix.lower()

    if ext == ".m3u8" or ct in {"application/vnd.apple.mpegurl", "application/x-mpegurl"}:
        return SniffedCandidate(
            url=url,
            kind="hls",
            mime_type="application/vnd.apple.mpegurl",
            score=100,
            size_bytes=content_length,
        )
    if ext == ".mpd" or ct == "application/dash+xml":
        return SniffedCandidate(
            url=url,
            kind="dash",
            mime_type="application/dash+xml",
            score=98,
            size_bytes=content_length,
        )
    if ext in {".vtt", ".srt", ".ttml"} or ct in {"text/vtt", "application/x-subrip"}:
        return SniffedCandidate(
            url=url,
            kind="subtitle",
            mime_type=ct or "text/vtt",
            score=60,
            size_bytes=content_length,
        )
    if ct.startswith("video/") or ext in {".mp4", ".webm", ".mov", ".mkv"}:
        # Ignore tiny tracking/probe chunks (< 50 KB when size is known)
        if content_length is not None and content_length < 50_000:
            return None
        return SniffedCandidate(
            url=url,
            kind="video",
            mime_type=ct or mimetypes.guess_type(url)[0] or "video/mp4",
            score=85,
            size_bytes=content_length,
        )
    if ct.startswith("audio/") or ext in {".mp3", ".m4a", ".ogg", ".opus", ".aac", ".flac"}:
        if content_length is not None and content_length < 20_000:
            return None
        return SniffedCandidate(
            url=url,
            kind="audio",
            mime_type=ct or mimetypes.guess_type(url)[0] or "audio/mpeg",
            score=75,
            size_bytes=content_length,
        )
    return None


def rank_sniffed_candidates(candidates: list[SniffedCandidate]) -> list[SniffedCandidate]:
    """Deduplicate by URL and sort highest-priority candidates first."""
    best_by_url: dict[str, SniffedCandidate] = {}
    for c in candidates:
        prev = best_by_url.get(c.url)
        if prev is None or c.score > prev.score:
            best_by_url[c.url] = c
    return sorted(
        best_by_url.values(),
        key=lambda c: (c.score, c.size_bytes or 0),
        reverse=True,
    )


class PlaywrightFallbackAdapter(ExtractorAdapter):
    """Tier-3 Playwright headless browser fallback for JS-rendered media & embedded players."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._version = "not-installed"
        self._sem = asyncio.Semaphore(settings.browser_max_contexts)
        self._jobs_processed = 0

    @property
    def id(self) -> str:
        return "playwright"

    @staticmethod
    def _playwright_available() -> bool:
        return importlib.util.find_spec("playwright") is not None

    def manifest(self) -> EngineManifest:
        installed = self._playwright_available() and self._settings.enable_browser_fallback
        return EngineManifest(
            id=self.id,
            name="Playwright Browser Fallback (Tier-3)",
            version=self._version,
            tier=3,
            supported_content_types=["video", "audio", "hls", "dash", "webpage", "image"],
            supported_domains=SupportedDomains(mode="wildcard"),
            protocols=["http", "https", "hls", "dash"],
            capabilities=[
                EngineCapability.EXTRACT_METADATA,
                EngineCapability.EXTRACT_MEDIA_URL,
                EngineCapability.EXTRACT_THUMBNAIL,
                EngineCapability.RENDER_JAVASCRIPT,
            ],
            cost_profile=CostProfile(avg_probe_ms=6500, memory_mb=320, requires_browser=True),
            priority=300,
            installation_status=(
                InstallationStatus.INSTALLED if installed else InstallationStatus.MISSING
            ),
            license="Apache-2.0",
            invocation_mode=InvocationMode.SUBPROCESS,
        )

    def quick_match(self, ctx: UrlContext) -> MatchScore:
        ct = (ctx.head_content_type or "").split(";")[0].strip().lower()
        if ct and ct not in {"text/html", "application/xhtml+xml"}:
            return MatchScore(
                engine_id=self.id,
                confidence=0,
                reason="Non-HTML resource does not require browser rendering.",
            )
        return MatchScore(
            engine_id=self.id,
            confidence=30,
            reason="Tier-3 headless browser escalation for JS-rendered pages.",
            predicted_content_type="webpage",
        )

    async def probe(
        self,
        ctx: UrlContext,
        options: ProbeOptions,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> NormalizedMediaOutput:
        if not self._playwright_available():
            raise AdapterError(
                ErrorCode.ERR_INTERNAL,
                "Playwright is not installed. Install with `pip install 'neon-sync[browser]'` and `playwright install chromium`.",
                engine_id=self.id,
            )

        from playwright.async_api import async_playwright

        t0 = time.monotonic()
        timeout_ms = min(options.timeout_seconds, self._settings.browser_timeout_seconds) * 1000
        sniffed: list[SniffedCandidate] = []
        subtitles: list[SubtitleTrack] = []

        async with self._sem, async_playwright() as pw:
            launch_args = [
                "--disable-dev-shm-usage",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ]
            proxy_cfg = (
                {"server": self._settings.egress_proxy_url}
                if self._settings.egress_proxy_url
                else None
            )
            browser = await pw.chromium.launch(headless=True, args=launch_args, proxy=proxy_cfg)
            try:
                context = await browser.new_context(
                    ignore_https_errors=False,
                    accept_downloads=False,
                    java_script_enabled=True,
                )
                await context.add_init_script(_EME_DRM_INIT_SCRIPT)

                async def _route_guard(route) -> None:
                    req = route.request
                    # Block heavy non-media assets to speed up discovery
                    if req.resource_type in {"font", "beacon"}:
                        await route.abort()
                        return
                    try:
                        validate_and_canonicalize_url(
                            req.url,
                            allow_private=self._settings.allow_private_addresses,
                            max_length=self._settings.max_url_length,
                        )
                    except UrlValidationError:
                        await route.abort()
                        return
                    await route.continue_()

                await context.route("**/*", _route_guard)
                page = await context.new_page()

                def _on_response(response) -> None:
                    headers = response.headers
                    cl_raw = headers.get("content-length")
                    cl = int(cl_raw) if (cl_raw and cl_raw.isdigit()) else None
                    cand = classify_sniffed_request(
                        response.url,
                        headers.get("content-type"),
                        cl,
                    )
                    if cand is not None:
                        sniffed.append(cand)

                page.on("response", _on_response)

                try:
                    await page.goto(ctx.url, wait_until="domcontentloaded", timeout=timeout_ms)
                    await page.wait_for_timeout(1500)
                    dom = await page.evaluate(_INSPECT_DOM_JS)
                    # Allow player network requests triggered by muted play click to fire
                    await page.wait_for_timeout(2000)
                    drm_key = dom.get("drm") or await page.evaluate("window.__neonSyncDrm || ''")
                except Exception as exc:
                    raise AdapterError(
                        ErrorCode.ERR_TIMEOUT if "Timeout" in str(exc) else ErrorCode.ERR_INTERNAL,
                        f"Playwright navigation failed: {exc}",
                        engine_id=self.id,
                        retryable=True,
                    ) from exc

                if drm_key:
                    raise AdapterError(
                        ErrorCode.ERR_DRM_PROTECTED,
                        f"Page requested EME DRM key system ({drm_key}); DRM bypass is not supported.",
                        engine_id=self.id,
                    )

                final_url = page.url
                for item in dom.get("mediaUrls") or []:
                    abs_url = urljoin(final_url, item.get("url", ""))
                    cand = classify_sniffed_request(abs_url)
                    if cand:
                        sniffed.append(cand)

                for node in dom.get("jsonLd") or []:
                    if isinstance(node, dict) and isinstance(node.get("contentUrl"), str):
                        cand = classify_sniffed_request(urljoin(final_url, node["contentUrl"]))
                        if cand:
                            sniffed.append(cand)

                await context.close()
            finally:
                await browser.close()
                self._jobs_processed += 1

        ranked = rank_sniffed_candidates(sniffed)
        formats: list[FormatInfo] = []
        for idx, cand in enumerate(ranked, start=1):
            if cand.kind == "subtitle":
                subtitles.append(
                    SubtitleTrack(
                        language="und",
                        is_auto_generated=False,
                        formats=[Path(urlsplit(cand.url).path).suffix.lstrip(".") or "vtt"],
                        url=cand.url,
                    )
                )
                continue
            proto = (
                ProtocolType.HLS
                if cand.kind == "hls"
                else ProtocolType.DASH
                if cand.kind == "dash"
                else ProtocolType.HTTPS
                if cand.url.startswith("https://")
                else ProtocolType.HTTP
            )
            ext = Path(urlsplit(cand.url).path).suffix.lstrip(".").lower() or (
                "m3u8" if cand.kind == "hls" else "mpd" if cand.kind == "dash" else "mp4"
            )
            formats.append(
                FormatInfo(
                    format_id=f"sniff-{idx}",
                    label=f"Sniffed {cand.kind.upper()} ({ext.upper()})",
                    container="mp4" if cand.kind in {"hls", "dash"} else ext,
                    mime_type=cand.mime_type,
                    size_bytes=cand.size_bytes,
                    protocol=proto,
                    has_video=(cand.kind in {"hls", "dash", "video"}),
                    has_audio=True,
                    direct_media_url=cand.url,
                )
            )

        content_type = (
            ContentType.VIDEO
            if any(f.has_video for f in formats)
            else ContentType.AUDIO
            if formats
            else ContentType.WEBPAGE
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        return NormalizedMediaOutput(
            job_id=ctx.job_id,
            original_url=ctx.validated.original_url,
            url=final_url,
            source=SourceInfo(domain=ctx.domain, platform="browser", extractor_key="PlaywrightFallback"),
            content_type=content_type,
            title=dom.get("title") or ctx.domain,
            description=dom.get("description") or None,
            thumbnail=ThumbnailInfo(url=urljoin(final_url, dom["thumbnail"])) if dom.get("thumbnail") else None,
            author=AuthorInfo(name=dom["author"]) if dom.get("author") else None,
            available_formats=formats,
            subtitles=subtitles,
            metadata=MediaMetadata(extra={"sniffed_count": len(formats)}),
            download=DownloadInfo(status=DownloadStatus.NOT_REQUESTED),
            engine=EngineProvenance(
                engine_used=self.id,
                engine_version=self._version,
                fallback_chain=[self.id],
                used_browser_fallback=True,
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
        raise AdapterError(
            ErrorCode.ERR_UNSUPPORTED_URL,
            "Playwright is a discovery-only fallback; hand off discovered direct_media_url to Tier-1/2 downloader.",
            engine_id=self.id,
        )

    async def health_check(self) -> HealthReport:
        if not self._playwright_available():
            return HealthReport(
                engine_id=self.id,
                installation_status=InstallationStatus.MISSING,
                version="missing",
                healthy=False,
                detail="Playwright package not installed (optional Tier-3 extra).",
            )
        from importlib.metadata import version as pkg_version

        ver = pkg_version("playwright")
        self._version = ver
        return HealthReport(
            engine_id=self.id,
            installation_status=InstallationStatus.INSTALLED,
            version=ver,
            healthy=self._settings.enable_browser_fallback,
            detail=f"Playwright {ver} ready (max_contexts={self._settings.browser_max_contexts})",
        )
