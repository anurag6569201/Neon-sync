"""Declarative Format, Quality, Codec & Post-Processing Options Catalog.

Single source of truth consumed by:
- API request validation (`DownloadRequest`, `BatchDownloadRequest`)
- Adapter format-selector & FFmpeg argument builders
- `GET /v1/formats` endpoint (hydrates Web UI, Mobile, Desktop & SDK clients dynamically)
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

VideoQuality = Literal["best", "2160", "1440", "1080", "720", "480", "360", "worst"]
VideoContainer = Literal["auto", "mp4", "mkv", "webm", "mov"]
VideoCodec = Literal["auto", "h264", "hevc", "vp9", "av1"]
AudioFormat = Literal["best", "mp3", "m4a", "opus", "flac", "wav"]
AudioBitrate = Literal["auto", "320k", "256k", "192k", "128k", "96k"]
ImageFormat = Literal["original", "jpg", "png", "webp", "avif"]
GalleryArchiveFormat = Literal["zip", "tar"]
SubtitleMode = Literal["prefer_manual", "manual_only", "include_auto"]
SubtitleFormat = Literal["vtt", "srt", "ass"]


class PresetDefinition(BaseModel):
    id: str
    name: str
    description: str
    settings: dict[str, Any]


class ChoiceOption(BaseModel):
    id: str
    label: str


class FormatCatalog(BaseModel):
    """Machine-readable catalog returned by GET /v1/formats."""

    modes: list[ChoiceOption]
    video_qualities: list[ChoiceOption]
    video_containers: list[ChoiceOption]
    video_codecs: list[ChoiceOption]
    fps_limits: list[ChoiceOption]
    audio_formats: list[ChoiceOption]
    audio_bitrates: list[ChoiceOption]
    image_formats: list[ChoiceOption]
    image_max_widths: list[ChoiceOption]
    gallery_archive_formats: list[ChoiceOption]
    subtitle_modes: list[ChoiceOption]
    subtitle_formats: list[ChoiceOption]
    presets: list[PresetDefinition]


PRESETS: list[PresetDefinition] = [
    PresetDefinition(
        id="best_compatible_mp4",
        name="1080p Compatible MP4",
        description="1080p H.264 MP4 with AAC audio, faststart & embedded metadata (plays everywhere).",
        settings={
            "mode": "video",
            "video_quality": "1080",
            "video_container": "mp4",
            "video_codec": "h264",
            "remux_faststart": True,
            "embed_metadata": True,
            "embed_thumbnail": True,
        },
    ),
    PresetDefinition(
        id="4k_master",
        name="4K Master Archive",
        description="Highest quality up to 2160p with subtitles, thumbnail & metadata.",
        settings={
            "mode": "video",
            "video_quality": "2160",
            "video_container": "mkv",
            "video_codec": "auto",
            "extract_subtitles": True,
            "embed_subtitles": True,
            "embed_metadata": True,
            "embed_thumbnail": True,
        },
    ),
    PresetDefinition(
        id="social_720p",
        name="720p Fast Web",
        description="Compact 720p 30fps H.264 MP4 optimized for fast downloads and sharing.",
        settings={
            "mode": "video",
            "video_quality": "720",
            "video_container": "mp4",
            "video_codec": "h264",
            "fps_limit": 30,
            "remux_faststart": True,
        },
    ),
    PresetDefinition(
        id="audio_mp3_320",
        name="MP3 320kbps",
        description="High-bitrate MP3 audio extraction with cover art & metadata.",
        settings={
            "mode": "audio",
            "audio_format": "mp3",
            "audio_bitrate": "320k",
            "embed_metadata": True,
            "embed_thumbnail": True,
        },
    ),
    PresetDefinition(
        id="audio_flac",
        name="Lossless FLAC",
        description="Uncompressed lossless FLAC audio extraction with loudness normalization.",
        settings={
            "mode": "audio",
            "audio_format": "flac",
            "normalize_audio": True,
            "embed_metadata": True,
        },
    ),
    PresetDefinition(
        id="gallery_webp_hd",
        name="WebP HD Gallery",
        description="Convert images/galleries to optimized WebP (Q90, max 1920px width) in ZIP.",
        settings={
            "mode": "image",
            "image_format": "webp",
            "image_quality": 90,
            "image_max_width": 1920,
            "gallery_archive_format": "zip",
        },
    ),
]


def get_format_catalog() -> FormatCatalog:
    return FormatCatalog(
        modes=[
            ChoiceOption(id="auto", label="Auto Detect"),
            ChoiceOption(id="video", label="Video"),
            ChoiceOption(id="audio", label="Audio Only"),
            ChoiceOption(id="image", label="Image / Gallery"),
            ChoiceOption(id="direct", label="Direct File"),
        ],
        video_qualities=[
            ChoiceOption(id="best", label="Best Available"),
            ChoiceOption(id="2160", label="4K (2160p)"),
            ChoiceOption(id="1440", label="2K (1440p)"),
            ChoiceOption(id="1080", label="Full HD (1080p)"),
            ChoiceOption(id="720", label="HD (720p)"),
            ChoiceOption(id="480", label="SD (480p)"),
            ChoiceOption(id="360", label="Low (360p)"),
            ChoiceOption(id="worst", label="Smallest Size"),
        ],
        video_containers=[
            ChoiceOption(id="auto", label="Auto (Source)"),
            ChoiceOption(id="mp4", label="MP4"),
            ChoiceOption(id="mkv", label="MKV"),
            ChoiceOption(id="webm", label="WebM"),
            ChoiceOption(id="mov", label="MOV"),
        ],
        video_codecs=[
            ChoiceOption(id="auto", label="Auto"),
            ChoiceOption(id="h264", label="H.264 / AVC (Most Compatible)"),
            ChoiceOption(id="hevc", label="HEVC / H.265"),
            ChoiceOption(id="vp9", label="VP9"),
            ChoiceOption(id="av1", label="AV1 (Smallest)"),
        ],
        fps_limits=[
            ChoiceOption(id="any", label="Any FPS"),
            ChoiceOption(id="60", label="Up to 60 fps"),
            ChoiceOption(id="30", label="Up to 30 fps"),
        ],
        audio_formats=[
            ChoiceOption(id="best", label="Best (Source)"),
            ChoiceOption(id="mp3", label="MP3"),
            ChoiceOption(id="m4a", label="M4A (AAC)"),
            ChoiceOption(id="opus", label="Opus"),
            ChoiceOption(id="flac", label="FLAC (Lossless)"),
            ChoiceOption(id="wav", label="WAV (Uncompressed)"),
        ],
        audio_bitrates=[
            ChoiceOption(id="auto", label="Auto"),
            ChoiceOption(id="320k", label="320 kbps"),
            ChoiceOption(id="256k", label="256 kbps"),
            ChoiceOption(id="192k", label="192 kbps"),
            ChoiceOption(id="128k", label="128 kbps"),
            ChoiceOption(id="96k", label="96 kbps"),
        ],
        image_formats=[
            ChoiceOption(id="original", label="Original (Unchanged)"),
            ChoiceOption(id="jpg", label="JPEG (.jpg)"),
            ChoiceOption(id="png", label="PNG (.png)"),
            ChoiceOption(id="webp", label="WebP (.webp)"),
            ChoiceOption(id="avif", label="AVIF (.avif)"),
        ],
        image_max_widths=[
            ChoiceOption(id="original", label="Original Resolution"),
            ChoiceOption(id="3840", label="4K (3840px)"),
            ChoiceOption(id="1920", label="Full HD (1920px)"),
            ChoiceOption(id="1280", label="HD (1280px)"),
            ChoiceOption(id="800", label="Compact (800px)"),
        ],
        gallery_archive_formats=[
            ChoiceOption(id="zip", label="ZIP Archive (.zip)"),
            ChoiceOption(id="tar", label="TAR Archive (.tar)"),
        ],
        subtitle_modes=[
            ChoiceOption(id="prefer_manual", label="Prefer Manual (fallback to Auto)"),
            ChoiceOption(id="manual_only", label="Manual Only"),
            ChoiceOption(id="include_auto", label="Manual + Auto-Generated"),
        ],
        subtitle_formats=[
            ChoiceOption(id="vtt", label="WebVTT (.vtt)"),
            ChoiceOption(id="srt", label="SubRip (.srt)"),
            ChoiceOption(id="ass", label="ASS (.ass)"),
        ],
        presets=PRESETS,
    )


def build_ytdlp_format_selector(
    *,
    mode: str = "auto",
    format_id: str | None = None,
    video_quality: VideoQuality = "best",
    video_codec: VideoCodec = "auto",
    video_container: VideoContainer = "auto",
    fps_limit: int | None = None,
    video_only: bool = False,
) -> str:
    """Translate high-level quality/codec/container/FPS settings into a yt-dlp `-f` selector."""
    if format_id:
        return format_id
    if mode == "audio":
        return "bestaudio/best"
    if video_quality == "worst":
        return "worstvideo/worst" if video_only else "worstvideo+worstaudio/worst"

    filters: list[str] = []
    if video_quality != "best" and video_quality.isdigit():
        filters.append(f"height<={video_quality}")
    if fps_limit is not None and fps_limit > 0:
        filters.append(f"fps<={fps_limit}")

    codec_patterns: dict[str, str] = {
        "h264": "vcodec^=avc",
        "hevc": "vcodec~=^(hev|hvc|hevc)",
        "vp9": "vcodec^=vp09",
        "av1": "vcodec^=av01",
    }
    if video_codec in codec_patterns:
        filters.append(codec_patterns[video_codec])

    v_filter = "".join(f"[{f}]" for f in filters)

    if video_only:
        height_only = f"[height<={video_quality}]" if video_quality.isdigit() else ""
        if v_filter:
            parts = [f"bestvideo{v_filter}"]
            if height_only and height_only != v_filter:
                parts.append(f"bestvideo{height_only}")
            parts.append("bestvideo")
            return "/".join(parts)
        if video_container == "mp4":
            return "bestvideo[ext=mp4]/bestvideo"
        return "bestvideo"

    # Prefer matching audio container when MP4 requested for lossless remuxing
    a_pref = "bestaudio[ext=m4a]/bestaudio" if video_container == "mp4" else "bestaudio"

    if v_filter:
        # Graceful fallback chain: exact filter -> height-only -> best available
        height_only = f"[height<={video_quality}]" if video_quality.isdigit() else ""
        parts = [f"bestvideo{v_filter}+{a_pref}", f"best{v_filter}"]
        if height_only and height_only != v_filter:
            parts.append(f"bestvideo{height_only}+bestaudio")
            parts.append(f"best{height_only}")
        parts.append("bestvideo+bestaudio/best")
        return "/".join(parts)

    if video_container == "mp4":
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
    return "bestvideo+bestaudio/best"


class AdvancedProcessingOptions(BaseModel):
    """Reusable mixin of advanced video/audio/image/subtitle/clipping settings."""

    # Video
    video_quality: VideoQuality = "best"
    video_container: VideoContainer = "auto"
    video_codec: VideoCodec = "auto"
    fps_limit: int | None = Field(default=None, ge=1, le=240)
    video_only: bool = False
    remux_faststart: bool = True

    # Audio
    audio_format: AudioFormat | None = None
    audio_bitrate: AudioBitrate = "auto"
    normalize_audio: bool = False

    # Image & Gallery
    image_format: ImageFormat = "original"
    image_quality: int = Field(default=90, ge=1, le=100)
    image_max_width: int | None = Field(default=None, ge=64, le=16384)
    gallery_archive_format: GalleryArchiveFormat = "zip"
    gallery_range_start: int = Field(default=1, ge=1, le=500)
    gallery_range_end: int | None = Field(default=None, ge=1, le=500)

    # Subtitles, Metadata & Transcripts
    extract_thumbnail: bool = False
    embed_thumbnail: bool = False
    embed_metadata: bool = True
    extract_subtitles: bool = False
    subtitles_only: bool = False
    embed_subtitles: bool = False
    subtitle_languages: list[str] = Field(default_factory=lambda: ["en"])
    subtitle_mode: SubtitleMode = "prefer_manual"
    subtitle_format: SubtitleFormat = "vtt"
    generate_transcript: bool = False

    # Clipping, Playlists & Live Streams
    clip_start_seconds: float | None = Field(default=None, ge=0)
    clip_end_seconds: float | None = Field(default=None, ge=0)
    playlist_items: str | None = Field(default=None, max_length=64, pattern=r"^[0-9,\-: ]+$")
    livestream_max_duration: int | None = Field(default=None, ge=5, le=7200)

    # Naming
    custom_filename_prefix: str = Field(default="", max_length=64)
