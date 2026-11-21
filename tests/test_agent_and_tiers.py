"""Tests for Neon Sync SaaS Freemium Tiers (Guest vs Pro/BYOK) and Streaming AI Agent."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from neon_sync.agent import extract_url_from_prompt, parse_intent_deterministic
from neon_sync.api import create_app
from neon_sync.config import Settings


class _MiniFileHandler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", "36")
        self.end_headers()

    def do_GET(self) -> None:
        body = b"%PDF-1.4\n%NeonSyncAgentTestPayload!\n"
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


def test_deterministic_agent_planner_parses_intents():
    u = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert extract_url_from_prompt(f"clip 0:05 to 0:15 in 720p mp4 {u}") == u

    p1 = parse_intent_deterministic(f"clip 0:05 to 0:15 in 720p h264 mp4 {u}", u)
    assert p1.action == "download"
    assert p1.download_params["mode"] == "video"
    assert p1.download_params["video_quality"] == "720"
    assert p1.download_params["video_codec"] == "h264"
    assert p1.download_params["video_container"] == "mp4"
    assert p1.download_params["clip_start_seconds"] == 5.0
    assert p1.download_params["clip_end_seconds"] == 15.0

    p2 = parse_intent_deterministic(f"extract audio as 320k mp3 and normalize volume {u}", u)
    assert p2.action == "download"
    assert p2.download_params["mode"] == "audio"
    assert p2.download_params["audio_format"] == "mp3"
    assert p2.download_params["audio_bitrate"] == "320k"
    assert p2.download_params["normalize_audio"] is True

    p3 = parse_intent_deterministic("download first 12 gallery photos as webp 1280px https://imgur.com/a/demo", "https://imgur.com/a/demo")
    assert p3.download_params["mode"] == "image"
    assert p3.download_params["image_format"] == "webp"
    assert p3.download_params["image_max_width"] == 1280
    assert p3.download_params["gallery_range_start"] == 1
    assert p3.download_params["gallery_range_end"] == 12

    p4 = parse_intent_deterministic(f"summarize the key points from {u}", u)
    assert p4.action == "summarize"

    p5 = parse_intent_deterministic(f"probe formats for {u}", u)
    assert p5.action == "probe"


async def test_saas_tier_gating_guest_vs_pro_and_byok(tmp_path: Path, local_pdf_server: str):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        allow_private_addresses=True,
        min_free_disk_bytes=1024,
        enforce_saas_tiers=True,
        guest_downloads_per_hour=2,
        guest_agent_runs_per_day=1,
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
        assert ent["agent_runs_remaining"] == 1
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

        # 4. BYOK Gemini key also unlocks Pro tier
        byok_tier = await client.get("/v1/tier", headers={"X-Gemini-Api-Key": "AIzaFakeByokKey123"})
        assert byok_tier.status_code == 200
        assert byok_tier.json()["tier"] == "pro"
        assert byok_tier.json()["has_llm_provider"] is True

        # 5. Guest consumes 2 free downloads, 3rd hits 402 ERR_GUEST_QUOTA_EXCEEDED
        ok1 = await client.post("/v1/jobs", json={"url": local_pdf_server})
        assert ok1.status_code == 202
        ok2 = await client.post("/v1/jobs", json={"url": local_pdf_server})
        assert ok2.status_code == 202
        blocked = await client.post("/v1/jobs", json={"url": local_pdf_server})
        assert blocked.status_code == 402
        assert blocked.json()["detail"]["code"] == "ERR_GUEST_QUOTA_EXCEEDED"


async def test_agent_run_sse_end_to_end_and_daily_quota(tmp_path: Path, local_pdf_server: str):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        allow_private_addresses=True,
        min_free_disk_bytes=1024,
        enforce_saas_tiers=True,
        guest_agent_runs_per_day=1,
        pro_api_keys="neon-pro-local",
    )
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # Missing URL returns 400 without consuming Guest credit
        no_url = await client.post("/v1/agent/run", json={"prompt": "download my video please"})
        assert no_url.status_code == 400
        assert (await client.get("/v1/tier")).json()["agent_runs_remaining"] == 1

        # Valid Agent run streams SSE events and completes real download
        resp = await client.post(
            "/v1/agent/run",
            json={"prompt": f"download this report {local_pdf_server}"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        events: list[tuple[str, dict]] = []
        current_event = "message"
        for line in resp.text.splitlines():
            if line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                events.append((current_event, json.loads(line.split(":", 1)[1].strip())))

        event_types = [e[0] for e in events]
        assert "thought" in event_types
        assert "plan" in event_types
        assert "tool" in event_types
        assert "job" in event_types
        assert "answer" in event_types
        assert "done" in event_types

        answer_evt = next(d for t, d in events if t == "answer")
        assert "report.pdf" in answer_evt["message"]
        assert answer_evt["data"]["job"]["status"] == "completed"

        # 2nd Guest Agent run hits 402 ERR_AGENT_QUOTA_EXCEEDED
        second = await client.post(
            "/v1/agent/run",
            json={"prompt": f"probe {local_pdf_server}"},
        )
        assert second.status_code == 402
        assert second.json()["detail"]["code"] == "ERR_AGENT_QUOTA_EXCEEDED"

        # Pro key bypasses daily cap and runs probe cleanly
        pro_run = await client.post(
            "/v1/agent/run",
            headers={"X-Neon-Pro-Key": "neon-pro-local"},
            json={"prompt": f"probe formats for {local_pdf_server}"},
        )
        assert pro_run.status_code == 200
        assert "event: answer" in pro_run.text
