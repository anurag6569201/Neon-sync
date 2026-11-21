"""Tests for DirectHttp, Aria2, YtDlp, GalleryDl, and Ffmpeg adapters."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from neon_sync.adapters import (
    AdapterError,
    Aria2Adapter,
    DirectHttpAdapter,
    FfmpegAdapter,
    GalleryDlAdapter,
    UrlContext,
    YtDlpAdapter,
)
from neon_sync.adapters.gallery_dl import parse_gallery_dl_json
from neon_sync.adapters.ytdlp import classify_ytdlp_error, normalize_ytdlp_json
from neon_sync.config import Settings
from neon_sync.models import ContentType, DownloadPlan, ErrorCode, ProbeOptions
from neon_sync.security import validate_and_canonicalize_url


class _LocalMediaHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def do_HEAD(self) -> None:
        if self.path == "/report.pdf":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", "18")
            self.send_header("Content-Disposition", 'attachment; filename="Q4 Report.pdf"')
            self.end_headers()
        elif self.path == "/article":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/report.pdf":
            body = b"%PDF-1.7\nhello pdf"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", 'attachment; filename="Q4 Report.pdf"')
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/malware.bin":
            body = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 32
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/article":
            html = b"""<!DOCTYPE html><html><head>
            <title>Fallback Title</title>
            <meta property="og:title" content="Neon Sync Demo Article" />
            <meta property="og:description" content="A rich media page." />
            <meta property="og:image" content="/thumb.jpg" />
            <script type="application/ld+json">
              {"@type": "VideoObject", "name": "Demo Clip", "contentUrl": "/clip.mp4"}
            </script>
            </head><body><h1>Hello</h1></body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture()
