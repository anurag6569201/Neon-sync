"""Zero-Egress FFmpeg / ffprobe Media Validation & Post-Processing Adapter.

Security invariants:
- ALWAYS passes `-protocol_whitelist file,pipe` BEFORE `-i` to block network SSRF,
  `concat:`, and `subfile:` local-file-read exploits in crafted media files.
- Never receives proxy env vars (designed for zero-network-egress media workers).
- Enforces strict `-fs` output byte cap and wall-clock timeouts.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from neon_sync.config import Settings
from neon_sync.models import (
    CostProfile,
    EngineCapability,
    EngineManifest,
    ErrorCode,
    HealthReport,
    InstallationStatus,
    InvocationMode,
    SupportedDomains,
)
from neon_sync.security import CancellationToken, run_safe_subprocess

from .base import AdapterError, PostProcessorAdapter


class FfmpegAdapter(PostProcessorAdapter):
    """Sandboxed local-only FFmpeg/ffprobe validation, remux, audio extract & clipping."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._version = "unknown"

    @property
    def id(self) -> str:
        return "ffmpeg"

    def manifest(self) -> EngineManifest:
        installed = (
            shutil.which(self._settings.ffmpeg_bin) is not None
            and shutil.which(self._settings.ffprobe_bin) is not None
        )
        return EngineManifest(
            id=self.id,
            name="FFmpeg / ffprobe Media Processor",
            version=self._version,
            tier=2,
            supported_content_types=["video", "audio", "image"],
            supported_domains=SupportedDomains(mode="allowlist", domains=[]),
            protocols=["http", "https"],
            capabilities=[
                EngineCapability.PROCESS_MEDIA,
                EngineCapability.EXTRACT_AUDIO,
                EngineCapability.EXTRACT_THUMBNAIL,
            ],
            cost_profile=CostProfile(avg_probe_ms=150, memory_mb=256, requires_browser=False),
            priority=1000,
            installation_status=(
                InstallationStatus.INSTALLED if installed else InstallationStatus.MISSING
            ),
            license="LGPL-2.1+ / GPL-2.0+ (CLI subprocess isolated)",
            invocation_mode=InvocationMode.SUBPROCESS,
        )

    def build_ffprobe_argv(self, input_path: Path) -> list[str]:
        return [
            self._settings.ffprobe_bin,
            "-v",
            "quiet",
            "-protocol_whitelist",
            "file,pipe",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-i",
            str(input_path),
        ]

    def build_remux_argv(
        self,
        input_path: Path,
        output_path: Path,
        *,
        max_output_bytes: int,
    ) -> list[str]:
        return [
            self._settings.ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(input_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-fs",
            str(max_output_bytes),
            str(output_path),
        ]

    def build_audio_extract_argv(
        self,
        input_path: Path,
        output_path: Path,
        *,
        audio_format: str = "mp3",
        max_output_bytes: int,
    ) -> list[str]:
        codec_args = {
            "mp3": ["-vn", "-c:a", "libmp3lame", "-q:a", "2"],
            "m4a": ["-vn", "-c:a", "aac", "-b:a", "192k"],
            "opus": ["-vn", "-c:a", "libopus", "-b:a", "160k"],
            "flac": ["-vn", "-c:a", "flac"],
            "wav": ["-vn", "-c:a", "pcm_s16le"],
        }.get(audio_format, ["-vn", "-c:a", "copy"])

        return [
            self._settings.ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(input_path),
            *codec_args,
            "-fs",
            str(max_output_bytes),
            str(output_path),
        ]

    def build_clip_argv(
        self,
        input_path: Path,
        output_path: Path,
        *,
        start_seconds: float,
        end_seconds: float | None,
        max_output_bytes: int,
    ) -> list[str]:
        argv = [
            self._settings.ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-ss",
            f"{start_seconds:.3f}",
        ]
        if end_seconds is not None and end_seconds > start_seconds:
            argv.extend(["-to", f"{end_seconds:.3f}"])
        argv.extend(
            [
                "-i",
                str(input_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-fs",
                str(max_output_bytes),
                str(output_path),
            ]
        )
        return argv

    def build_thumbnail_argv(self, input_path: Path, output_path: Path) -> list[str]:
        return [
            self._settings.ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-ss",
            "00:00:02",
            "-i",
            str(input_path),
            "-vframes",
            "1",
            "-vf",
            "scale=640:-2",
            str(output_path),
        ]

    async def probe_file(
        self,
        path: Path,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> dict[str, Any]:
        argv = self.build_ffprobe_argv(path)
        res = await run_safe_subprocess(
            argv,
            timeout=20.0,
            proxy_url=None,  # Zero-egress!
            cancel_token=cancel_token,
        )
        if res.returncode != 0:
            raise AdapterError(
                ErrorCode.ERR_VALIDATION_FAILED,
                f"ffprobe failed on {path.name}: {res.stderr[-300:]}",
                engine_id=self.id,
            )
        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            raise AdapterError(
                ErrorCode.ERR_VALIDATION_FAILED,
                f"ffprobe returned invalid JSON: {exc}",
                engine_id=self.id,
            ) from exc

        fmt = data.get("format") or {}
        streams = data.get("streams") or []
        vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
        astream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        return {
            "format_name": fmt.get("format_name"),
            "duration_seconds": float(fmt["duration"]) if fmt.get("duration") else None,
            "size_bytes": int(fmt["size"]) if fmt.get("size") else None,
            "video_codec": vstream.get("codec_name") if vstream else None,
            "width": vstream.get("width") if vstream else None,
            "height": vstream.get("height") if vstream else None,
            "audio_codec": astream.get("codec_name") if astream else None,
        }

    async def extract_audio(
        self,
        input_path: Path,
        output_path: Path,
        *,
        audio_format: str = "mp3",
        max_output_bytes: int,
        cancel_token: CancellationToken | None = None,
    ) -> Path:
        argv = self.build_audio_extract_argv(
            input_path,
            output_path,
            audio_format=audio_format,
            max_output_bytes=max_output_bytes,
        )
        res = await run_safe_subprocess(
            argv,
            timeout=float(self._settings.ffmpeg_timeout_seconds),
            proxy_url=None,
            cancel_token=cancel_token,
        )
        if res.returncode != 0 or not output_path.exists():
            raise AdapterError(
                ErrorCode.ERR_INTERNAL,
                f"FFmpeg audio extraction failed: {res.stderr[-300:]}",
                engine_id=self.id,
            )
        return output_path

    async def clip_media(
        self,
        input_path: Path,
        output_path: Path,
        *,
        start_seconds: float,
        end_seconds: float | None,
        max_output_bytes: int,
        cancel_token: CancellationToken | None = None,
    ) -> Path:
        argv = self.build_clip_argv(
            input_path,
            output_path,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            max_output_bytes=max_output_bytes,
        )
        res = await run_safe_subprocess(
            argv,
            timeout=float(self._settings.ffmpeg_timeout_seconds),
            proxy_url=None,
            cancel_token=cancel_token,
        )
        if res.returncode != 0 or not output_path.exists():
            raise AdapterError(
                ErrorCode.ERR_INTERNAL,
                f"FFmpeg clip failed: {res.stderr[-300:]}",
                engine_id=self.id,
            )
        return output_path

    async def health_check(self) -> HealthReport:
        ff = shutil.which(self._settings.ffmpeg_bin)
        fp = shutil.which(self._settings.ffprobe_bin)
        if not ff or not fp:
            return HealthReport(
                engine_id=self.id,
                installation_status=InstallationStatus.MISSING,
                version="missing",
                healthy=False,
                detail="ffmpeg or ffprobe not found on PATH.",
            )
        res = await run_safe_subprocess([ff, "-version"], timeout=5.0)
        first = (res.stdout.splitlines() or ["unknown"])[0]
        parts = first.split()
        version = parts[2] if len(parts) >= 3 else "unknown"
        self._version = version
        return HealthReport(
            engine_id=self.id,
            installation_status=InstallationStatus.INSTALLED,
            version=version,
            healthy=(res.returncode == 0),
            detail=first,
        )
