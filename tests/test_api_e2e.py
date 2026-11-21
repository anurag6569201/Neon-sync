"""End-to-end tests for the FastAPI Control Plane, SSE progress, SSRF rejection & safe artifact delivery."""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from neon_sync.api import create_app
from neon_sync.config import Settings


class _E2EMediaHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def do_HEAD(self) -> None:
        if self.path == "/guide.pdf":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", "24")
            self.send_header("Content-Disposition", 'attachment; filename="Neon Guide.pdf"')
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/guide.pdf":
            body = b"%PDF-1.7\nNeon Sync Guide"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", 'attachment; filename="Neon Guide.pdf"')
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture()
def e2e_media_server():
    server = HTTPServer(("127.0.0.1", 0), _E2EMediaHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


async def test_api_health_engines_and_ssrf_blocking(tmp_path: Path):
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
        h = await client.get("/v1/health")
        assert h.status_code == 200
        assert "direct-http" in h.json()["engines_installed"]

        eng = await client.get("/v1/engines")
        assert eng.status_code == 200
        ids = {e["id"] for e in eng.json()}
        assert {
            "direct-http",
            "aria2",
            "yt-dlp",
            "gallery-dl",
            "streamlink",
            "n-m3u8dl-re",
            "playwright",
            "ffmpeg",
        }.issubset(ids)

        # SSRF attempt blocked at ingress with 403
        ssrf = await client.post("/v1/probe", json={"url": "http://169.254.169.254/latest/meta-data/"})
        assert ssrf.status_code == 403
        assert ssrf.json()["detail"]["code"] == "ERR_SSRF_BLOCKED"

        # Invalid scheme blocked with 400
        bad = await client.post("/v1/jobs", json={"url": "file:///etc/passwd"})
        assert bad.status_code == 400
        assert bad.json()["detail"]["code"] == "ERR_INVALID_URL"


async def test_api_full_probe_download_sse_and_safe_artifact_delivery(
    e2e_media_server: str,
    tmp_path: Path,
):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        artifact_base_url="http://testserver",
        allow_private_addresses=True,
        min_free_disk_bytes=1024,
    )
    app = create_app(settings)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        pdf_url = f"{e2e_media_server}/guide.pdf"

        # 1. Probe
        probe = await client.post("/v1/probe", json={"url": pdf_url})
        assert probe.status_code == 200
        pdata = probe.json()
        assert pdata["content_type"] == "document"
        assert pdata["engine"]["engine_used"] == "direct-http"

        # 2. Submit job with idempotency key
        j1 = await client.post("/v1/jobs", json={"url": pdf_url, "idempotency_key": "idem-1"})
        assert j1.status_code == 202
        job_id = j1.json()["job_id"]

        j2 = await client.post("/v1/jobs", json={"url": pdf_url, "idempotency_key": "idem-1"})
        assert j2.json()["job_id"] == job_id

        # 3. Wait for job completion
        job_data = None
        for _ in range(40):
            r = await client.get(f"/v1/jobs/{job_id}")
            job_data = r.json()
            if job_data["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0.05)

        assert job_data is not None
        assert job_data["status"] == "completed"
        dl = job_data["result"]["download"]
        assert dl["filename"] == "Neon_Guide.pdf"
        assert len(dl["sha256"]) == 64

        # 4. SSE progress endpoint returns terminal state cleanly
        sse = await client.get(f"/v1/jobs/{job_id}/events")
        assert sse.status_code == 200
        assert "event: progress" in sse.text
        assert '"completed"' in sse.text

        # 5. Fetch artifact and verify strict delivery security headers + inline preview mode
        art = await client.get(f"/v1/artifacts/{job_id}/{dl['filename']}")
        assert art.status_code == 200
        assert art.content == b"%PDF-1.7\nNeon Sync Guide"
        assert art.headers["content-disposition"].startswith('attachment; filename="Neon_Guide.pdf"')
        assert art.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in art.headers["content-security-policy"]

        inline_art = await client.get(f"/v1/artifacts/{job_id}/{dl['filename']}?inline=true")
        assert inline_art.status_code == 200
        assert inline_art.headers["content-disposition"] == 'inline; filename="Neon_Guide.pdf"'
        assert inline_art.headers["x-content-type-options"] == "nosniff"


async def test_api_rate_limiting_enforced(tmp_path: Path):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        rate_limit_probes_per_minute=2,
        min_free_disk_bytes=1024,
    )
    app = create_app(settings)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # First two hit validator (403 SSRF), third hits rate limit (429)
        assert (await client.post("/v1/probe", json={"url": "http://127.0.0.1/"})).status_code == 403
        assert (await client.post("/v1/probe", json={"url": "http://127.0.0.1/"})).status_code == 403
        limited = await client.post("/v1/probe", json={"url": "http://127.0.0.1/"})
        assert limited.status_code == 429
        assert limited.json()["detail"]["code"] == "ERR_RATE_LIMITED"
