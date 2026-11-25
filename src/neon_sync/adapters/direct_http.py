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
import zipfile
from email.message import Message
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from lxml import etree as lxml_etree

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
    MediaItem,
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
_MAX_HTML_BYTES = 2 * 1024 * 1024  # 2 MiB HTML/XML inspection cap
_FEED_MIMES = frozenset(
    {
        "application/rss+xml",
        "application/atom+xml",
        "application/xml",
        "text/xml",
    }
)
_FEED_EXTS = frozenset({".rss", ".atom", ".xml"})


def _parse_itunes_duration(raw: str | None) -> float | None:
    if not raw:
        return None
    s = raw.strip()
    if s.isdigit():
        return float(int(s))
    parts = s.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return None


def _parse_rss_or_atom_feed(xml_text: str, base_url: str, max_items: int = 50) -> dict | None:
    """Safely parse RSS 2.0 / Podcast / Atom XML feeds (XXE & network entity resolution disabled)."""
    if not xml_text or not xml_text.strip():
        return None
    try:
        parser = lxml_etree.XMLParser(resolve_entities=False, no_network=True, recover=True)
        root = lxml_etree.fromstring(xml_text.encode("utf-8", errors="replace"), parser=parser)
    except (lxml_etree.XMLSyntaxError, ValueError, TypeError):
        return None
    if root is None:
        return None

    tag_local = lxml_etree.QName(root).localname.lower() if isinstance(root.tag, str) else ""
    if tag_local not in {"rss", "feed", "rdf"}:
        return None

    def _first_text(el, names: tuple[str, ...]) -> str | None:
        for child in el:
            if not isinstance(child.tag, str):
                continue
            loc = lxml_etree.QName(child).localname.lower()
            if loc in names and child.text and child.text.strip():
                return child.text.strip()
        return None

    items: list[MediaItem] = []
    feed_title: str | None = None
    feed_desc: str | None = None
    feed_author: str | None = None
    feed_thumb: str | None = None
    has_audio_enclosures = False

    if tag_local in {"rss", "rdf"}:
        channel = next(
            (c for c in root if isinstance(c.tag, str) and lxml_etree.QName(c).localname.lower() == "channel"),
            root,
        )
        feed_title = _first_text(channel, ("title",))
        feed_desc = _first_text(channel, ("description", "subtitle"))
        feed_author = _first_text(channel, ("author", "creator", "managingeditor"))
        for child in channel:
            if not isinstance(child.tag, str):
                continue
            loc = lxml_etree.QName(child).localname.lower()
            if loc == "image":
                href = child.get("href")
                if href:
                    feed_thumb = urljoin(base_url, href)
                else:
                    u = _first_text(child, ("url",))
                    if u:
                        feed_thumb = urljoin(base_url, u)

        raw_items = [
            el for el in channel if isinstance(el.tag, str) and lxml_etree.QName(el).localname.lower() == "item"
        ]
        if not raw_items and tag_local == "rdf":
            raw_items = [
                el for el in root if isinstance(el.tag, str) and lxml_etree.QName(el).localname.lower() == "item"
            ]

        for idx, item_el in enumerate(raw_items[:max_items], start=1):
            it_title = _first_text(item_el, ("title",)) or f"Episode {idx}"
            it_link = _first_text(item_el, ("link",))
            it_dur = _parse_itunes_duration(_first_text(item_el, ("duration",)))
            it_guid = _first_text(item_el, ("guid",))
            it_thumb = feed_thumb
            enc_url: str | None = None
            enc_mime: str | None = None
            enc_size: int | None = None

            for ch in item_el:
                if not isinstance(ch.tag, str):
                    continue
                loc = lxml_etree.QName(ch).localname.lower()
                if (loc == "enclosure" and ch.get("url")) or (
                    loc == "content" and ch.get("url") and not enc_url
                ):
                    enc_url = urljoin(base_url, str(ch.get("url")))
                    enc_mime = str(ch.get("type") or "") or None
                    length = ch.get("length") or ch.get("fileSize")
                    enc_size = int(length) if length and length.isdigit() else None
                elif loc in {"image", "thumbnail"} and ch.get("href"):
                    it_thumb = urljoin(base_url, str(ch.get("href")))
                elif loc == "thumbnail" and ch.get("url"):
                    it_thumb = urljoin(base_url, str(ch.get("url")))

            target_url = enc_url or (urljoin(base_url, it_link) if it_link else base_url)
            mime_low = (enc_mime or "").lower()
            if mime_low.startswith("audio/") or (enc_url and Path(urlsplit(enc_url).path).suffix.lower() in {".mp3", ".m4a", ".ogg", ".opus", ".wav"}):
                kind = "audio"
                has_audio_enclosures = True
            elif mime_low.startswith("video/"):
                kind = "video"
            else:
                kind = "webpage"

            items.append(
                MediaItem(
                    index=idx,
                    external_id=it_guid[:64] if it_guid else f"ep_{idx}",
                    url=target_url,
                    title=it_title,
                    content_type=kind,
                    mime_type=enc_mime,
                    duration_seconds=it_dur,
                    size_bytes=enc_size,
                    thumbnail_url=it_thumb,
                    direct_media_url=enc_url,
                )
            )
    else:
        # Atom <feed>
        feed_title = _first_text(root, ("title",))
        feed_desc = _first_text(root, ("subtitle",))
        for ch in root:
            if not isinstance(ch.tag, str):
                continue
            loc = lxml_etree.QName(ch).localname.lower()
            if loc == "author":
                feed_author = _first_text(ch, ("name",)) or feed_author
            elif loc in {"logo", "icon"} and ch.text and ch.text.strip():
                feed_thumb = urljoin(base_url, ch.text.strip())

        entries = [
            el for el in root if isinstance(el.tag, str) and lxml_etree.QName(el).localname.lower() == "entry"
        ]
        for idx, entry in enumerate(entries[:max_items], start=1):
            it_title = _first_text(entry, ("title",)) or f"Entry {idx}"
            it_id = _first_text(entry, ("id",))
            enc_url = None
            enc_mime = None
            enc_size = None
            alt_url = None
            for ch in entry:
                if not isinstance(ch.tag, str):
                    continue
                if lxml_etree.QName(ch).localname.lower() == "link" and ch.get("href"):
                    rel = (ch.get("rel") or "alternate").lower()
                    href = urljoin(base_url, str(ch.get("href")))
                    if rel == "enclosure":
                        enc_url = href
                        enc_mime = ch.get("type")
                        length = ch.get("length")
                        enc_size = int(length) if length and length.isdigit() else None
                    elif not alt_url:
                        alt_url = href
            target_url = enc_url or alt_url or base_url
            is_audio = bool(enc_mime and enc_mime.lower().startswith("audio/"))
            if is_audio:
                has_audio_enclosures = True
            items.append(
                MediaItem(
                    index=idx,
                    external_id=it_id[:64] if it_id else f"entry_{idx}",
                    url=target_url,
                    title=it_title,
                    content_type="audio" if is_audio else "webpage",
                    mime_type=enc_mime,
                    size_bytes=enc_size,
                    thumbnail_url=feed_thumb,
                    direct_media_url=enc_url,
                )
            )

    return {
        "title": feed_title or "RSS Feed",
        "description": feed_desc,
        "author": feed_author,
        "thumbnail_url": feed_thumb,
        "feed_type": "podcast" if has_audio_enclosures else "rss",
        "items": items,
    }


