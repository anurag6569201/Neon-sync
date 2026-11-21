"""Tier-1 Fast Direct HTTP & Static Webpage Metadata Adapter.

Handles:
- Direct downloadable files (PDF, images, audio, video, archives) via httpx streaming
- Raw HLS (.m3u8) / DASH (.mpd) detection
- Public webpages: Open Graph, Twitter Cards, JSON-LD (VideoObject/AudioObject/Article),
  HTML5 <video>/<audio> tags, and optional Trafilatura article metadata
"""

from __future__ import annotations

import json
import mimetypes
import re
import time
import uuid
from email.message import Message
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from neon_sync import __version__
from neon_sync.config import Settings
from neon_sync.models import (
    AuthorInfo,
    ContentType,
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
    MediaMetadata,
    NormalizedMediaOutput,
    ProbeOptions,
    ProgressEvent,
    ProtocolType,
    SourceInfo,
    SupportedDomains,
    ThumbnailInfo,
    WarningInfo,
)
from neon_sync.security import (
    CancellationToken,
    detect_magic_mime,
    ensure_safe_path,
    sanitize_filename,
)
from neon_sync.security.url_validator import UrlValidationError, validate_and_canonicalize_url

from .base import AdapterError, DownloadArtifact, ExtractorAdapter, ProgressCallback, UrlContext

