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
from .direct_http import DirectHttpAdapter
from .ffmpeg import FfmpegAdapter
from .gallery_dl import GalleryDlAdapter
from .ytdlp import YtDlpAdapter

__all__ = [
    "AdapterError",
    "Aria2Adapter",
    "DirectHttpAdapter",
    "DownloadArtifact",
    "ExtractorAdapter",
    "FfmpegAdapter",
    "GalleryDlAdapter",
    "PostProcessorAdapter",
    "ProgressCallback",
    "UrlContext",
    "YtDlpAdapter",
]
