"""Tests for Neon Sync white-label API surface, zero engine leakage, and hidden developer surfaces."""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from neon_sync.api import create_app
from neon_sync.api.branding import (
    brand_media_output,
    sanitize_public_text,
)
from neon_sync.config import Settings
from neon_sync.models import (
    ContentType,
    DownloadInfo,
    DownloadStatus,
    EngineProvenance,
    ErrorCode,
    ErrorInfo,
    MediaMetadata,
    NormalizedMediaOutput,
    SourceInfo,
    WarningInfo,
)

FORBIDDEN_PUBLIC_TOKENS = (
    "yt-dlp",
    "ytdlp",
    "gallery-dl",
    "gallery_dl",
    "streamlink",
    "n-m3u8dl-re",
    "nm3u8dl",
    "aria2",
    "playwright",
    "ffmpeg",
    "ffprobe",
    "direct-http",
)


class _SamplePdfHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", "22")
        self.end_headers()

    def do_GET(self) -> None:
        body = b"%PDF-1.7\nNeon Sync OK!"
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def sample_pdf_url() -> str:
    server = HTTPServer(("127.0.0.1", 0), _SamplePdfHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/brief.pdf"
    finally:
        server.shutdown()
        server.server_close()


def _assert_zero_engine_leak(payload: str, *, context: str) -> None:
    low = payload.lower()
    for token in FORBIDDEN_PUBLIC_TOKENS:
        assert token not in low, f"Leaked internal engine token {token!r} in {context}"


def test_branding_sanitizer_and_media_output_scrubbing():
    raw_msg = (
        "Engine 'yt-dlp' failed (rc=1) at /Users/dev/scratch/jobs/job_1/out.mp4; "
        "falling back to gallery-dl and ffmpeg post-processor."
    )
    cleaned = sanitize_public_text(raw_msg)
    _assert_zero_engine_leak(cleaned, context="sanitize_public_text")
    assert "/Users/dev" not in cleaned
    assert "(rc=1)" not in cleaned
    assert "Neon Sync" in cleaned

    internal = NormalizedMediaOutput(
        job_id="job_brand_1",
        original_url="https://example.com/watch?v=1",
        url="https://example.com/watch?v=1",
        source=SourceInfo(domain="example.com", platform="youtube", extractor_key="Youtube"),
        content_type=ContentType.VIDEO,
        title="Demo",
        metadata=MediaMetadata(extra={"ffprobe": {"video_codec": "h264", "width": 1920, "height": 1080}}),
        download=DownloadInfo(status=DownloadStatus.COMPLETED),
        engine=EngineProvenance(
            engine_used="yt-dlp",
            engine_version="2026.10.26",
            post_processor="ffmpeg-7.1",
            fallback_chain=["yt-dlp", "gallery-dl"],
        ),
        warnings=[
            WarningInfo(code="WARN_ENGINE_FALLBACK", message="Engine 'yt-dlp' skipped", engine="yt-dlp"),
            WarningInfo(code="WARN_FFPROBE_SKIPPED", message="ffprobe timeout", engine="ffmpeg"),
        ],
        errors=[
            ErrorInfo(code=ErrorCode.ERR_INTERNAL, message="aria2c exited with code 1", engine="aria2"),
        ],
    )

    branded = brand_media_output(internal)
    dumped = branded.model_dump_json()
    _assert_zero_engine_leak(dumped, context="brand_media_output")
    assert branded.engine.engine_used == "neon-sync"
    assert branded.engine.post_processor == "neon-sync-media"
    assert branded.engine.fallback_chain == ["neon-sync"]
    assert "media_analysis" in branded.metadata.extra
    assert "ffprobe" not in branded.metadata.extra
    # WARN_ENGINE_FALLBACK dropped; remaining warning branded
    assert len(branded.warnings) == 1
    assert branded.warnings[0].code == "WARN_MEDIA_PROBE_SKIPPED"
    assert branded.warnings[0].engine == "neon-sync"


async def test_public_api_zero_leak_headers_and_hidden_dev_surfaces(
    sample_pdf_url: str,
    tmp_path: Path,
):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        allow_private_addresses=True,
        min_free_disk_bytes=1024,
        pro_api_keys="neon-pro-secret-9999",
    )
    app = create_app(settings)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # 1. Developer console, OpenAPI docs, internal engine list, AI agent & automations are NOT exposed
        for hidden_path in (
            "/docs",
            "/redoc",
            "/openapi.json",
            "/v1/engines",
            "/v1/status",
            "/v1/logs",
            "/v1/logs/stream",
            "/v1/automations",
        ):
            assert (await client.get(hidden_path)).status_code == 404, f"Expected 404 for {hidden_path}"
        assert (await client.post("/v1/agent/run", json={"prompt": "test"})).status_code == 404

        # 2. Web UI static assets keep Spatial Graph UI with zero third-party engine leaks & no dev console/AI
        html = await client.get("/")
        js = await client.get("/static/app.js")
        assert html.status_code == 200 and js.status_code == 200
        _assert_zero_engine_leak(html.text, context="index.html")
        _assert_zero_engine_leak(js.text, context="app.js")
        assert "dev-console-modal" not in html.text
        assert "automations-modal" not in html.text
        assert "gravity-field" in html.text
        assert "threads-svg" in html.text

        # 3. Health response carries Neon Sync white-label headers & zero engine names
        health = await client.get("/v1/health")
        assert health.status_code == 200
        assert health.headers["x-powered-by"] == "Neon Sync"
        assert health.headers["x-request-id"].startswith("ns_req_")
        assert "x-neon-sync-version" in health.headers
        assert "x-response-time-ms" in health.headers
        assert "server" not in health.headers
        _assert_zero_engine_leak(health.text, context="/v1/health")

        # 4. Probe & Job responses have zero engine leakage
        probe = await client.post(
            "/v1/probe",
            headers={"X-Neon-Pro-Key": "neon-pro-secret-9999"},
            json={"url": sample_pdf_url},
        )
        assert probe.status_code == 200
        _assert_zero_engine_leak(probe.text, context="/v1/probe")
        assert probe.json()["engine"]["engine_used"] == "neon-sync"

        job_resp = await client.post(
            "/v1/jobs",
            headers={"X-Neon-Pro-Key": "neon-pro-secret-9999"},
            json={"url": sample_pdf_url},
        )
        assert job_resp.status_code == 202
        job_id = job_resp.json()["job_id"]

        for _ in range(40):
            jr = await client.get(f"/v1/jobs/{job_id}")
            if jr.json()["status"] in {"completed", "failed"}:
                _assert_zero_engine_leak(jr.text, context=f"/v1/jobs/{job_id}")
                break
            await asyncio.sleep(0.05)