DIRECT_FILE_EXTENSIONS: dict[str, tuple[ContentType, str]] = {
    ".pdf": (ContentType.DOCUMENT, "application/pdf"),
    ".epub": (ContentType.DOCUMENT, "application/epub+zip"),
    ".docx": (ContentType.DOCUMENT, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".csv": (ContentType.DOCUMENT, "text/csv"),
    ".txt": (ContentType.DOCUMENT, "text/plain"),
    ".mp4": (ContentType.VIDEO, "video/mp4"),
    ".webm": (ContentType.VIDEO, "video/webm"),
    ".mov": (ContentType.VIDEO, "video/quicktime"),
    ".mkv": (ContentType.VIDEO, "video/x-matroska"),
    ".mp3": (ContentType.AUDIO, "audio/mpeg"),
    ".m4a": (ContentType.AUDIO, "audio/mp4"),
    ".wav": (ContentType.AUDIO, "audio/wav"),
    ".flac": (ContentType.AUDIO, "audio/flac"),
    ".ogg": (ContentType.AUDIO, "audio/ogg"),
    ".opus": (ContentType.AUDIO, "audio/opus"),
    ".jpg": (ContentType.IMAGE, "image/jpeg"),
    ".jpeg": (ContentType.IMAGE, "image/jpeg"),
    ".png": (ContentType.IMAGE, "image/png"),
    ".webp": (ContentType.IMAGE, "image/webp"),
    ".gif": (ContentType.IMAGE, "image/gif"),
    ".avif": (ContentType.IMAGE, "image/avif"),
    ".zip": (ContentType.ARCHIVE, "application/zip"),
    ".tar": (ContentType.ARCHIVE, "application/x-tar"),
    ".gz": (ContentType.ARCHIVE, "application/gzip"),
    ".7z": (ContentType.ARCHIVE, "application/x-7z-compressed"),
    ".iso": (ContentType.FILE, "application/octet-stream"),
}

_USER_AGENT = f"NeonSync/{__version__} (+https://github.com/anurag6569201/Neon-sync)"
_MAX_HTML_BYTES = 2 * 1024 * 1024  # 2 MiB HTML inspection cap


def _parse_content_disposition_filename(header_value: str | None) -> str | None:
    if not header_value:
        return None
    msg = Message()
    msg["content-disposition"] = header_value
    filename = msg.get_filename()
    if filename:
        return sanitize_filename(filename)
    return None


def _classify_mime(mime: str | None, path: str) -> ContentType:
    m = (mime or "").split(";")[0].strip().lower()
    if m.startswith("video/"):
        return ContentType.VIDEO
    if m.startswith("audio/"):
        return ContentType.AUDIO
    if m.startswith("image/"):
        return ContentType.IMAGE
    if m in {"application/pdf", "application/epub+zip", "text/csv", "text/plain"}:
        return ContentType.DOCUMENT
    if m in {"application/zip", "application/gzip", "application/x-tar", "application/x-7z-compressed"}:
        return ContentType.ARCHIVE
    if m in {"text/html", "application/xhtml+xml"}:
        return ContentType.WEBPAGE
    if m in {"application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml"}:
        return ContentType.VIDEO

    ext = Path(urlsplit(path).path).suffix.lower()
    if ext in DIRECT_FILE_EXTENSIONS:
        return DIRECT_FILE_EXTENSIONS[ext][0]
    if m == "application/octet-stream":
        return ContentType.FILE
    return ContentType.UNKNOWN


def _extract_html_metadata(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    def _meta(attr: str, key: str) -> str | None:
        tag = soup.find("meta", attrs={attr: key})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
        return None

    title = (
        _meta("property", "og:title")
        or _meta("name", "twitter:title")
        or (soup.title.string.strip() if soup.title and soup.title.string else None)
    )
    description = (
        _meta("property", "og:description")
        or _meta("name", "description")
        or _meta("name", "twitter:description")
    )
    og_image = _meta("property", "og:image") or _meta("name", "twitter:image")
    og_video = _meta("property", "og:video:secure_url") or _meta("property", "og:video")
    og_audio = _meta("property", "og:audio")
    author = _meta("name", "author") or _meta("property", "article:author")

    canonical_tag = soup.find("link", rel=lambda v: v and "canonical" in v)
    canonical_url = (
        urljoin(base_url, str(canonical_tag["href"]))
        if canonical_tag and canonical_tag.get("href")
        else None
    )

    json_ld_items: list[dict] = []
    embedded_media: list[tuple[str, str]] = []
    if og_video:
        embedded_media.append((urljoin(base_url, og_video), "video"))
    if og_audio:
        embedded_media.append((urljoin(base_url, og_audio), "audio"))

    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.IGNORECASE)}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes = parsed if isinstance(parsed, list) else [parsed]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            json_ld_items.append(node)
            if not title and isinstance(node.get("name"), str):
                title = node["name"]
            if not description and isinstance(node.get("description"), str):
                description = node["description"]
            if not og_image:
                img = node.get("thumbnailUrl") or node.get("image")
                if isinstance(img, str):
                    og_image = img
                elif isinstance(img, list) and img and isinstance(img[0], str):
                    og_image = img[0]
            content_url = node.get("contentUrl")
            if isinstance(content_url, str):
                node_type = str(node.get("@type", "")).lower()
                kind = "audio" if "audio" in node_type else "video"
                embedded_media.append((urljoin(base_url, content_url), kind))

    # Inspect HTML5 <video>/<audio>/<source> tags
    for tag in soup.find_all(["video", "audio"]):
        kind = "audio" if tag.name == "audio" else "video"
        src = tag.get("src")
        if src and not str(src).startswith(("blob:", "data:")):
            embedded_media.append((urljoin(base_url, str(src)), kind))
        for source in tag.find_all("source"):
            ssrc = source.get("src")
            if ssrc and not str(ssrc).startswith(("blob:", "data:")):
                embedded_media.append((urljoin(base_url, str(ssrc)), kind))

    # Deduplicate embedded media preserving order
    seen: set[str] = set()
    unique_media: list[tuple[str, str]] = []
    for u, kind in embedded_media:
        if u not in seen:
            seen.add(u)
            unique_media.append((u, kind))

    return {
        "title": title,
        "description": description,
        "thumbnail_url": urljoin(base_url, og_image) if og_image else None,
        "author": author,
        "canonical_url": canonical_url,
        "embedded_media": unique_media,
        "json_ld_count": len(json_ld_items),
    }


