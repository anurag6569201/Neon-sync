"""Tests for the Webpage Reader/Assets, Podcast/RSS Feed, and FFmpeg Creative Studio engines."""

from __future__ import annotations

import shutil
import struct
import threading
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from neon_sync.adapters import (
    DirectHttpAdapter,
    DownloadArtifact,
    FfmpegAdapter,
    PlaywrightFallbackAdapter,
    UrlContext,
)
from neon_sync.api.branding import build_public_capabilities, sanitize_public_text
from neon_sync.config import Settings
from neon_sync.models import (
    ContentType,
    DownloadPlan,
    DownloadRequest,
    DownloadStatus,
    ProbeOptions,
)
from neon_sync.pipeline import ExtractionOrchestrator, LocalArtifactStorage
from neon_sync.registry import EngineRegistry
from neon_sync.security import run_safe_subprocess, validate_and_canonicalize_url


def _make_png_bytes(width: int = 24, height: int = 24) -> bytes:
    def _chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + (b"\xff\x4d\x00" * width) for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


_SAMPLE_PNG = _make_png_bytes()

_SAMPLE_ARTICLE_HTML = """<!doctype html>
<html>
<head>
  <title>Building Spatial Media Tools</title>
  <meta name="author" content="Anurag" />
  <meta property="og:title" content="Building Spatial Media Tools" />
</head>
<body>
  <nav>Home | Pricing | Login</nav>
  <article>
    <h1>Building Spatial Media Tools</h1>
    <p>Neon Sync transforms any web link into an interactive branching graph of single-purpose outputs.
    Instead of guessing formats up front, it probes the real content first and reveals only the capabilities
    actually backed by the source stream or document.</p>
    <p>This architecture eliminates wasted bandwidth, prevents rate-limit collisions on caption downloads,
    and lets creators extract clean articles, vector logos, animated clips, and compressed MP4s in one click.</p>
    <svg width="32" height="32" viewBox="0 0 32 32"><circle cx="16" cy="16" r="14" fill="#ff4d00"/></svg>
    <img src="/hero.png" width="24" height="24" alt="Hero" />
  </article>
</body>
</html>
"""

_SAMPLE_PODCAST_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Neon Studio Cast</title>
    <description>Deep dives into media engineering.</description>
    <itunes:author>Neon Crew</itunes:author>
    <itunes:image href="/cover.png" />
    <item>
      <title>Episode 1: Zero-Latency Streaming</title>
      <guid>ep-001</guid>
      <itunes:duration>01:05:30</itunes:duration>
      <enclosure url="/ep1.mp3" length="123456" type="audio/mpeg" />
    </item>
    <item>
      <title>Episode 2: Spatial Canvas UI</title>
      <guid>ep-002</guid>
      <itunes:duration>1845</itunes:duration>
      <enclosure url="/ep2.mp3" length="98765" type="audio/mpeg" />
    </item>
  </channel>
