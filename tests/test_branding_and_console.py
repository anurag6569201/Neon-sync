"""Tests for Neon Sync white-label API surface, zero engine leakage, and Developer Console logs."""

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


async def test_public_api_zero_leak_headers_logs_and_console(
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
        # 1. OpenAPI schema & Web UI static assets have zero third-party engine leaks
        openapi = await client.get("/openapi.json")
        assert openapi.status_code == 200
        _assert_zero_engine_leak(openapi.text, context="/openapi.json")
        assert "/v1/engines" not in openapi.text
        assert "preferred_engine" not in openapi.text
        assert "/v1/status" in openapi.text
        assert "/v1/logs" in openapi.text

        html = await client.get("/")
        js = await client.get("/static/app.js")
        assert html.status_code == 200 and js.status_code == 200
        _assert_zero_engine_leak(html.text, context="index.html")
        _assert_zero_engine_leak(js.text, context="app.js")
        assert "dev-console-modal" in html.text

        # 2. Health & Platform Status responses carry Neon Sync headers & zero engine names
        health = await client.get("/v1/health")
        assert health.status_code == 200
        assert health.headers["x-powered-by"] == "Neon Sync"
        assert health.headers["x-request-id"].startswith("ns_req_")
        assert "x-neon-sync-version" in health.headers
        assert "x-response-time-ms" in health.headers
        _assert_zero_engine_leak(health.text, context="/v1/health")

        status_resp = await client.get("/v1/status")
        assert status_resp.status_code == 200
        _assert_zero_engine_leak(status_resp.text, context="/v1/status")

        # 3. Probe & Job with Pro secret header -> secret must be masked in /v1/logs!
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

        # 4. Agent SSE stream has zero engine leaks
        agent_resp = await client.post(
            "/v1/agent/run",
            headers={"X-Neon-Pro-Key": "neon-pro-secret-9999"},
            json={"prompt": f"probe {sample_pdf_url}"},
        )
        assert agent_resp.status_code == 200
        _assert_zero_engine_leak(agent_resp.text, context="/v1/agent/run")

        # 5. Developer Console /v1/logs returns structured, secret-masked, zero-leak telemetry
        logs_resp = await client.get("/v1/logs?limit=100")
        assert logs_resp.status_code == 200
        logs_text = logs_resp.text
        _assert_zero_engine_leak(logs_text, context="/v1/logs")
        assert "neon-pro-secret-9999" not in logs_text  # Secret must be masked!
        assert "neon••••9999" in logs_text

        ldata = logs_resp.json()
        assert ldata["service"] == "neon-sync"
        assert ldata["metrics"]["total_requests"] >= 4
        assert any(e["path"] == "/v1/probe" and e["event"] == "media.probe.completed" for e in ldata["logs"])
        assert any(e["method"] == "PIPELINE" and e["job_id"] == job_id for e in ldata["logs"])

        # Filter logs by status & search query
        pipe_logs = await client.get("/v1/logs?status=pipeline")
        assert pipe_logs.status_code == 200
        assert all(e["method"] == "PIPELINE" for e in pipe_logs.json()["logs"])

        # Clear logs works cleanly
        cleared = await client.delete("/v1/logs")
        assert cleared.status_code == 200
        assert cleared.json()["cleared"] >= 1
        assert (await client.get("/v1/logs")).json()["returned"] == 0
