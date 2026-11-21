"""Normalized media output schema matching research/schemas/normalized-media-output.schema.json."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ContentType(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    GALLERY = "gallery"
    PLAYLIST = "playlist"
    LIVESTREAM = "livestream"
    DOCUMENT = "document"
    ARCHIVE = "archive"
    WEBPAGE = "webpage"
    FILE = "file"
    UNKNOWN = "unknown"


class ProtocolType(StrEnum):
    HTTPS = "https"
    HTTP = "http"
    HLS = "hls"
    DASH = "dash"
    ISM = "ism"
    RTMP = "rtmp"
    OTHER = "other"


class DownloadStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    QUEUED = "queued"
    PROBING = "probing"
    DOWNLOADING = "downloading"
    POST_PROCESSING = "post_processing"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorCode(StrEnum):
    ERR_INVALID_URL = "ERR_INVALID_URL"
    ERR_SSRF_BLOCKED = "ERR_SSRF_BLOCKED"
    ERR_UNSUPPORTED_URL = "ERR_UNSUPPORTED_URL"
    ERR_DRM_PROTECTED = "ERR_DRM_PROTECTED"
    ERR_AUTH_REQUIRED = "ERR_AUTH_REQUIRED"
    ERR_CHALLENGE_REQUIRED = "ERR_CHALLENGE_REQUIRED"
    ERR_NOT_FOUND = "ERR_NOT_FOUND"
    ERR_GEO_BLOCKED = "ERR_GEO_BLOCKED"
    ERR_RATE_LIMITED = "ERR_RATE_LIMITED"
    ERR_QUOTA_EXCEEDED = "ERR_QUOTA_EXCEEDED"
    ERR_TIMEOUT = "ERR_TIMEOUT"
    ERR_EXTRACTOR_OUTDATED = "ERR_EXTRACTOR_OUTDATED"
    ERR_VALIDATION_FAILED = "ERR_VALIDATION_FAILED"
    ERR_INTERNAL = "ERR_INTERNAL"


# Errors where escalating to another engine is futile or unsafe
HARD_STOP_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.ERR_INVALID_URL,
        ErrorCode.ERR_SSRF_BLOCKED,
        ErrorCode.ERR_DRM_PROTECTED,
        ErrorCode.ERR_AUTH_REQUIRED,
        ErrorCode.ERR_CHALLENGE_REQUIRED,
        ErrorCode.ERR_NOT_FOUND,
        ErrorCode.ERR_GEO_BLOCKED,
        ErrorCode.ERR_QUOTA_EXCEEDED,
    }
)


class SourceInfo(BaseModel):
    domain: str
    platform: str | None = None
    extractor_key: str | None = None
    external_id: str | None = None


class ThumbnailInfo(BaseModel):
    url: str
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    mime_type: str | None = None


class AuthorInfo(BaseModel):
    name: str | None = None
    id: str | None = None
    url: str | None = None


class FormatInfo(BaseModel):
    format_id: str
    label: str | None = None
    container: str
    mime_type: str
    resolution: str | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    fps: float | None = Field(default=None, ge=0)
    video_codec: str | None = None
    audio_codec: str | None = None
    video_bitrate_kbps: float | None = Field(default=None, ge=0)
    audio_bitrate_kbps: float | None = Field(default=None, ge=0)
    size_bytes: int | None = Field(default=None, ge=0)
    size_is_approx: bool = False
    protocol: ProtocolType = ProtocolType.HTTPS
    has_video: bool = False
    has_audio: bool = False
    requires_muxing: bool = False
    direct_media_url: str | None = None
    expires_at: datetime | None = None


class SubtitleTrack(BaseModel):
    language: str
    name: str | None = None
    is_auto_generated: bool = False
    formats: list[str] = Field(default_factory=list)
    url: str | None = None


class TranscriptInfo(BaseModel):
    available: bool = False
    language: str | None = None
    source: Literal["manual_captions", "auto_captions", "webpage_text"] | None = None
    artifact_url: str | None = None


class MediaItem(BaseModel):
    index: int = Field(ge=1)
    external_id: str | None = None
    url: str
    title: str | None = None
    content_type: str
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    size_bytes: int | None = None
    thumbnail_url: str | None = None
    direct_media_url: str | None = None


class ChapterInfo(BaseModel):
    title: str
    start_seconds: float
    end_seconds: float


class MediaMetadata(BaseModel):
    published_at: datetime | None = None
    view_count: int | None = None
    like_count: int | None = None
    tags: list[str] = Field(default_factory=list)
    item_count_total: int | None = None
    item_count_returned: int | None = None
    chapters: list[ChapterInfo] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class DownloadInfo(BaseModel):
    status: DownloadStatus = DownloadStatus.NOT_REQUESTED
    selected_format_id: str | None = None
    artifact_url: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    expires_at: datetime | None = None


class EngineProvenance(BaseModel):
    engine_used: str
    engine_version: str
    post_processor: str | None = None
    fallback_chain: list[str] = Field(default_factory=list)
    used_browser_fallback: bool = False
    used_authentication: bool = False
    probe_duration_ms: int | None = Field(default=None, ge=0)
    download_duration_ms: int | None = Field(default=None, ge=0)


class WarningInfo(BaseModel):
    code: str
    message: str
    engine: str | None = None


class ErrorInfo(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool = False
    engine: str | None = None


class NormalizedMediaOutput(BaseModel):
    """Canonical response returned regardless of which engine executed."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    job_id: str
    original_url: str
    url: str
    source: SourceInfo
    content_type: ContentType = ContentType.UNKNOWN
    is_live: bool = False
    title: str | None = None
    description: str | None = None
    thumbnail: ThumbnailInfo | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    size_bytes: int | None = Field(default=None, ge=0)
    author: AuthorInfo | None = None
    available_formats: list[FormatInfo] = Field(default_factory=list)
    subtitles: list[SubtitleTrack] = Field(default_factory=list)
    transcript: TranscriptInfo | None = None
    items: list[MediaItem] = Field(default_factory=list)
    metadata: MediaMetadata = Field(default_factory=MediaMetadata)
    download: DownloadInfo = Field(default_factory=DownloadInfo)
    engine: EngineProvenance
    warnings: list[WarningInfo] = Field(default_factory=list)
    errors: list[ErrorInfo] = Field(default_factory=list)
