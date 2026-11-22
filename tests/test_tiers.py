"""Tests for Neon Sync Freemium Tiers (Guest Trial vs Pro Entitlements)."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from neon_sync.api import create_app
from neon_sync.config import Settings


class _MiniFileHandler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", "36")
        self.end_headers()

    def do_GET(self) -> None:
        body = b"%PDF-1.4\n%NeonSyncTierTestPayload!!\n"
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def local_pdf_server() -> str:
    server = HTTPServer(("127.0.0.1", 0), _MiniFileHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/report.pdf"
    finally:
        server.shutdown()
        server.server_close()


async def test_saas_tier_gating_guest_vs_pro(tmp_path: Path, local_pdf_server: str):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        allow_private_addresses=True,
        min_free_disk_bytes=1024,
        enforce_saas_tiers=True,
        guest_downloads_per_hour=2,
        pro_api_keys="neon-pro-local",
    )
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # 1. Default Guest entitlements
        tier_resp = await client.get("/v1/tier")
        assert tier_resp.status_code == 200
        ent = tier_resp.json()
        assert ent["tier"] == "guest"
        assert ent["downloads_remaining"] == 2
        assert ent["allow_4k"] is False
        assert ent["allow_lossless_audio"] is False

        # 2. Guest blocked on 4K (2160p), FLAC & Browser Fallback -> 402 Payment Required
        r4k = await client.post("/v1/jobs", json={"url": local_pdf_server, "video_quality": "2160"})
        assert r4k.status_code == 402
        assert r4k.json()["detail"]["code"] == "ERR_PRO_FEATURE_REQUIRED"

        rflac = await client.post(
            "/v1/jobs",
            json={"url": local_pdf_server, "mode": "audio", "audio_format": "flac"},
        )
        assert rflac.status_code == 402

        rbrowser = await client.post(
            "/v1/probe",
            json={"url": local_pdf_server, "allow_browser_fallback": True},
        )
        assert rbrowser.status_code == 402

        # 3. Pro key header unlocks 4K & unlimited entitlements
        pro_tier = await client.get("/v1/tier", headers={"X-Neon-Pro-Key": "neon-pro-local"})
        assert pro_tier.status_code == 200
        assert pro_tier.json()["tier"] == "pro"
        assert pro_tier.json()["allow_4k"] is True

        # 4. Guest consumes 2 free downloads, 3rd hits 402 ERR_GUEST_QUOTA_EXCEEDED
        ok1 = await client.post("/v1/jobs", json={"url": local_pdf_server})
        assert ok1.status_code == 202
        ok2 = await client.post("/v1/jobs", json={"url": local_pdf_server})
        assert ok2.status_code == 202
        blocked = await client.post("/v1/jobs", json={"url": local_pdf_server})
        assert blocked.status_code == 402
        assert blocked.json()["detail"]["code"] == "ERR_GUEST_QUOTA_EXCEEDED"
