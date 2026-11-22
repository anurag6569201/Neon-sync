"""Neon Sync Live Network Stream Resolver & SSRF-Safe Range/HLS Proxy (VLC-Style Watch Live)."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote, urljoin

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from neon_sync.adapters import AdapterError
from neon_sync.models import ContentType, ErrorCode, FormatInfo, ProbeRequest, ProtocolType
from neon_sync.pipeline import RateLimitExceededError, convert_json3_to_vtt
from neon_sync.security import (
    UrlValidationError,
    build_content_disposition,
    sanitize_filename,
    validate_and_canonicalize_url,
)

from .branding import brand_error_detail, sanitize_public_text

if TYPE_CHECKING:
    from .app import AppContainer

stream_router = APIRouter(prefix="/stream", tags=["streaming"])

_STREAM_TTL_SECONDS = 2 * 60 * 60  # 2 hours
_MAX_REGISTERED_STREAMS = 500
_BROWSER_VIDEO_CONTAINERS = frozenset({"mp4", "webm", "mov", "m4v", "ogv", "m3u8"})
_BROWSER_AUDIO_CONTAINERS = frozenset({"m4a", "mp3", "opus", "webm", "ogg", "aac", "wav", "flac"})
_HLS_URI_ATTR_RE = re.compile(r'URI="([^"]+)"')


class StreamResolveRequest(BaseModel):
    url: str = Field(min_length=4, max_length=4096)
    mode: Literal["video", "audio"] = "video"
    preferred_quality: Literal["auto", "1080", "720", "480", "360"] = "auto"


class PlayableTrack(BaseModel):
    stream_id: str
    label: str
    kind: Literal["video", "audio"]
    protocol: Literal["progressive", "hls"]
    container: str
    mime_type: str
    resolution: str | None = None
    height: int | None = None
    fps: float | None = None
    size_bytes: int | None = None
    stream_url: str
    m3u_url: str


class PlayableSubtitle(BaseModel):
    sub_id: str
    language: str
    name: str | None = None
    is_auto_generated: bool = False
    url: str


class PlayableChapter(BaseModel):
    title: str
    start_seconds: float
    end_seconds: float


class StreamResolveResponse(BaseModel):
    service: str = "neon-sync"
    original_url: str
    title: str
    author: str | None = None
    thumbnail_url: str | None = None
    source_domain: str
    content_type: str
    duration_seconds: float | None = None
    is_live: bool = False
    default_stream_id: str
    tracks: list[PlayableTrack]
    subtitles: list[PlayableSubtitle] = Field(default_factory=list)
    chapters: list[PlayableChapter] = Field(default_factory=list)


@dataclass
class RegisteredStream:
    stream_id: str
    target_url: str
    mime_type: str
    is_hls: bool
    title: str
    referer: str | None = None
    expires_at: float = field(default_factory=lambda: time.monotonic() + _STREAM_TTL_SECONDS)


@dataclass
class RegisteredSubtitle:
    sub_id: str
    target_url: str
    language: str
    expires_at: float = field(default_factory=lambda: time.monotonic() + _STREAM_TTL_SECONDS)


class LiveStreamRegistry:
    """Bounded, server-issued registry of SSRF-validated live stream & subtitle tokens."""

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)
        self._streams: dict[str, RegisteredStream] = {}
        self._subs: dict[str, RegisteredSubtitle] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        expired_s = [k for k, v in self._streams.items() if v.expires_at < now]
        for k in expired_s:
            self._streams.pop(k, None)
        expired_sub = [k for k, v in self._subs.items() if v.expires_at < now]
        for k in expired_sub:
            self._subs.pop(k, None)
        while len(self._streams) > _MAX_REGISTERED_STREAMS:
            oldest = next(iter(self._streams))
            self._streams.pop(oldest, None)

    def register_stream(
        self,
        *,
        target_url: str,
        mime_type: str,
        is_hls: bool,
        title: str,
        referer: str | None = None,
    ) -> RegisteredStream:
        self._prune()
        sid = f"ns_str_{uuid.uuid4().hex[:16]}"
        entry = RegisteredStream(
            stream_id=sid,
            target_url=target_url,
            mime_type=mime_type,
            is_hls=is_hls,
            title=title,
            referer=referer,
        )
        self._streams[sid] = entry
        return entry

    def get_stream(self, stream_id: str) -> RegisteredStream | None:
        entry = self._streams.get(stream_id)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            self._streams.pop(stream_id, None)
            return None
        return entry

    def register_subtitle(self, *, target_url: str, language: str) -> RegisteredSubtitle:
        self._prune()
        sub_id = f"ns_sub_{uuid.uuid4().hex[:14]}"
        entry = RegisteredSubtitle(sub_id=sub_id, target_url=target_url, language=language)
        self._subs[sub_id] = entry
        return entry

    def get_subtitle(self, sub_id: str) -> RegisteredSubtitle | None:
        entry = self._subs.get(sub_id)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            self._subs.pop(sub_id, None)
            return None
        return entry

    def sign_hls_asset(self, stream_id: str, asset_url: str) -> str:
        msg = f"{stream_id}:{asset_url}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:24]

    def verify_hls_asset(self, stream_id: str, asset_url: str, sig: str) -> bool:
        expected = self.sign_hls_asset(stream_id, asset_url)
        return hmac.compare_digest(expected, sig)


def _container(request: Request) -> AppContainer:
    return request.app.state.container


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _request_origin(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _is_hls_format(fmt: FormatInfo) -> bool:
    url = (fmt.direct_media_url or "").lower()
    mime = (fmt.mime_type or "").lower()
    return (
        fmt.protocol == ProtocolType.HLS
        or ".m3u8" in url
        or "mpegurl" in mime
        or fmt.container.lower() == "m3u8"
    )


def _format_label(fmt: FormatInfo, kind: Literal["video", "audio"], is_hls: bool) -> str:
    if kind == "audio":
        abr = f" {int(fmt.audio_bitrate_kbps)}k" if fmt.audio_bitrate_kbps else ""
        return f"Audio · {fmt.container.upper()}{abr}"
    res = f"{fmt.height}p" if fmt.height else (fmt.label or ("HLS Live" if is_hls else "Stream"))
    fps = f" {int(fmt.fps)}fps" if fmt.fps and fmt.fps >= 48 else ""
    proto = " HLS" if is_hls else f" {fmt.container.upper()}"
    return f"{res}{fps}{proto}".strip()


def _select_playable_formats(
    formats: list[FormatInfo],
    *,
    content_type: ContentType,
) -> list[tuple[FormatInfo, Literal["video", "audio"], bool]]:
    """Extract browser-playable & VLC-playable muxed video, HLS, and audio streams."""
    muxed_video: list[tuple[FormatInfo, Literal["video", "audio"], bool]] = []
    hls_video: list[tuple[FormatInfo, Literal["video", "audio"], bool]] = []
    audio_tracks: list[tuple[FormatInfo, Literal["video", "audio"], bool]] = []

    seen_urls: set[str] = set()
    for f in formats:
        if not f.direct_media_url or f.direct_media_url in seen_urls:
            continue
        if f.protocol not in {ProtocolType.HTTPS, ProtocolType.HTTP, ProtocolType.HLS}:
            continue
        is_hls = _is_hls_format(f)
        mime = (f.mime_type or "").lower()
        ext = (f.container or "").lower()

        # Direct HTTP video/audio where adapter didn't set has_video/has_audio flags
        inferred_video = f.has_video or mime.startswith("video/") or content_type in {ContentType.VIDEO, ContentType.LIVESTREAM}
        inferred_audio = f.has_audio or mime.startswith("audio/") or content_type == ContentType.AUDIO

        if is_hls and (f.has_video or not (f.has_audio and not f.has_video)):
            seen_urls.add(f.direct_media_url)
            hls_video.append((f, "video", True))
        elif (
            (f.has_video and f.has_audio and (ext in _BROWSER_VIDEO_CONTAINERS or mime.startswith("video/")))
            or ((not f.has_video and not f.has_audio) and mime.startswith("video/"))
        ):
            seen_urls.add(f.direct_media_url)
            muxed_video.append((f, "video", False))
        elif (
            (f.has_audio and not f.has_video and (ext in _BROWSER_AUDIO_CONTAINERS or mime.startswith("audio/")))
            or ((not f.has_video and not f.has_audio) and inferred_audio and not inferred_video)
        ):
            seen_urls.add(f.direct_media_url)
            audio_tracks.append((f, "audio", False))

    # Prefer MP4/WebM progressive with highest resolution first, then HLS (great for 1080p/live!), then audio
    muxed_video.sort(
        key=lambda item: (
            item[0].height or 0,
            1 if item[0].container.lower() == "mp4" else 0,
            item[0].fps or 0.0,
        ),
        reverse=True,
    )
    hls_video.sort(key=lambda item: (item[0].height or 0, item[0].fps or 0.0), reverse=True)
    audio_tracks.sort(
        key=lambda item: (
            1 if item[0].container.lower() in {"m4a", "mp3"} else 0,
            item[0].audio_bitrate_kbps or 0.0,
        ),
        reverse=True,
    )

    # Deduplicate by height for video tracks so the quality selector stays clean & crisp
    picked: list[tuple[FormatInfo, Literal["video", "audio"], bool]] = []
    seen_heights: set[tuple[int | None, bool]] = set()
    for item in [*muxed_video, *hls_video]:
        key = (item[0].height, item[2])
        if key in seen_heights and len(picked) >= 4:
            continue
        seen_heights.add(key)
        picked.append(item)
        if len(picked) >= 6:
            break

    picked.extend(audio_tracks[:2])
    return picked


def _rewrite_hls_manifest(
    manifest_text: str,
    *,
    base_url: str,
    stream_id: str,
    registry: LiveStreamRegistry,
) -> str:
    """Rewrite HLS master/media playlist URIs to signed Neon Sync proxy URLs."""
    def _proxy_for(raw_uri: str) -> str:
        abs_url = urljoin(base_url, raw_uri.strip())
        sig = registry.sign_hls_asset(stream_id, abs_url)
        return f"/v1/stream/hls/{stream_id}/asset?u={quote(abs_url, safe='')}&sig={sig}"

    out_lines: list[str] = []
    for raw_line in manifest_text.splitlines():
        line = raw_line.strip()
        if not line:
            out_lines.append("")
        elif line.startswith("#"):
            # Rewrite URI="..." attributes (e.g. EXT-X-MEDIA audio/subs or EXT-X-MAP init segment)
            rewritten = _HLS_URI_ATTR_RE.sub(lambda m: f'URI="{_proxy_for(m.group(1))}"', line)
            out_lines.append(rewritten)
        else:
            out_lines.append(_proxy_for(line))
    return "\n".join(out_lines) + "\n"


@stream_router.post("/resolve", response_model=StreamResolveResponse)
async def resolve_live_stream(body: StreamResolveRequest, request: Request) -> StreamResolveResponse:
    """Resolve a URL into instant playable Neon Sync network streams (no full-file download)."""
    c = _container(request)
    try:
        c.probe_limiter.check_and_consume(_client_key(request))
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=brand_error_detail(
                ErrorCode.ERR_RATE_LIMITED.value,
                str(exc),
                retryable=True,
                request_id=_request_id(request),
            ),
        ) from exc

    try:
        probe = await c.orchestrator.probe(ProbeRequest(url=body.url, include_subtitles=True))
    except UrlValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN if exc.code == ErrorCode.ERR_SSRF_BLOCKED else status.HTTP_400_BAD_REQUEST,
            detail=brand_error_detail(exc.code.value, exc.message, request_id=_request_id(request)),
        ) from exc
    except AdapterError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=brand_error_detail(exc.code.value, exc.message, retryable=exc.retryable, request_id=_request_id(request)),
        ) from exc

    candidates = _select_playable_formats(probe.available_formats, content_type=probe.content_type)
    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=brand_error_detail(
                "ERR_STREAM_UNAVAILABLE",
                "This source does not expose a direct live-playable video or audio stream. Try extracting it as a file instead.",
                request_id=_request_id(request),
            ),
        )

    title = sanitize_public_text(probe.title or probe.url)
    tracks: list[PlayableTrack] = []
    for fmt, kind, is_hls in candidates:
        assert fmt.direct_media_url is not None
        # Validate every candidate media URL against SSRF before issuing a token
        try:
            validated_media = validate_and_canonicalize_url(
                fmt.direct_media_url,
                allow_private=c.settings.allow_private_addresses,
                max_length=c.settings.max_url_length,
            )
        except UrlValidationError:
            continue

        mime = "application/vnd.apple.mpegurl" if is_hls else (fmt.mime_type or ("video/mp4" if kind == "video" else "audio/mpeg"))
        reg = c.stream_registry.register_stream(
            target_url=validated_media.canonical_url,
            mime_type=mime,
            is_hls=is_hls,
            title=title,
            referer=probe.url,
        )
        stream_url = (
            f"/v1/stream/hls/{reg.stream_id}/manifest.m3u8"
            if is_hls
            else f"/v1/stream/play/{reg.stream_id}"
        )
        tracks.append(
            PlayableTrack(
                stream_id=reg.stream_id,
                label=_format_label(fmt, kind, is_hls),
                kind=kind,
                protocol="hls" if is_hls else "progressive",
                container="m3u8" if is_hls else fmt.container,
                mime_type=mime,
                resolution=fmt.resolution,
                height=fmt.height,
                fps=fmt.fps,
                size_bytes=fmt.size_bytes,
                stream_url=stream_url,
                m3u_url=f"/v1/stream/playlist/{reg.stream_id}.m3u",
            )
        )

    if not tracks:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=brand_error_detail(
                "ERR_STREAM_UNAVAILABLE",
                "No safe network-playable stream URL was found for this source.",
                request_id=_request_id(request),
            ),
        )

    # Pick default track matching requested mode & quality preference
    matching_kind = [t for t in tracks if t.kind == body.mode] or tracks
    default_track = matching_kind[0]
    if body.preferred_quality.isdigit():
        target_h = int(body.preferred_quality)
        under = [t for t in matching_kind if t.height is not None and t.height <= target_h]
        if under:
            default_track = under[0]

    # Register VTT subtitle tracks for CORS-free in-player closed captions
    subs: list[PlayableSubtitle] = []
    for sub in probe.subtitles[:6]:
        if not sub.url:
            continue
        try:
            validated_sub = validate_and_canonicalize_url(
                sub.url,
                allow_private=c.settings.allow_private_addresses,
                max_length=c.settings.max_url_length,
            )
        except UrlValidationError:
            continue
        reg_sub = c.stream_registry.register_subtitle(
            target_url=validated_sub.canonical_url,
            language=sub.language,
        )
        subs.append(
            PlayableSubtitle(
                sub_id=reg_sub.sub_id,
                language=sub.language,
                name=sub.name,
                is_auto_generated=sub.is_auto_generated,
                url=f"/v1/stream/sub/{reg_sub.sub_id}.vtt",
            )
        )

    chapters = [
        PlayableChapter(
            title=ch.title,
            start_seconds=ch.start_seconds,
            end_seconds=ch.end_seconds,
        )
        for ch in probe.metadata.chapters[:40]
    ]

    return StreamResolveResponse(
        original_url=probe.original_url,
        title=title,
        author=probe.author.name if probe.author else None,
        thumbnail_url=probe.thumbnail.url if probe.thumbnail else None,
        source_domain=probe.source.domain,
        content_type=probe.content_type.value,
        duration_seconds=probe.duration_seconds,
        is_live=probe.is_live,
        default_stream_id=default_track.stream_id,
        tracks=tracks,
        subtitles=subs,
        chapters=chapters,
    )


async def _proxy_media_response(
    *,
    target_url: str,
    fallback_mime: str,
    referer: str | None,
    request: Request,
) -> StreamingResponse:
    """Stream upstream bytes with HTTP 206 Range forwarding and strict SSRF validation."""
    c = _container(request)
    try:
        validate_and_canonicalize_url(
            target_url,
            allow_private=c.settings.allow_private_addresses,
            max_length=c.settings.max_url_length,
        )
    except UrlValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=brand_error_detail(exc.code.value, exc.message, request_id=_request_id(request)),
        ) from exc

    up_headers: dict[str, str] = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }
    if referer:
        up_headers["Referer"] = referer
    range_hdr = request.headers.get("range")
    if range_hdr:
        up_headers["Range"] = range_hdr

    client_kwargs: dict = {
        "follow_redirects": True,
        "max_redirects": c.settings.max_redirects,
        "timeout": httpx.Timeout(connect=12.0, read=45.0, write=15.0, pool=15.0),
    }
    if c.settings.egress_proxy_url:
        client_kwargs["proxy"] = c.settings.egress_proxy_url

    client = httpx.AsyncClient(**client_kwargs)
    try:
        req = client.build_request(request.method, target_url, headers=up_headers)
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=brand_error_detail("ERR_STREAM_UPSTREAM", "Unable to connect to media stream.", request_id=_request_id(request)),
        ) from exc

    # Re-verify final redirected URL against SSRF
    try:
        validate_and_canonicalize_url(
            str(upstream.url),
            allow_private=c.settings.allow_private_addresses,
            max_length=c.settings.max_url_length,
        )
    except UrlValidationError as exc:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=brand_error_detail(exc.code.value, exc.message, request_id=_request_id(request)),
        ) from exc

    if upstream.status_code >= 400:
        code = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY if code >= 500 else code,
            detail=brand_error_detail("ERR_STREAM_UPSTREAM", f"Media source returned HTTP {code}.", request_id=_request_id(request)),
        )

    out_headers: dict[str, str] = {
        "Accept-Ranges": upstream.headers.get("accept-ranges", "bytes"),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    for hname in ("content-length", "content-range", "etag", "last-modified"):
        if hname in upstream.headers:
            out_headers[hname.title()] = upstream.headers[hname]

    media_type = upstream.headers.get("content-type", fallback_mime).split(";")[0].strip() or fallback_mime

    async def _body_iter() -> AsyncIterator[bytes]:
        try:
            if request.method == "HEAD":
                return
            async for chunk in upstream.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        _body_iter(),
        status_code=upstream.status_code,
        media_type=media_type,
        headers=out_headers,
    )


@stream_router.get("/play/{stream_id}")
@stream_router.head("/play/{stream_id}", include_in_schema=False)
async def play_live_stream(stream_id: str, request: Request) -> StreamingResponse:
    """Live byte-range streaming proxy for progressive video/audio (VLC & browser seekable)."""
    entry = _container(request).stream_registry.get_stream(stream_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=brand_error_detail("ERR_NOT_FOUND", "Stream token expired or not found. Re-resolve to play.", request_id=_request_id(request)),
        )
    return await _proxy_media_response(
        target_url=entry.target_url,
        fallback_mime=entry.mime_type,
        referer=entry.referer,
        request=request,
    )


@stream_router.get("/hls/{stream_id}/manifest.m3u8")
async def stream_hls_manifest(stream_id: str, request: Request) -> Response:
    """Fetch and rewrite an HLS (.m3u8) manifest so browser HLS.js & VLC play cleanly without CORS errors."""
    c = _container(request)
    entry = c.stream_registry.get_stream(stream_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=brand_error_detail("ERR_NOT_FOUND", "HLS stream token expired or not found.", request_id=_request_id(request)),
        )
    return await _fetch_and_rewrite_m3u8(entry.target_url, stream_id=stream_id, referer=entry.referer, request=request)


async def _fetch_and_rewrite_m3u8(
    manifest_url: str,
    *,
    stream_id: str,
    referer: str | None,
    request: Request,
) -> Response:
    c = _container(request)
    try:
        validate_and_canonicalize_url(
            manifest_url,
            allow_private=c.settings.allow_private_addresses,
            max_length=c.settings.max_url_length,
        )
    except UrlValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=brand_error_detail(exc.code.value, exc.message, request_id=_request_id(request)),
        ) from exc

    client_kwargs: dict = {"follow_redirects": True, "max_redirects": c.settings.max_redirects, "timeout": 15.0}
    if c.settings.egress_proxy_url:
        client_kwargs["proxy"] = c.settings.egress_proxy_url
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(manifest_url, headers=headers)
            resp.raise_for_status()
            final_url = str(resp.url)
            text = resp.text
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=brand_error_detail("ERR_STREAM_UPSTREAM", "Unable to load live stream manifest.", request_id=_request_id(request)),
        ) from exc

    rewritten = _rewrite_hls_manifest(
        text,
        base_url=final_url,
        stream_id=stream_id,
        registry=c.stream_registry,
    )
    return Response(
        content=rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@stream_router.get("/hls/{stream_id}/asset")
async def stream_hls_asset(
    stream_id: str,
    request: Request,
    u: str = Query(min_length=8, max_length=4096),
    sig: str = Query(min_length=16, max_length=64),
) -> Response:
    """Proxy a signed HLS child playlist or media segment with HMAC verification & SSRF guard."""
    c = _container(request)
    entry = c.stream_registry.get_stream(stream_id)
    if entry is None or not c.stream_registry.verify_hls_asset(stream_id, u, sig):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=brand_error_detail("ERR_STREAM_SIGNATURE", "Invalid or expired stream segment token.", request_id=_request_id(request)),
        )
    if ".m3u8" in u.lower().split("?", 1)[0]:
        return await _fetch_and_rewrite_m3u8(u, stream_id=stream_id, referer=entry.referer, request=request)
    return await _proxy_media_response(
        target_url=u,
        fallback_mime="video/mp2t",
        referer=entry.referer,
        request=request,
    )


@stream_router.get("/playlist/{stream_id}.m3u")
async def download_vlc_playlist(stream_id: str, request: Request) -> Response:
    """Download a VLC/IINA/mpv-compatible `.m3u` network stream playlist for instant external playback."""
    entry = _container(request).stream_registry.get_stream(stream_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=brand_error_detail("ERR_NOT_FOUND", "Stream token expired or not found.", request_id=_request_id(request)),
        )
    origin = _request_origin(request)
    rel = (
        f"/v1/stream/hls/{entry.stream_id}/manifest.m3u8"
        if entry.is_hls
        else f"/v1/stream/play/{entry.stream_id}"
    )
    clean_title = entry.title.replace("\n", " ").replace("\r", " ")[:120]
    m3u_body = f"#EXTM3U\n#EXTINF:-1,{clean_title}\n{origin}{rel}\n"
    fname = f"{sanitize_filename(clean_title, fallback='neon_sync_stream', max_len=64)}.m3u"
    return Response(
        content=m3u_body,
        media_type="audio/x-mpegurl",
        headers={
            "Content-Disposition": build_content_disposition(fname),
            "Cache-Control": "no-store",
        },
    )


@stream_router.get("/sub/{sub_id}.vtt")
async def stream_subtitle_track(sub_id: str, request: Request) -> Response:
    """Proxy a registered WebVTT caption track for CORS-free live player subtitles."""
    c = _container(request)
    sub = c.stream_registry.get_subtitle(sub_id)
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=brand_error_detail("ERR_NOT_FOUND", "Subtitle track expired or not found.", request_id=_request_id(request)),
        )
    try:
        validate_and_canonicalize_url(
            sub.target_url,
            allow_private=c.settings.allow_private_addresses,
            max_length=c.settings.max_url_length,
        )
    except UrlValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=brand_error_detail(exc.code.value, exc.message, request_id=_request_id(request)),
        ) from exc
    client_kwargs: dict = {"follow_redirects": True, "max_redirects": c.settings.max_redirects, "timeout": 12.0}
    if c.settings.egress_proxy_url:
        client_kwargs["proxy"] = c.settings.egress_proxy_url
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(sub.target_url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            body = resp.text
            if body.lstrip().startswith("{"):
                body = convert_json3_to_vtt(body) or body
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=brand_error_detail("ERR_STREAM_UPSTREAM", "Unable to fetch caption track.", request_id=_request_id(request)),
        ) from exc
    return Response(content=body, media_type="text/vtt; charset=utf-8", headers={"Cache-Control": "private, max-age=300"})
