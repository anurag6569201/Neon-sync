"""Zero-Egress FFmpeg / ffprobe Media Validation & Post-Processing Adapter.

Security invariants:
- ALWAYS passes `-protocol_whitelist file,pipe` BEFORE `-i` to block network SSRF,
  `concat:`, and `subfile:` local-file-read exploits in crafted media files.
- Never receives proxy env vars (designed for zero-network-egress media workers).
- Enforces strict `-fs` output byte cap and wall-clock timeouts.
"""

from __future__ import annotations

import json
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
from neon_sync.security import CancellationToken, find_binary, run_safe_subprocess

from .base import AdapterError, PostProcessorAdapter


class FfmpegAdapter(PostProcessorAdapter):
    """Sandboxed local-only FFmpeg/ffprobe validation, remux, audio extract & clipping."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._version = "unknown"

    @property
    def id(self) -> str:
        return "ffmpeg"

    def _ffmpeg(self) -> str:
        return find_binary(self._settings.ffmpeg_bin) or self._settings.ffmpeg_bin

    def _ffprobe(self) -> str:
        return find_binary(self._settings.ffprobe_bin) or self._settings.ffprobe_bin

    def manifest(self) -> EngineManifest:
        installed = (
            find_binary(self._settings.ffmpeg_bin) is not None
            and find_binary(self._settings.ffprobe_bin) is not None
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
            self._ffprobe(),
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
            self._ffmpeg(),
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
        audio_bitrate: str = "auto",
        normalize_audio: bool = False,
        max_output_bytes: int,
    ) -> list[str]:
        br = audio_bitrate if audio_bitrate != "auto" else None
        codec_args = {
            "mp3": ["-vn", "-c:a", "libmp3lame", *(["-b:a", br] if br else ["-q:a", "2"])],
            "m4a": ["-vn", "-c:a", "aac", "-b:a", br or "192k"],
            "opus": ["-vn", "-c:a", "libopus", "-b:a", br or "160k"],
            "flac": ["-vn", "-c:a", "flac"],
            "wav": ["-vn", "-c:a", "pcm_s16le"],
        }.get(audio_format, ["-vn", "-c:a", "copy"])

        filter_args = ["-af", "loudnorm"] if normalize_audio else []

        return [
            self._ffmpeg(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(input_path),
            *filter_args,
            *codec_args,
            "-fs",
            str(max_output_bytes),
            str(output_path),
        ]

    def build_image_process_argv(
        self,
        input_path: Path,
        output_path: Path,
        *,
        image_format: str = "webp",
        quality: int = 90,
        max_width: int | None = None,
        strip_metadata: bool = False,
    ) -> list[str]:
        """Build zero-egress FFmpeg image format conversion & aspect-preserving resize argv."""
        argv = [
            self._ffmpeg(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(input_path),
        ]
        if max_width is not None and max_width > 0:
            argv.extend(["-vf", f"scale='min({max_width},iw)':-2"])
        if strip_metadata:
            argv.extend(["-map_metadata", "-1"])

        q = max(1, min(100, quality))
        if image_format == "webp":
            argv.extend(["-c:v", "libwebp", "-quality", str(q)])
        elif image_format == "jpg":
            # Map 1..100 quality -> ffmpeg mjpeg qscale 31..2
            qscale = max(2, min(31, round(31 - (q / 100.0) * 29)))
            argv.extend(["-q:v", str(qscale)])
        elif image_format == "avif":
            crf = max(0, min(63, round(63 - (q / 100.0) * 50)))
            argv.extend(["-c:v", "libsvtav1", "-crf", str(crf), "-still-picture", "1"])
        elif image_format == "png":
            argv.extend(["-c:v", "png"])

        argv.extend(["-frames:v", "1", str(output_path)])
        return argv

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
            self._ffmpeg(),
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
        argv.extend(["-i", str(input_path), "-c", "copy"])
        if output_path.suffix.lower() in {".mp4", ".mov", ".m4a", ".m4v"}:
            argv.extend(["-movflags", "+faststart"])
        argv.extend(["-fs", str(max_output_bytes), str(output_path)])
        return argv

    def build_thumbnail_argv(self, input_path: Path, output_path: Path) -> list[str]:
        return [
            self._ffmpeg(),
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

    def build_gif_argv(
        self,
        input_path: Path,
        output_path: Path,
        *,
        fps: int = 12,
        width: int = 640,
        max_output_bytes: int,
    ) -> list[str]:
        """Build zero-egress high-quality 2-stage palettegen+paletteuse GIF argv."""
        vf = (
            f"fps={max(4, min(30, fps))},scale='min({max(160, min(1280, width))},iw)':-1:flags=lanczos,"
            "split[s0][s1];[s0]palettegen=stats_mode=diff[p];"
            "[s1][p]paletteuse=dither=bayer:bayer_scale=3"
        )
        return [
            self._ffmpeg(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(input_path),
            "-vf",
            vf,
            "-loop",
            "0",
            "-fs",
            str(max_output_bytes),
            str(output_path),
        ]

    def build_animated_webp_argv(
        self,
        input_path: Path,
        output_path: Path,
        *,
        fps: int = 12,
        width: int = 640,
        quality: int = 78,
        max_output_bytes: int,
    ) -> list[str]:
        """Build zero-egress animated WebP argv."""
        vf = f"fps={max(4, min(30, fps))},scale='min({max(160, min(1280, width))},iw)':-1:flags=lanczos"
        return [
            self._ffmpeg(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(input_path),
            "-vf",
            vf,
            "-c:v",
            "libwebp",
            "-lossless",
            "0",
            "-compression_level",
            "4",
            "-q:v",
            str(max(10, min(100, quality))),
            "-loop",
            "0",
            "-an",
            "-fs",
            str(max_output_bytes),
            str(output_path),
        ]

    def build_contact_sheet_argv(
        self,
        input_path: Path,
        output_path: Path,
        *,
        duration_seconds: float | None = None,
        cols: int = 4,
        rows: int = 3,
        tile_width: int = 320,
    ) -> list[str]:
        """Build zero-egress evenly-spaced 4x3 video thumbnail contact sheet argv."""
        total_tiles = max(1, cols * rows)
        dur = max(float(duration_seconds or 12.0), 1.0)
        sample_fps = max(0.01, min(10.0, total_tiles / dur))
        vf = (
            f"fps={sample_fps:.4f},scale={tile_width}:-2:flags=lanczos,"
            f"tile={cols}x{rows}:padding=6:margin=8:color=0x111114"
        )
        return [
            self._ffmpeg(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(input_path),
            "-vf",
            vf,
            "-frames:v",
            "1",
            str(output_path),
        ]

    def build_keyframe_argv(
        self,
        input_path: Path,
        output_path: Path,
        *,
        timestamp_seconds: float = 2.0,
    ) -> list[str]:
        """Build zero-egress single full-resolution PNG keyframe extraction argv."""
        return [
            self._ffmpeg(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-ss",
            f"{max(0.0, timestamp_seconds):.3f}",
            "-i",
            str(input_path),
            "-frames:v",
            "1",
            str(output_path),
        ]

    def build_target_size_argv(
        self,
        input_path: Path,
        output_path: Path,
        *,
        target_size_mb: float,
        duration_seconds: float | None,
        max_output_bytes: int,
    ) -> list[str]:
        """Build zero-egress H.264 MP4 compression argv targeting a maximum MB budget."""
        dur = max(float(duration_seconds or 30.0), 1.0)
        total_kbps = (target_size_mb * 8192.0 * 0.90) / dur
        audio_kbps = 96 if total_kbps >= 320 else 64
        video_kbps = max(120, int(total_kbps - audio_kbps))
        cap_bytes = min(max_output_bytes, int(target_size_mb * 1024 * 1024))
        return [
            self._ffmpeg(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(input_path),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-b:v",
            f"{video_kbps}k",
            "-maxrate",
            f"{int(video_kbps * 1.25)}k",
            "-bufsize",
            f"{video_kbps * 2}k",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            f"{audio_kbps}k",
            "-movflags",
            "+faststart",
            "-fs",
            str(cap_bytes),
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
        audio_bitrate: str = "auto",
        normalize_audio: bool = False,
        max_output_bytes: int,
        cancel_token: CancellationToken | None = None,
    ) -> Path:
        argv = self.build_audio_extract_argv(
            input_path,
            output_path,
            audio_format=audio_format,
            audio_bitrate=audio_bitrate,
            normalize_audio=normalize_audio,
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

    async def process_image(
        self,
        input_path: Path,
        output_path: Path,
        *,
        image_format: str = "webp",
        quality: int = 90,
        max_width: int | None = None,
        strip_metadata: bool = False,
        cancel_token: CancellationToken | None = None,
    ) -> Path:
        """Convert, resize, and/or strip EXIF metadata using Pillow (falling back to FFmpeg)."""
        try:
            from PIL import Image, ImageOps
        except ImportError:
            Image = None  # type: ignore[assignment]
            ImageOps = None  # type: ignore[assignment]

        if Image is not None and image_format in {"jpg", "jpeg", "png", "webp"}:
            import asyncio

            def _convert() -> Path:
                with Image.open(input_path) as img:
                    img = ImageOps.exif_transpose(img)
                    if max_width is not None and max_width > 0 and img.width > max_width:
                        ratio = max_width / float(img.width)
                        new_h = max(2, round(img.height * ratio))
                        img = img.resize((max_width, new_h), Image.Resampling.LANCZOS)

                    fmt = image_format.lower()
                    # Saving without exif= / icc_profile= cleanly strips GPS & camera metadata
                    if fmt in {"jpg", "jpeg"}:
                        if img.mode not in {"RGB", "L"}:
                            img = img.convert("RGB")
                        img.save(output_path, format="JPEG", quality=max(1, min(100, quality)), optimize=True)
                    elif fmt == "webp":
                        img.save(output_path, format="WEBP", quality=max(1, min(100, quality)), method=4)
                    else:
                        img.save(output_path, format="PNG", optimize=True)
                return output_path

            try:
                return await asyncio.to_thread(_convert)
            except Exception as exc:
                raise AdapterError(
                    ErrorCode.ERR_VALIDATION_FAILED,
                    f"Image processing failed: {exc}",
                    engine_id=self.id,
                ) from exc

        argv = self.build_image_process_argv(
            input_path,
            output_path,
            image_format=image_format,
            quality=quality,
            max_width=max_width,
            strip_metadata=strip_metadata,
        )
        res = await run_safe_subprocess(
            argv,
            timeout=60.0,
            proxy_url=None,
            cancel_token=cancel_token,
        )
        if res.returncode != 0 or not output_path.exists():
            raise AdapterError(
                ErrorCode.ERR_INTERNAL,
                f"FFmpeg image processing failed: {res.stderr[-300:]}",
                engine_id=self.id,
            )
        return output_path

    async def remux_faststart(
        self,
        input_path: Path,
        output_path: Path,
        *,
        max_output_bytes: int,
        cancel_token: CancellationToken | None = None,
    ) -> Path:
        argv = self.build_remux_argv(
            input_path,
            output_path,
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
                f"FFmpeg faststart remux failed: {res.stderr[-300:]}",
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

    async def create_gif(
        self,
        input_path: Path,
        output_path: Path,
        *,
        fps: int = 12,
        width: int = 640,
        max_output_bytes: int,
        cancel_token: CancellationToken | None = None,
    ) -> Path:
        argv = self.build_gif_argv(
            input_path,
            output_path,
            fps=fps,
            width=width,
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
                f"GIF creation failed: {res.stderr[-300:]}",
                engine_id=self.id,
            )
        return output_path

    async def create_animated_webp(
        self,
        input_path: Path,
        output_path: Path,
        *,
        fps: int = 12,
        width: int = 640,
        quality: int = 78,
        max_output_bytes: int,
        cancel_token: CancellationToken | None = None,
    ) -> Path:
        argv = self.build_animated_webp_argv(
            input_path,
            output_path,
            fps=fps,
            width=width,
            quality=quality,
            max_output_bytes=max_output_bytes,
        )
        res = await run_safe_subprocess(
            argv,
            timeout=float(self._settings.ffmpeg_timeout_seconds),
            proxy_url=None,
            cancel_token=cancel_token,
        )
        if res.returncode == 0 and output_path.exists():
            return output_path

        # Fallback when system FFmpeg lacks libwebp: extract frames via zero-egress FFmpeg & encode via Pillow
        import asyncio
        import shutil as _shutil

        try:
            from PIL import Image
        except ImportError as exc:
            raise AdapterError(
                ErrorCode.ERR_INTERNAL,
                f"Animated WebP creation failed: {res.stderr[-300:]}",
                engine_id=self.id,
            ) from exc

        frames_dir = output_path.parent / f".webp_frames_{output_path.stem}"
        frames_dir.mkdir(parents=True, exist_ok=True)
        try:
            eff_fps = max(4, min(30, fps))
            eff_w = max(160, min(1280, width))
            frame_argv = [
                self._ffmpeg(),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(input_path),
                "-vf",
                f"fps={eff_fps},scale='min({eff_w},iw)':-2:flags=lanczos",
                "-frames:v",
                "240",
                str(frames_dir / "f_%04d.png"),
            ]
            fres = await run_safe_subprocess(
                frame_argv,
                timeout=float(self._settings.ffmpeg_timeout_seconds),
                proxy_url=None,
                cancel_token=cancel_token,
            )
            frame_paths = sorted(frames_dir.glob("f_*.png"))
            if fres.returncode != 0 or not frame_paths:
                raise AdapterError(
                    ErrorCode.ERR_INTERNAL,
                    f"Animated WebP frame extraction failed: {fres.stderr[-300:]}",
                    engine_id=self.id,
                )

            def _encode_webp() -> Path:
                imgs = [Image.open(p).convert("RGB") for p in frame_paths]
                try:
                    imgs[0].save(
                        output_path,
                        format="WEBP",
                        save_all=True,
                        append_images=imgs[1:],
                        duration=max(20, round(1000 / eff_fps)),
                        loop=0,
                        quality=max(10, min(100, quality)),
                        method=4,
                    )
                finally:
                    for im in imgs:
                        im.close()
                return output_path

            return await asyncio.to_thread(_encode_webp)
        finally:
            _shutil.rmtree(frames_dir, ignore_errors=True)

    async def create_contact_sheet(
        self,
        input_path: Path,
        output_path: Path,
        *,
        duration_seconds: float | None = None,
        cols: int = 4,
        rows: int = 3,
        tile_width: int = 320,
        cancel_token: CancellationToken | None = None,
    ) -> Path:
        argv = self.build_contact_sheet_argv(
            input_path,
            output_path,
            duration_seconds=duration_seconds,
            cols=cols,
            rows=rows,
            tile_width=tile_width,
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
                f"Contact sheet generation failed: {res.stderr[-300:]}",
                engine_id=self.id,
            )
        return output_path

    async def extract_keyframe(
        self,
        input_path: Path,
        output_path: Path,
        *,
        timestamp_seconds: float = 2.0,
        cancel_token: CancellationToken | None = None,
    ) -> Path:
        argv = self.build_keyframe_argv(
            input_path,
            output_path,
            timestamp_seconds=timestamp_seconds,
        )
        res = await run_safe_subprocess(
            argv,
            timeout=60.0,
            proxy_url=None,
            cancel_token=cancel_token,
        )
        if res.returncode != 0 or not output_path.exists():
            raise AdapterError(
                ErrorCode.ERR_INTERNAL,
                f"Keyframe extraction failed: {res.stderr[-300:]}",
                engine_id=self.id,
            )
        return output_path

    async def compress_to_target_size(
        self,
        input_path: Path,
        output_path: Path,
        *,
        target_size_mb: float,
        duration_seconds: float | None,
        max_output_bytes: int,
        cancel_token: CancellationToken | None = None,
    ) -> Path:
        argv = self.build_target_size_argv(
            input_path,
            output_path,
            target_size_mb=target_size_mb,
            duration_seconds=duration_seconds,
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
                f"Target-size compression failed: {res.stderr[-300:]}",
                engine_id=self.id,
            )
        return output_path

    async def health_check(self) -> HealthReport:
        ff = find_binary(self._settings.ffmpeg_bin)
        fp = find_binary(self._settings.ffprobe_bin)
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
