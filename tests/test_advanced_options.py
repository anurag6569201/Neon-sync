"""Tests for advanced format catalog, yt-dlp selectors, FFmpeg image/audio processing & batch jobs."""

from __future__ import annotations

import shutil
import struct
import zlib
from pathlib import Path

import httpx
import pytest

from neon_sync.adapters import FfmpegAdapter, GalleryDlAdapter, YtDlpAdapter
from neon_sync.api import create_app
from neon_sync.config import Settings
from neon_sync.models import DownloadPlan, build_ytdlp_format_selector, get_format_catalog


def _make_tiny_png(width: int = 32, height: int = 16) -> bytes:
    """Generate a valid minimal RGB PNG in pure Python for live FFmpeg image tests."""
    def _chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + (b"\x38\xbd\xf8" * width) for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def test_format_catalog_and_ytdlp_format_selectors():
    cat = get_format_catalog()
    assert any(q.id == "2160" for q in cat.video_qualities)
    assert any(c.id == "h264" for c in cat.video_codecs)
    assert any(f.id == "webp" for f in cat.image_formats)
    assert len(cat.presets) >= 5

    # Exact format_id takes precedence
    assert build_ytdlp_format_selector(format_id="299+140") == "299+140"
    # Audio mode
    assert build_ytdlp_format_selector(mode="audio") == "bestaudio/best"
    # 1080p H.264 60fps MP4
    sel = build_ytdlp_format_selector(
        mode="video",
        video_quality="1080",
        video_codec="h264",
        video_container="mp4",
        fps_limit=60,
    )
    assert "[height<=1080][fps<=60][vcodec^=avc]" in sel
    assert "bestaudio[ext=m4a]" in sel
    assert sel.endswith("bestvideo+bestaudio/best")


def test_ytdlp_and_gallery_dl_advanced_argv_flags(tmp_path: Path):
    settings = Settings(egress_proxy_url="http://127.0.0.1:3128")
    yt = YtDlpAdapter(settings)
    plan = DownloadPlan(
        job_id="job_adv_1",
        output_dir=tmp_path,
        mode="video",
        video_quality="720",
        video_codec="h264",
        video_container="mp4",
        fps_limit=30,
        extract_subtitles=True,
        embed_subtitles=True,
        subtitle_languages=["en", "es"],
        subtitle_mode="include_auto",
        subtitle_format="srt",
        embed_metadata=True,
        embed_thumbnail=True,
        playlist_items="1-3",
    )
    argv = yt.build_download_argv("https://www.youtube.com/watch?v=abc", plan)
    assert "--merge-output-format" in argv and "mp4" in argv
    assert "--write-subs" in argv and "--write-auto-subs" in argv and "--embed-subs" in argv
    assert "en,es" in argv
    assert "--embed-metadata" in argv and "--embed-thumbnail" in argv
    assert "--playlist-items" in argv and "1-3" in argv

    gal = GalleryDlAdapter(settings)
    gal_plan = DownloadPlan(
        job_id="job_gal_adv",
        output_dir=tmp_path,
        mode="image",
        gallery_range_start=5,
        gallery_range_end=20,
        gallery_archive_format="tar",
    )
    g_argv = gal.build_download_argv("https://imgur.com/a/demo", gal_plan)
    assert "--range" in g_argv and "5-20" in g_argv


def test_ffmpeg_advanced_image_and_audio_security_invariants(tmp_path: Path):
    ff = FfmpegAdapter(Settings())

    img_argv = ff.build_image_process_argv(
        tmp_path / "in.png",
        tmp_path / "out.webp",
        image_format="webp",
        quality=85,
        max_width=1280,
    )
    assert img_argv.index("-protocol_whitelist") < img_argv.index("-i")
    assert img_argv[img_argv.index("-protocol_whitelist") + 1] == "file,pipe"
    assert "scale='min(1280,iw)':-2" in img_argv
    assert "libwebp" in img_argv

    aud_argv = ff.build_audio_extract_argv(
        tmp_path / "in.webm",
        tmp_path / "out.mp3",
        audio_format="mp3",
        audio_bitrate="320k",
        normalize_audio=True,
        max_output_bytes=10_000_000,
    )
    assert aud_argv.index("-protocol_whitelist") < aud_argv.index("-i")
    assert "-af" in aud_argv and "loudnorm" in aud_argv
    assert "-b:a" in aud_argv and "320k" in aud_argv


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_ffmpeg_live_image_png_to_webp_resize(tmp_path: Path):
    ff = FfmpegAdapter(Settings())
    src = tmp_path / "input.png"
    dst = tmp_path / "output.webp"
    src.write_bytes(_make_tiny_png(64, 32))

    out = await ff.process_image(src, dst, image_format="webp", quality=85, max_width=32)
    assert out.exists()
    assert out.read_bytes()[:4] == b"RIFF"
    info = await ff.probe_file(out)
    assert info["width"] == 32
    assert info["height"] == 16


async def test_api_formats_endpoint_and_batch_validation(tmp_path: Path):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        allow_private_addresses=False,
        min_free_disk_bytes=1024,
    )
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        fmt = await client.get("/v1/formats")
        assert fmt.status_code == 200
        data = fmt.json()
        assert len(data["presets"]) >= 5
        assert any(c["id"] == "h264" for c in data["video_codecs"])

        # Batch rejects entire batch if ANY URL violates SSRF
        bad_batch = await client.post(
            "/v1/jobs/batch",
            json={
                "urls": ["https://example.com/ok.pdf", "http://169.254.169.254/latest/meta-data/"],
                "video_quality": "1080",
            },
        )
        assert bad_batch.status_code == 403
        assert bad_batch.json()["detail"]["code"] == "ERR_SSRF_BLOCKED"