def _extract_article_text(html: str, *, fmt: str = "markdown", url: str | None = None) -> str:
    """Extract clean article Markdown or plain text via trafilatura with BeautifulSoup fallback."""
    try:
        import trafilatura

        out_fmt = "markdown" if fmt == "markdown" else "txt"
        extracted = trafilatura.extract(
            html,
            url=url,
            output_format=out_fmt,
            include_links=(fmt == "markdown"),
            include_formatting=(fmt == "markdown"),
            include_tables=True,
        )
        if extracted and extracted.strip():
            return extracted.strip()
    except (ImportError, ValueError, TypeError, RuntimeError):
        extracted = None

    soup = BeautifulSoup(html, "lxml")
    for junk in soup(["script", "style", "noscript", "nav", "footer", "header", "aside"]):
        junk.decompose()
    blocks: list[str] = []
    for el in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if not txt:
            continue
        if fmt == "markdown" and el.name in {"h1", "h2", "h3"}:
            level = int(el.name[1])
            blocks.append(f"{'#' * level} {txt}")
        elif fmt == "markdown" and el.name == "li":
            blocks.append(f"- {txt}")
        else:
            blocks.append(txt)
    return "\n\n".join(blocks).strip()


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

    # Inspect inline <svg> logos & valid page <img> assets (strip any <script> tags from SVGs)
    svg_strings: list[str] = []
    for svg in soup.find_all("svg"):
        for sc in svg.find_all("script"):
            sc.decompose()
        raw_svg = str(svg)
        if 30 <= len(raw_svg) <= 250_000:
            if "xmlns=" not in raw_svg[:120]:
                raw_svg = raw_svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
            svg_strings.append(raw_svg)
        if len(svg_strings) >= 40:
            break

    page_images: list[str] = []
    seen_imgs: set[str] = set()
    if og_image:
        og_abs = urljoin(base_url, og_image)
        seen_imgs.add(og_abs)
        page_images.append(og_abs)
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src:
            continue
        s = str(src).strip()
        if not s or s.startswith(("data:", "blob:", "javascript:")):
            continue
        # Skip 1x1 tracking pixels when dimensions are explicit
        w_attr = str(img.get("width") or "").strip()
        h_attr = str(img.get("height") or "").strip()
        if w_attr in {"0", "1"} or h_attr in {"0", "1"}:
            continue
        abs_img = urljoin(base_url, s)
        if abs_img not in seen_imgs:
            seen_imgs.add(abs_img)
            page_images.append(abs_img)
        if len(page_images) >= 32:
            break

    # Deduplicate embedded media preserving order
    seen: set[str] = set()
    unique_media: list[tuple[str, str]] = []
    for u, kind in embedded_media:
        if u not in seen:
            seen.add(u)
            unique_media.append((u, kind))

    article_preview = _extract_article_text(html, fmt="txt", url=base_url)
    word_count = len(article_preview.split()) if article_preview else 0
    preview_svgs = [s for s in svg_strings if len(s) <= 24_000][:8]

    return {
        "title": title,
        "description": description,
        "thumbnail_url": urljoin(base_url, og_image) if og_image else None,
        "author": author,
        "canonical_url": canonical_url,
        "embedded_media": unique_media,
        "json_ld_count": len(json_ld_items),
        "svg_strings": svg_strings,
        "page_images": page_images,
        "webpage_stats": {
            "word_count": word_count,
            "reading_minutes": max(1, round(word_count / 220)) if word_count >= 35 else 0,
            "has_article": word_count >= 35,
            "excerpt": article_preview[:750] if article_preview else "",
            "image_count": len(page_images),
            "svg_count": len(svg_strings),
        },
        "webpage_assets_preview": {
            "svgs": preview_svgs,
            "images": page_images[:12],
        },
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

        if ctx.mode in {"direct", "webpage"}:
            return MatchScore(
                engine_id=self.id,
                confidence=96,
                reason=f"Explicit {ctx.mode} extraction mode requested.",
                predicted_content_type="webpage" if ctx.mode == "webpage" else "file",
            )

        if ct in _FEED_MIMES or ext in _FEED_EXTS:
            return MatchScore(
                engine_id=self.id,
                confidence=96,
                reason="Podcast / RSS / Atom feed detected.",
                predicted_content_type="playlist",
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
                    is_feed = mime in _FEED_MIMES or Path(urlsplit(final_url).path).suffix.lower() in _FEED_EXTS
                    title = filename
                    description: str | None = None
                    thumbnail: ThumbnailInfo | None = None
                    author: AuthorInfo | None = None
                    formats: list[FormatInfo] = []
                    items: list[MediaItem] = []
                    extra_meta: dict = {"http_status": resp.status_code}

                    if is_feed:
                        buf = bytearray()
                        async for chunk in resp.aiter_bytes(chunk_size=16384):
                            buf.extend(chunk)
                            if len(buf) >= _MAX_HTML_BYTES:
                                break
                        xml_text = buf.decode(resp.encoding or "utf-8", errors="replace")
                        feed = _parse_rss_or_atom_feed(xml_text, final_url, max_items=options.max_items)
                        if feed is not None:
                            content_type = ContentType.PLAYLIST
                            title = feed["title"] or title
                            description = feed["description"]
                            if feed["thumbnail_url"]:
                                thumbnail = ThumbnailInfo(url=feed["thumbnail_url"])
                            if feed["author"]:
                                author = AuthorInfo(name=feed["author"])
                            items = feed["items"]
                            extra_meta["feed_type"] = feed["feed_type"]
                            first_audio = next((it for it in items if it.direct_media_url), None)
                            if first_audio and first_audio.direct_media_url:
                                fext = Path(urlsplit(first_audio.direct_media_url).path).suffix.lstrip(".").lower() or "mp3"
                                formats.append(
                                    FormatInfo(
                                        format_id="latest-episode",
                                        label=f"Latest Episode ({fext.upper()})",
                                        container=fext,
                                        mime_type=first_audio.mime_type or f"audio/{fext}",
                                        protocol=(
                                            ProtocolType.HTTPS
                                            if first_audio.direct_media_url.startswith("https://")
                                            else ProtocolType.HTTP
                                        ),
                                        has_video=(first_audio.content_type == "video"),
                                        has_audio=True,
                                        size_bytes=first_audio.size_bytes,
                                        direct_media_url=first_audio.direct_media_url,
                                    )
                                )
                        else:
                            is_feed = False

                    if not is_feed and content_type == ContentType.WEBPAGE:
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
                        extra_meta["webpage_stats"] = parsed["webpage_stats"]
                        extra_meta["webpage_assets_preview"] = parsed["webpage_assets_preview"]

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
                    elif not is_feed:
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
            items=items,
            metadata=MediaMetadata(
                item_count_total=len(items) if items else 1,
                item_count_returned=len(items) if items else 1,
                extra=extra_meta,
            ),
            download=DownloadInfo(status=DownloadStatus.NOT_REQUESTED),
            engine=EngineProvenance(
                engine_used=self.id,
                engine_version=__version__,
                fallback_chain=[self.id],
                probe_duration_ms=elapsed_ms,
            ),
            warnings=warnings,
        )

    async def _download_webpage_transform(
        self,
        ctx: UrlContext,
        plan: DownloadPlan,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadArtifact:
        """Extract clean Article Markdown/Text, RSS Feed Index, or Page SVG/Image Asset Pack."""
        fmt = plan.webpage_format or "markdown"
        async with self._build_client(float(plan.timeout_seconds)) as client:
            resp = await client.get(ctx.url)
            if resp.status_code >= 400:
                raise AdapterError(
                    ErrorCode.ERR_NOT_FOUND if resp.status_code == 404 else ErrorCode.ERR_INTERNAL,
                    f"Webpage fetch failed with HTTP {resp.status_code}.",
                    engine_id=self.id,
                    retryable=resp.status_code in {429, 502, 503, 504},
                )
            final_url = str(resp.url)
            validate_and_canonicalize_url(
                final_url,
                allow_private=self._settings.allow_private_addresses,
                max_length=self._settings.max_url_length,
            )
            raw_text = resp.content[:_MAX_HTML_BYTES].decode(resp.encoding or "utf-8", errors="replace")
            mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()

            # Check if this is an RSS / Podcast feed -> export crisp episode Markdown index
            if mime in _FEED_MIMES or Path(urlsplit(final_url).path).suffix.lower() in _FEED_EXTS:
                feed = _parse_rss_or_atom_feed(raw_text, final_url, max_items=plan.max_items)
                if feed is not None:
                    lines = [f"# {feed['title']}", "", f"> Source: {final_url}"]
                    if feed.get("author"):
                        lines.append(f"> Author: {feed['author']}")
                    if feed.get("description"):
                        lines.extend(["", feed["description"]])
                    lines.extend(["", "## Episodes / Entries", ""])
                    for it in feed["items"]:
                        dur_str = f" ({int(it.duration_seconds // 60)}m)" if it.duration_seconds else ""
                        lines.append(f"- **{it.index}. {it.title}**{dur_str} — {it.url}")
                    body = "\n".join(lines).strip() + "\n"
                    stem = sanitize_filename(feed["title"], fallback="feed_index", max_len=80)
                    out_path = ensure_safe_path(plan.output_dir, plan.output_dir / f"{stem}.md")
                    out_path.write_text(body, encoding="utf-8")
                    return DownloadArtifact(
                        local_path=out_path,
                        filename=out_path.name,
                        mime_type="text/markdown; charset=utf-8",
                        size_bytes=out_path.stat().st_size,
                    )

            parsed = _extract_html_metadata(raw_text, final_url)
            title = parsed["title"] or ctx.domain
            stem = sanitize_filename(title, fallback="article", max_len=80)

            if fmt in {"markdown", "text"}:
                extracted = _extract_article_text(raw_text, fmt=fmt, url=final_url)
                if not extracted:
                    raise AdapterError(
                        ErrorCode.ERR_NOT_FOUND,
                        "No readable article content was found on this webpage.",
                        engine_id=self.id,
                    )
                if fmt == "markdown":
                    header = [f"# {title}", "", f"> Source: {final_url}"]
                    if parsed.get("author"):
                        header.append(f"> Author: {parsed['author']}")
                    header.extend(["", "---", ""])
                    body = "\n".join(header) + extracted + "\n"
                    out_path = ensure_safe_path(plan.output_dir, plan.output_dir / f"{stem}.md")
                    out_path.write_text(body, encoding="utf-8")
                    return DownloadArtifact(
                        local_path=out_path,
                        filename=out_path.name,
                        mime_type="text/markdown; charset=utf-8",
                        size_bytes=out_path.stat().st_size,
                    )
                body = f"{title}\nSource: {final_url}\n\n{extracted}\n"
                out_path = ensure_safe_path(plan.output_dir, plan.output_dir / f"{stem}.txt")
                out_path.write_text(body, encoding="utf-8")
                return DownloadArtifact(
                    local_path=out_path,
                    filename=out_path.name,
                    mime_type="text/plain; charset=utf-8",
                    size_bytes=out_path.stat().st_size,
                )

            if fmt == "assets_zip":
                zip_path = ensure_safe_path(plan.output_dir, plan.output_dir / f"{stem}_assets.zip")
                added = 0
                seen_names: set[str] = set()
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for idx, svg_code in enumerate(parsed["svg_strings"], start=1):
                        name = f"svgs/vector_{idx:02d}.svg"
                        zf.writestr(name, svg_code.encode("utf-8"))
                        added += 1

                    for idx, img_url in enumerate(parsed["page_images"][:24], start=1):
                        if cancel_token and cancel_token.is_cancelled:
                            break
                        try:
                            validate_and_canonicalize_url(
                                img_url,
                                allow_private=self._settings.allow_private_addresses,
                                max_length=self._settings.max_url_length,
                            )
                            ir = await client.get(img_url, timeout=10.0)
                            if ir.status_code != 200 or not ir.content or len(ir.content) > 10 * 1024 * 1024:
                                continue
                            magic_mime, is_exec = detect_magic_mime(ir.content[:64])
                            if is_exec:
                                continue
                            ct_img = (magic_mime or ir.headers.get("content-type", "")).split(";")[0].strip().lower()
                            if not ct_img.startswith("image/"):
                                continue
                            ext = (mimetypes.guess_extension(ct_img) or Path(urlsplit(img_url).path).suffix or ".jpg")
                            if ext == ".jpe":
                                ext = ".jpg"
                            raw_name = sanitize_filename(Path(urlsplit(img_url).path).name or f"image_{idx:02d}{ext}", fallback=f"image_{idx:02d}{ext}")
                            if not Path(raw_name).suffix:
                                raw_name = f"{raw_name}{ext}"
                            arc = f"images/{raw_name}"
                            if arc in seen_names:
                                arc = f"images/{idx:02d}_{raw_name}"
                            seen_names.add(arc)
                            zf.writestr(arc, ir.content)
                            added += 1
                        except (UrlValidationError, httpx.HTTPError, OSError):
                            continue

                if added == 0:
                    zip_path.unlink(missing_ok=True)
                    raise AdapterError(
                        ErrorCode.ERR_NOT_FOUND,
                        "No downloadable SVG or image assets were found on this webpage.",
                        engine_id=self.id,
                    )
                return DownloadArtifact(
                    local_path=zip_path,
                    filename=zip_path.name,
                    mime_type="application/zip",
                    size_bytes=zip_path.stat().st_size,
                )

        raise AdapterError(
            ErrorCode.ERR_UNSUPPORTED_URL,
            f"Unsupported webpage extraction format '{fmt}'.",
            engine_id=self.id,
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
        if plan.webpage_format in {"markdown", "text", "assets_zip"}:
            return await self._download_webpage_transform(ctx, plan, cancel_token=cancel_token)
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
                    if mime in {"text/html", "application/xhtml+xml"} and plan.mode in {
                        "video",
                        "audio",
                        "image",
                    }:
                        raise AdapterError(
                            ErrorCode.ERR_UNSUPPORTED_URL,
                            "URL returned an HTML webpage instead of a direct media file.",
                            engine_id=self.id,
                        )
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