def local_server_url():
    server = HTTPServer(("127.0.0.1", 0), _LocalMediaHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


async def test_direct_http_probe_and_download_pdf(local_server_url: str, tmp_path: Path):
    settings = Settings(allow_private_addresses=True)
    adapter = DirectHttpAdapter(settings)
    validated = validate_and_canonicalize_url(f"{local_server_url}/report.pdf", allow_private=True)
    ctx = UrlContext(validated=validated, job_id="job_pdf_1")

    await adapter.head_inspect(ctx)
    assert ctx.head_content_type == "application/pdf"
    assert ctx.head_filename == "Q4_Report.pdf"

    result = await adapter.probe(ctx, ProbeOptions())
    assert result.content_type == ContentType.DOCUMENT
    assert result.available_formats[0].mime_type == "application/pdf"
    assert result.size_bytes == 18

    plan = DownloadPlan(job_id="job_pdf_1", output_dir=tmp_path)
    artifact = await adapter.download(ctx, plan)
    assert artifact.local_path.exists()
    assert artifact.mime_type == "application/pdf"
    assert artifact.filename == "Q4_Report.pdf"


async def test_direct_http_blocks_elf_executable_download(local_server_url: str, tmp_path: Path):
    settings = Settings(allow_private_addresses=True)
    adapter = DirectHttpAdapter(settings)
    validated = validate_and_canonicalize_url(f"{local_server_url}/malware.bin", allow_private=True)
    ctx = UrlContext(validated=validated, job_id="job_elf_1")

    with pytest.raises(AdapterError) as exc_info:
        await adapter.download(ctx, DownloadPlan(job_id="job_elf_1", output_dir=tmp_path))
    assert exc_info.value.code == ErrorCode.ERR_VALIDATION_FAILED


async def test_direct_http_extracts_og_and_jsonld_video(local_server_url: str):
    settings = Settings(allow_private_addresses=True)
    adapter = DirectHttpAdapter(settings)
    validated = validate_and_canonicalize_url(f"{local_server_url}/article", allow_private=True)
    ctx = UrlContext(validated=validated, job_id="job_art_1")

    result = await adapter.probe(ctx, ProbeOptions())
    assert result.title == "Neon Sync Demo Article"
    assert result.description == "A rich media page."
    assert result.content_type == ContentType.VIDEO
    assert result.thumbnail is not None and result.thumbnail.url.endswith("/thumb.jpg")
    assert result.available_formats[0].direct_media_url.endswith("/clip.mp4")


def test_ytdlp_normalizer_error_classifier_and_safe_argv():
    settings = Settings(egress_proxy_url="http://127.0.0.1:3128")
    adapter = YtDlpAdapter(settings)

    argv = adapter.build_probe_argv("https://www.youtube.com/watch?v=abc", ProbeOptions(max_items=20))
    assert "--ignore-config" in argv
    assert "--no-exec" in argv
    assert "--proxy" in argv
    assert argv[-2] == "--"  # Prevents URL flag injection!

    # Outdated extractor detection
    err_outdated = classify_ytdlp_error("ERROR: Unable to extract nsig player response")
    assert err_outdated.code == ErrorCode.ERR_EXTRACTOR_OUTDATED
    assert err_outdated.outdated is True

    # DRM detection
    err_drm = classify_ytdlp_error("ERROR: This video is DRM protected")
    assert err_drm.code == ErrorCode.ERR_DRM_PROTECTED

    # Normalize real yt-dlp JSON structure
    validated = validate_and_canonicalize_url("https://www.youtube.com/watch?v=abc")
    ctx = UrlContext(validated=validated, job_id="job_yt_1")
    raw = {
        "id": "abc",
        "title": "Test Video",
        "extractor": "youtube",
        "extractor_key": "Youtube",
        "duration": 120,
        "uploader": "Creator",
        "thumbnail": "https://i.ytimg.com/vi/abc/hqdefault.jpg",
        "formats": [
            {
                "format_id": "137",
                "ext": "mp4",
                "width": 1920,
                "height": 1080,
                "vcodec": "avc1.640028",
                "acodec": "none",
                "filesize": 10_000_000,
                "protocol": "https",
            }
        ],
        "subtitles": {"en": [{"ext": "vtt", "url": "https://example.com/en.vtt", "name": "English"}]},
    }
    norm = normalize_ytdlp_json(raw, ctx, version="2026.10.26", probe_ms=450)
    assert norm.content_type == ContentType.VIDEO
    assert norm.available_formats[0].resolution == "1920x1080"
    assert norm.available_formats[0].requires_muxing is True
    assert norm.subtitles[0].language == "en"
    assert norm.transcript is not None and norm.transcript.available is True


def test_gallery_dl_parser_and_safe_argv():
    settings = Settings(egress_proxy_url="http://127.0.0.1:3128", max_gallery_items=50)
    adapter = GalleryDlAdapter(settings)
    argv = adapter.build_probe_argv("https://imgur.com/a/xyz", ProbeOptions(max_items=200))
    assert "--ignore-config" in argv
    assert "1-50" in argv  # Respects server max_gallery_items cap!
    assert argv[-2] == "--"

    validated = validate_and_canonicalize_url("https://imgur.com/a/xyz")
    ctx = UrlContext(validated=validated, job_id="job_gal_1")
    raw_json = json.dumps(
        [
            [2, {"category": "imgur", "title": "Sunset Album", "author": "alice"}],
            [3, "https://i.imgur.com/1.jpg", {"extension": "jpg", "width": 1600, "height": 900}],
            [3, "https://i.imgur.com/2.png", {"extension": "png", "width": 1200, "height": 800}],
        ]
    )
    norm = parse_gallery_dl_json(raw_json, ctx, version="1.32.21", probe_ms=320)
    assert norm.content_type == ContentType.GALLERY
    assert len(norm.items) == 2
    assert norm.title == "Sunset Album"
    assert norm.available_formats[0].container == "zip"


def test_aria2_and_ffmpeg_argv_security_invariants(tmp_path: Path):
    settings = Settings(egress_proxy_url="http://127.0.0.1:3128")

    aria2 = Aria2Adapter(settings)
    a_argv = aria2.build_download_argv("https://cdn.example.com/big.iso", tmp_path, "out.iso")
    assert "--enable-rpc=false" in a_argv
    assert "--follow-torrent=false" in a_argv
    assert "--follow-metalink=false" in a_argv
    assert "--all-proxy" in a_argv
    assert a_argv[-2] == "--"

    ff = FfmpegAdapter(settings)
    remux_argv = ff.build_remux_argv(tmp_path / "in.mkv", tmp_path / "out.mp4", max_output_bytes=50_000_000)
    # CRITICAL: -protocol_whitelist file,pipe MUST appear BEFORE -i!
    wl_idx = remux_argv.index("-protocol_whitelist")
    i_idx = remux_argv.index("-i")
    assert wl_idx < i_idx
    assert remux_argv[wl_idx + 1] == "file,pipe"
