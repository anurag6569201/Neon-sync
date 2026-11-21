"""Engine adapters for Neon Sync."""

from .aria2 import Aria2Adapter
from .base import (
    AdapterError,
    DownloadArtifact,
    ExtractorAdapter,
    PostProcessorAdapter,
    ProgressCallback,
    UrlContext,
)
from .browser_fallback import PlaywrightFallbackAdapter
from .direct_http import DirectHttpAdapter
from .ffmpeg import FfmpegAdapter
from .gallery_dl import GalleryDlAdapter
from .nm3u8dl import NM3u8DlReAdapter
from .streamlink import StreamlinkAdapter
from .ytdlp import YtDlpAdapter

__all__ = [
    "AdapterError",
    "Aria2Adapter",
    "DirectHttpAdapter",
    "DownloadArtifact",
    "ExtractorAdapter",
    "FfmpegAdapter",
    "GalleryDlAdapter",
    "NM3u8DlReAdapter",
    "PlaywrightFallbackAdapter",
    "PostProcessorAdapter",
    "ProgressCallback",
    "StreamlinkAdapter",
    "UrlContext",
    "YtDlpAdapter",
]