</rss>
"""


class _WebAndFeedHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_HEAD(self) -> None:
        if self.path == "/article":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
        elif self.path == "/podcast.rss":
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
            self.end_headers()
        elif self.path in {"/hero.png", "/cover.png"}:
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(_SAMPLE_PNG)))
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/article":
            body = _SAMPLE_ARTICLE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/podcast.rss":
            body = _SAMPLE_PODCAST_RSS.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in {"/hero.png", "/cover.png"}:
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(_SAMPLE_PNG)))
            self.end_headers()
            self.wfile.write(_SAMPLE_PNG)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture()
def web_and_feed_server():
    server = HTTPServer(("127.0.0.1", 0), _WebAndFeedHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


async def test_webpage_reader_markdown_text_and_assets_zip(web_and_feed_server: str, tmp_path: Path):
    settings = Settings(allow_private_addresses=True)
    adapter = DirectHttpAdapter(settings)
    url = f"{web_and_feed_server}/article"
    ctx = UrlContext(validated=validate_and_canonicalize_url(url, allow_private=True), job_id="j_web")

    probed = await adapter.probe(ctx, ProbeOptions())
    assert probed.content_type == ContentType.WEBPAGE
    stats = probed.metadata.extra["webpage_stats"]
    assert stats["has_article"] is True
    assert stats["word_count"] >= 35
    assert stats["reading_minutes"] >= 1
    assert "branching graph" in stats["excerpt"]
    assert stats["svg_count"] == 1
    assert stats["image_count"] == 1
    assets_prev = probed.metadata.extra["webpage_assets_preview"]
    assert len(assets_prev["svgs"]) == 1
    assert len(assets_prev["images"]) == 1 and assets_prev["images"][0].endswith("/hero.png")

    # 1. Clean Markdown
    md_art = await adapter.download(
        ctx,
        DownloadPlan(job_id="j_md", output_dir=tmp_path / "md", mode="webpage", webpage_format="markdown"),
    )
    assert md_art.filename.endswith(".md")
    md_text = md_art.local_path.read_text(encoding="utf-8")
    assert "# Building Spatial Media Tools" in md_text
    assert "branching graph" in md_text

    # 2. Plain text
    txt_art = await adapter.download(
        ctx,
        DownloadPlan(job_id="j_txt", output_dir=tmp_path / "txt", mode="webpage", webpage_format="text"),
    )
    assert txt_art.filename.endswith(".txt")
    assert "branching graph" in txt_art.local_path.read_text(encoding="utf-8")

    # 3. Assets ZIP (inline SVG + verified PNG image)
    zip_art = await adapter.download(
        ctx,
        DownloadPlan(job_id="j_zip", output_dir=tmp_path / "zip", mode="webpage", webpage_format="assets_zip"),
    )
    assert zip_art.mime_type == "application/zip"
    with zipfile.ZipFile(zip_art.local_path, "r") as zf:
        names = set(zf.namelist())
        assert "svgs/vector_01.svg" in names
        assert "images/hero.png" in names
        assert 'xmlns="http://www.w3.org/2000/svg"' in zf.read("svgs/vector_01.svg").decode("utf-8")


async def test_podcast_rss_feed_probe_xxe_safe_and_markdown_index(web_and_feed_server: str, tmp_path: Path):
    settings = Settings(allow_private_addresses=True)
    adapter = DirectHttpAdapter(settings)
    url = f"{web_and_feed_server}/podcast.rss"
    ctx = UrlContext(validated=validate_and_canonicalize_url(url, allow_private=True), job_id="j_pod")

    probed = await adapter.probe(ctx, ProbeOptions())
    assert probed.content_type == ContentType.PLAYLIST
    assert probed.metadata.extra["feed_type"] == "podcast"
    assert probed.title == "Neon Studio Cast"
    assert len(probed.items) == 2
    assert probed.items[0].duration_seconds == 3930.0
    assert probed.items[1].duration_seconds == 1845.0
    assert probed.items[0].direct_media_url is not None and probed.items[0].direct_media_url.endswith("/ep1.mp3")
    assert probed.thumbnail is not None and probed.thumbnail.url.endswith("/cover.png")
    # XXE entity must NOT expand
    assert "root:" not in probed.model_dump_json()

    md_art = await adapter.download(
        ctx,
        DownloadPlan(job_id="j_pod_md", output_dir=tmp_path / "pod", mode="webpage", webpage_format="markdown"),
    )
    md = md_art.local_path.read_text(encoding="utf-8")
    assert "# Neon Studio Cast" in md
    assert "Zero-Latency Streaming" in md


def test_ffmpeg_studio_argv_security_invariants(tmp_path: Path):
    ff = FfmpegAdapter(Settings())
    src = tmp_path / "in.mp4"

    for argv in (
        ff.build_gif_argv(src, tmp_path / "out.gif", fps=12, width=480, max_output_bytes=5_000_000),
        ff.build_animated_webp_argv(src, tmp_path / "out.webp", fps=12, width=480, max_output_bytes=5_000_000),
        ff.build_contact_sheet_argv(src, tmp_path / "sheet.png", duration_seconds=60.0),
        ff.build_keyframe_argv(src, tmp_path / "frame.png", timestamp_seconds=4.5),
        ff.build_target_size_argv(src, tmp_path / "comp.mp4", target_size_mb=8.0, duration_seconds=30.0, max_output_bytes=20_000_000),
        ff.build_image_process_argv(tmp_path / "in.jpg", tmp_path / "out.jpg", image_format="jpg", strip_metadata=True),
    ):
        assert argv.index("-protocol_whitelist") < argv.index("-i")
        assert argv[argv.index("-protocol_whitelist") + 1] == "file,pipe"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_ffmpeg_studio_live_gif_webp_sheet_keyframe_compress_and_exif_strip(tmp_path: Path):
    ff = FfmpegAdapter(Settings())
    sample_mp4 = tmp_path / "sample.mp4"

    # Generate a tiny 2-second test video locally with FFmpeg lavfi
    gen = await run_safe_subprocess(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x180:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(sample_mp4),
        ],
        timeout=30.0,
    )
    assert gen.returncode == 0 and sample_mp4.exists()

    gif = await ff.create_gif(sample_mp4, tmp_path / "out.gif", fps=8, width=240, max_output_bytes=5_000_000)
    assert gif.read_bytes()[:6] in {b"GIF89a", b"GIF87a"}

    webp = await ff.create_animated_webp(sample_mp4, tmp_path / "out.webp", fps=8, width=240, max_output_bytes=5_000_000)
    assert webp.read_bytes()[:4] == b"RIFF"

    kf = await ff.extract_keyframe(sample_mp4, tmp_path / "frame.png", timestamp_seconds=0.5)
    assert kf.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    sheet = await ff.create_contact_sheet(sample_mp4, tmp_path / "sheet.png", duration_seconds=2.0, cols=2, rows=2, tile_width=160)
    assert sheet.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    comp = await ff.compress_to_target_size(
        sample_mp4,
        tmp_path / "discord.mp4",
        target_size_mb=8.0,
        duration_seconds=2.0,
        max_output_bytes=10_000_000,
    )
    assert comp.exists() and comp.stat().st_size > 1000 and comp.stat().st_size <= 8 * 1024 * 1024

    # Pillow EXIF privacy strip test
    from PIL import Image

    jpg_with_exif = tmp_path / "gps.jpg"
    im = Image.new("RGB", (32, 32), color=(255, 77, 0))
    exif = im.getexif()
    exif[0x010F] = "SecretCameraMake"
    im.save(jpg_with_exif, format="JPEG", exif=exif)
    assert b"SecretCameraMake" in jpg_with_exif.read_bytes()

    cleaned = await ff.process_image(jpg_with_exif, tmp_path / "clean.jpg", image_format="jpg", strip_metadata=True)
    assert b"SecretCameraMake" not in cleaned.read_bytes()


async def test_orchestrator_webpage_snapshot_dispatch_and_zero_leak_branding(
    web_and_feed_server: str,
    tmp_path: Path,
):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        allow_private_addresses=True,
        min_free_disk_bytes=1024,
    )
    direct = DirectHttpAdapter(settings)
    pw = PlaywrightFallbackAdapter(settings)
    reg = EngineRegistry()
    reg.register_extractor(direct)
    reg.register_extractor(pw)
    orch = ExtractionOrchestrator(settings, reg, LocalArtifactStorage(settings))

    async def _fake_capture(ctx, out_dir: Path, *, fmt: str = "pdf", **kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / ("webpage_print.pdf" if fmt == "pdf" else "webpage_full.png")
        out.write_bytes(b"%PDF-1.7\nRendered" if fmt == "pdf" else _SAMPLE_PNG)
        return DownloadArtifact(
            local_path=out,
            filename=out.name,
            mime_type="application/pdf" if fmt == "pdf" else "image/png",
            size_bytes=out.stat().st_size,
        )

    with patch.object(pw, "capture_webpage", new=AsyncMock(side_effect=_fake_capture)) as mock_cap:
        res = await orch.execute_download(
            "job_pdf_snap",
            DownloadRequest(
                url=f"{web_and_feed_server}/article",
                mode="webpage",
                webpage_format="pdf",
            ),
        )
    assert res.download.status == DownloadStatus.COMPLETED
    assert res.download.filename is not None and res.download.filename.endswith(".pdf")
    assert mock_cap.await_count == 1

    # Branding sanitizer scrubs new internal library tokens too
    cleaned = sanitize_public_text("trafilatura and beautifulsoup4 with lxml and pillow failed")
    for tok in ("trafilatura", "beautifulsoup", "lxml", "pillow"):
        assert tok not in cleaned.lower()

    caps = build_public_capabilities(reg.list_manifests())
    assert any(c.id == "neon-web-reader" and c.status == "operational" for c in caps)


async def test_api_live_subtitle_preview_endpoint(tmp_path: Path):
    import json
    from unittest.mock import MagicMock

    import httpx

    from neon_sync.api import create_app
    from neon_sync.models import EngineProvenance, NormalizedMediaOutput, SourceInfo, SubtitleTrack

    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        allow_private_addresses=False,
        min_free_disk_bytes=1024,
    )
    app = create_app(settings)
    stub_probe = NormalizedMediaOutput(
        job_id="p_sub_prev",
        original_url="https://youtu.be/YW7Uc5_ixtM",
        url="https://www.youtube.com/watch?v=YW7Uc5_ixtM",
        source=SourceInfo(domain="youtu.be", platform="youtube"),
        content_type=ContentType.VIDEO,
        title="Caption Preview Video",
        subtitles=[
            SubtitleTrack(
                language="en",
                name="English",
                is_auto_generated=False,
                formats=["json3"],
                url="https://www.youtube.com/api/timedtext?v=YW7Uc5_ixtM&lang=en&fmt=json3",
            )
        ],
        engine=EngineProvenance(engine_used="yt-dlp", engine_version="1.0"),
    )
    json3_payload = json.dumps(
        {
            "events": [
                {"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "Welcome to Neon Sync!"}]},
                {"tStartMs": 4500, "dDurationMs": 2500, "segs": [{"utf8": "Searchable live captions."}]},
            ]
        }
    )
    mock_resp = MagicMock(status_code=200, text=json3_payload, content=json3_payload.encode())
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.get = AsyncMock(return_value=mock_resp)

    real_async_client = httpx.AsyncClient
    with (
        patch.object(app.state.container.orchestrator, "probe", new=AsyncMock(return_value=stub_probe)),
        patch("neon_sync.api.routes.httpx.AsyncClient", return_value=mock_client),
    ):
        async with real_async_client(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/v1/preview/subtitles",
                json={"url": "https://youtu.be/YW7Uc5_ixtM", "language": "en"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["cue_count"] == 2
            assert data["cues"][0]["start"] == "00:01"
            assert "Welcome to Neon Sync!" in data["plain_text"]
            assert "Searchable live captions." in data["plain_text"]