class DirectHttpAdapter(ExtractorAdapter):
    """Tier-1 adapter for direct files, documents, images, and static HTML metadata."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def id(self) -> str:
        return "direct-http"

    def manifest(self) -> EngineManifest:
        return EngineManifest(
            id=self.id,
            name="Direct HTTP & Static Metadata",
            version=__version__,
            tier=1,
            supported_content_types=[
                "document",
                "image",
                "archive",
                "file",
                "video",
                "audio",
                "webpage",
            ],
            supported_domains=SupportedDomains(mode="wildcard"),
            protocols=["http", "https"],
            capabilities=[
                EngineCapability.EXTRACT_METADATA,
                EngineCapability.EXTRACT_MEDIA_URL,
                EngineCapability.DOWNLOAD_DIRECT_FILE,
                EngineCapability.DOWNLOAD_MEDIA,
            ],
            priority=950,
            installation_status=InstallationStatus.INSTALLED,
            license="Proprietary (uses BSD-3-Clause httpx + MIT BeautifulSoup4)",
            invocation_mode=InvocationMode.IN_PROCESS,
        )

    def _build_client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            proxy=self._settings.egress_proxy_url,
            follow_redirects=True,
            max_redirects=self._settings.max_redirects,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"User-Agent": _USER_AGENT},
        )

    def quick_match(self, ctx: UrlContext) -> MatchScore:
        ext = Path(urlsplit(ctx.url).path).suffix.lower()
        ct = (ctx.head_content_type or "").split(";")[0].strip().lower()

        if ctx.mode == "direct":
            return MatchScore(
                engine_id=self.id,
                confidence=95,
                reason="Explicit direct download mode requested.",
                predicted_content_type="file",
            )

        if ct and ct not in {"text/html", "application/xhtml+xml"}:
            if ct in {"application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml"}:
                return MatchScore(
                    engine_id=self.id,
                    confidence=25,
                    reason="Stream manifest better handled by segmented stream engine.",
                    predicted_content_type="video",
                )
            size = ctx.head_content_length or 0
            # Large direct files (>= threshold) get higher score on aria2 when installed
            conf = 96 if size < self._settings.direct_aria2_threshold_bytes else 78
            return MatchScore(
                engine_id=self.id,
                confidence=conf,
                reason=f"Direct non-HTML Content-Type '{ct}' detected via HEAD.",
                predicted_content_type=_classify_mime(ct, ctx.path).value,
            )

        if ext in DIRECT_FILE_EXTENSIONS:
            kind, _ = DIRECT_FILE_EXTENSIONS[ext]
            return MatchScore(
                engine_id=self.id,
                confidence=94,
                reason=f"Direct file extension '{ext}' matched.",
                predicted_content_type=kind.value,
            )

        if ct in {"text/html", "application/xhtml+xml"}:
            return MatchScore(
                engine_id=self.id,
                confidence=35,
                reason="Static HTML webpage metadata fallback.",
                predicted_content_type="webpage",
            )

        return MatchScore(
            engine_id=self.id,
            confidence=20,
            reason="Generic HTTP fallback.",
            predicted_content_type="unknown",
        )

    async def head_inspect(self, ctx: UrlContext, timeout: float = 10.0) -> None:
        """Perform a cheap HEAD (falling back to 1-byte Range GET) to enrich UrlContext."""
        async with self._build_client(timeout) as client:
            try:
                resp = await client.head(ctx.url)
                if resp.status_code in {405, 501}:
                    resp = await client.get(ctx.url, headers={"Range": "bytes=0-1023"})
            except httpx.HTTPError:
                return

            # Validate final redirected URL against SSRF policy as defense-in-depth
            final_url = str(resp.url)
            try:
                validate_and_canonicalize_url(
                    final_url,
                    allow_private=self._settings.allow_private_addresses,
                    max_length=self._settings.max_url_length,
                )
            except UrlValidationError:
                return

            ctx.head_status = resp.status_code
            ctx.head_final_url = final_url
            ctx.head_content_type = resp.headers.get("content-type")
            cl = resp.headers.get("content-length")
            if cl and cl.isdigit():
                ctx.head_content_length = int(cl)
            ctx.head_filename = _parse_content_disposition_filename(
                resp.headers.get("content-disposition")
            )

    async def probe(
        self,
        ctx: UrlContext,
        options: ProbeOptions,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> NormalizedMediaOutput:
        t0 = time.monotonic()
        warnings: list[WarningInfo] = []

        async with self._build_client(float(options.timeout_seconds)) as client:
            try:
                async with client.stream("GET", ctx.url) as resp:
                    if resp.status_code == 404 or resp.status_code == 410:
                        raise AdapterError(
                            ErrorCode.ERR_NOT_FOUND,
                            f"Remote server returned HTTP {resp.status_code}.",
                            engine_id=self.id,
                        )
                    if resp.status_code in {401, 403}:
                        raise AdapterError(
                            ErrorCode.ERR_AUTH_REQUIRED,
                            f"Remote server denied access (HTTP {resp.status_code}).",
                            engine_id=self.id,
                        )
                    if resp.status_code == 429:
                        raise AdapterError(
                            ErrorCode.ERR_RATE_LIMITED,
                            "Remote server returned HTTP 429 Too Many Requests.",
                            engine_id=self.id,
                            retryable=True,
                        )
                    if resp.status_code >= 400:
                        raise AdapterError(
                            ErrorCode.ERR_INTERNAL,
                            f"Remote server returned HTTP {resp.status_code}.",
                            engine_id=self.id,
                            retryable=resp.status_code >= 500,
                        )

                    final_url = str(resp.url)
                    validate_and_canonicalize_url(
                        final_url,
                        allow_private=self._settings.allow_private_addresses,
                        max_length=self._settings.max_url_length,
                    )

                    ct_raw = resp.headers.get("content-type", "")
                    mime = ct_raw.split(";")[0].strip().lower() or "application/octet-stream"
                    cl = resp.headers.get("content-length")
                    size_bytes = int(cl) if (cl and cl.isdigit()) else None
                    filename = _parse_content_disposition_filename(
                        resp.headers.get("content-disposition")
                    ) or sanitize_filename(Path(urlsplit(final_url).path).name or ctx.domain)

                    content_type = _classify_mime(mime, final_url)
                    title = filename
                    description: str | None = None
                    thumbnail: ThumbnailInfo | None = None
                    author: AuthorInfo | None = None
                    formats: list[FormatInfo] = []
                    extra_meta: dict = {"http_status": resp.status_code}

                    if content_type == ContentType.WEBPAGE:
                        buf = bytearray()
                        async for chunk in resp.aiter_bytes(chunk_size=16384):
                            buf.extend(chunk)
                            if len(buf) >= _MAX_HTML_BYTES:
                                break
                        html = buf.decode(resp.encoding or "utf-8", errors="replace")
                        parsed = _extract_html_metadata(html, final_url)
                        title = parsed["title"] or title
                        description = parsed["description"]
                        if parsed["thumbnail_url"]:
                            thumbnail = ThumbnailInfo(url=parsed["thumbnail_url"])
                        if parsed["author"]:
                            author = AuthorInfo(name=parsed["author"])
                        if parsed["canonical_url"]:
                            extra_meta["canonical_url"] = parsed["canonical_url"]
                        extra_meta["json_ld_count"] = parsed["json_ld_count"]

                        for idx, (media_url, kind) in enumerate(parsed["embedded_media"], start=1):
                            ext = Path(urlsplit(media_url).path).suffix.lstrip(".").lower() or (
                                "mp4" if kind == "video" else "mp3"
                            )
                            guessed_mime = mimetypes.guess_type(media_url)[0] or f"{kind}/{ext}"
                            proto = (
                                ProtocolType.HLS
                                if ext == "m3u8"
                                else ProtocolType.DASH
                                if ext == "mpd"
                                else ProtocolType.HTTPS
                                if media_url.startswith("https://")
                                else ProtocolType.HTTP
                            )
                            formats.append(
                                FormatInfo(
                                    format_id=f"embedded-{idx}",
                                    label=f"Embedded {kind} ({ext.upper()})",
                                    container=ext,
                                    mime_type=guessed_mime,
                                    protocol=proto,
                                    has_video=(kind == "video"),
                                    has_audio=True,
                                    direct_media_url=media_url,
                                )
                            )
                        if formats:
                            content_type = (
                                ContentType.VIDEO
                                if any(f.has_video for f in formats)
                                else ContentType.AUDIO
                            )
                    else:
                        # Peek first 64 bytes for magic-byte validation without downloading full file
                        peek = b""
                        async for chunk in resp.aiter_bytes(chunk_size=64):
                            peek = chunk[:64]
                            break
                        magic_mime, is_exec = detect_magic_mime(peek)
                        if is_exec:
                            warnings.append(
                                WarningInfo(
                                    code="WARN_EXECUTABLE_DETECTED",
                                    message=f"Remote file appears to be an executable ({magic_mime}).",
                                    engine=self.id,
                                )
                            )
                        effective_mime = magic_mime or mime
                        ext = (
                            Path(urlsplit(final_url).path).suffix.lstrip(".").lower()
                            or (mimetypes.guess_extension(effective_mime) or ".bin").lstrip(".")
                        )
                        proto = (
                            ProtocolType.HTTPS
                            if final_url.startswith("https://")
                            else ProtocolType.HTTP
                        )
                        formats.append(
                            FormatInfo(
                                format_id="direct",
                                label=f"Direct {ext.upper()}",
                                container=ext,
                                mime_type=effective_mime,
                                size_bytes=size_bytes,
                                protocol=proto,
                                has_video=(content_type == ContentType.VIDEO),
                                has_audio=(content_type in {ContentType.VIDEO, ContentType.AUDIO}),
                                direct_media_url=final_url,
                            )
                        )
                        if content_type == ContentType.IMAGE:
                            thumbnail = ThumbnailInfo(url=final_url, mime_type=effective_mime)

            except AdapterError:
                raise
            except httpx.TimeoutException as exc:
                raise AdapterError(
                    ErrorCode.ERR_TIMEOUT,
                    f"Direct HTTP probe timed out: {exc}",
                    engine_id=self.id,
                    retryable=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise AdapterError(
                    ErrorCode.ERR_INTERNAL,
                    f"Direct HTTP request failed: {exc}",
                    engine_id=self.id,
                    retryable=True,
                ) from exc

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return NormalizedMediaOutput(
            job_id=ctx.job_id,
            original_url=ctx.validated.original_url,
            url=final_url,
            source=SourceInfo(domain=ctx.domain, platform="direct", extractor_key="DirectHttp"),
            content_type=content_type,
            title=title,
            description=description,
            thumbnail=thumbnail,
            size_bytes=size_bytes,
            author=author,
            available_formats=formats,
            metadata=MediaMetadata(extra=extra_meta),
            download=DownloadInfo(status=DownloadStatus.NOT_REQUESTED),
            engine=EngineProvenance(
                engine_used=self.id,
                engine_version=__version__,
                fallback_chain=[self.id],
                probe_duration_ms=elapsed_ms,
            ),
            warnings=warnings,
        )

    async def download(
        self,
        ctx: UrlContext,
        plan: DownloadPlan,
        *,
        progress_cb: ProgressCallback | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadArtifact:
        plan.output_dir.mkdir(parents=True, exist_ok=True)
        temp_file = ensure_safe_path(plan.output_dir, plan.output_dir / f"{uuid.uuid4().hex}.part")
        warnings: list[WarningInfo] = []

        async with self._build_client(float(plan.timeout_seconds)) as client:
            try:
                async with client.stream("GET", ctx.url) as resp:
                    if resp.status_code >= 400:
                        raise AdapterError(
                            ErrorCode.ERR_NOT_FOUND if resp.status_code == 404 else ErrorCode.ERR_INTERNAL,
                            f"Download failed with HTTP {resp.status_code}.",
                            engine_id=self.id,
                            retryable=resp.status_code in {429, 502, 503, 504},
                        )

                    final_url = str(resp.url)
                    validate_and_canonicalize_url(
                        final_url,
                        allow_private=self._settings.allow_private_addresses,
                        max_length=self._settings.max_url_length,
                    )

                    cl = resp.headers.get("content-length")
                    total = int(cl) if (cl and cl.isdigit()) else None
                    if total is not None and total > plan.max_bytes:
                        raise AdapterError(
                            ErrorCode.ERR_QUOTA_EXCEEDED,
                            f"Remote file ({total} bytes) exceeds quota ({plan.max_bytes} bytes).",
                            engine_id=self.id,
                        )

                    mime = resp.headers.get("content-type", "").split(";")[0].strip().lower() or None
                    filename = _parse_content_disposition_filename(
                        resp.headers.get("content-disposition")
                    ) or sanitize_filename(Path(urlsplit(final_url).path).name or "download")

                    downloaded = 0
                    started = time.monotonic()
                    last_emit = 0.0

                    with temp_file.open("wb") as out:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            if cancel_token and cancel_token.is_cancelled:
                                raise AdapterError(
                                    ErrorCode.ERR_INTERNAL,
                                    "Download cancelled.",
                                    engine_id=self.id,
                                )
                            downloaded += len(chunk)
                            if downloaded > plan.max_bytes:
                                raise AdapterError(
                                    ErrorCode.ERR_QUOTA_EXCEEDED,
                                    f"Download exceeded max_bytes quota ({plan.max_bytes}).",
                                    engine_id=self.id,
                                )
                            out.write(chunk)

                            now = time.monotonic()
                            if progress_cb and (now - last_emit) >= 0.25:
                                last_emit = now
                                elapsed = max(0.001, now - started)
                                speed = downloaded / elapsed
                                pct = (downloaded * 100.0 / total) if total else None
                                eta = ((total - downloaded) / speed) if (total and speed > 0) else None
                                evt = ProgressEvent(
                                    job_id=plan.job_id,
                                    status=JobStatus.DOWNLOADING,
                                    stage="downloading",
                                    percent=round(pct, 2) if pct is not None else None,
                                    bytes_downloaded=downloaded,
                                    bytes_total=total,
                                    speed_bps=round(speed, 1),
                                    eta_seconds=round(eta, 1) if eta is not None else None,
                                    message=f"Downloading via {self.id}",
                                )
                                res = progress_cb(evt)
                                if hasattr(res, "__await__"):
                                    await res
            except BaseException:
                temp_file.unlink(missing_ok=True)
                raise

        magic_mime, is_exec = detect_magic_mime(temp_file)
        if is_exec:
            temp_file.unlink(missing_ok=True)
            raise AdapterError(
                ErrorCode.ERR_VALIDATION_FAILED,
                f"Blocked dangerous executable payload ({magic_mime}).",
                engine_id=self.id,
            )

        effective_mime = magic_mime or mime or "application/octet-stream"
        ext = Path(filename).suffix
        if not ext:
            ext = mimetypes.guess_extension(effective_mime) or ".bin"
            filename = f"{filename}{ext}"

        final_path = ensure_safe_path(plan.output_dir, plan.output_dir / f"{uuid.uuid4().hex}{ext}")
        temp_file.rename(final_path)
        return DownloadArtifact(
            local_path=final_path,
            filename=sanitize_filename(filename),
            mime_type=effective_mime,
            size_bytes=downloaded,
            warnings=warnings,
        )

    async def health_check(self) -> HealthReport:
        return HealthReport(
            engine_id=self.id,
            installation_status=InstallationStatus.INSTALLED,
            version=__version__,
            healthy=True,
            detail="In-process httpx + BeautifulSoup4 ready.",
        )
