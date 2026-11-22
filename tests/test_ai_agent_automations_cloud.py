"""Tests for Durable Artifact Storage, Probe Coalescing, AI Agent v2, Automations & Cloudflare Stack."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from neon_sync.agent import parse_intent_deterministic
from neon_sync.api import create_app
from neon_sync.cloud import TurnstileVerificationError, verify_turnstile_token
from neon_sync.config import Settings
from neon_sync.pipeline import LocalArtifactStorage


class _AutomationWebhookAndMediaServer(BaseHTTPRequestHandler):
    received_webhooks: ClassVar[list[tuple[dict[str, str], bytes]]] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_HEAD(self) -> None:
        if self.path == "/doc.pdf":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", "28")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/doc.pdf":
            body = b"%PDF-1.7\nNeon Sync Auto PDF!"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        if self.path == "/webhook":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            self.received_webhooks.append(({k.lower(): v for k, v in self.headers.items()}, raw))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"received":true}')
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture()
def auto_server() -> str:
    _AutomationWebhookAndMediaServer.received_webhooks.clear()
    server = HTTPServer(("127.0.0.1", 0), _AutomationWebhookAndMediaServer)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_local_artifact_storage_survives_restart(tmp_path: Path):
    settings = Settings(storage_dir=tmp_path / "artifacts", artifact_ttl_seconds=3600)
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00\x00\x00\x18ftypmp42persist")

    store1 = LocalArtifactStorage(settings)
    art = store1.store(job_id="job_restart_1", source_path=src, filename="clip.mp4", mime_type="video/mp4")
    store1.close()

    # Re-open fresh LocalArtifactStorage simulating full process restart
    store2 = LocalArtifactStorage(settings)
    try:
        resolved = store2.resolve("job_restart_1", "clip.mp4")
        assert resolved is not None
        assert resolved.sha256 == art.sha256
        assert resolved.disk_path is not None and resolved.disk_path.exists()
    finally:
        store2.close()


def test_ai_agent_v2_planner_multi_step_and_live_stream_intents():
    u = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    # Multi-step viral reel kit
    kit = parse_intent_deterministic(f"create a viral reel kit from 0:10 to 0:25 {u}", u)
    assert kit.action == "multi_step"
    assert len(kit.steps) == 2
    assert kit.steps[0]["params"]["clip_start_seconds"] == 10.0
    assert kit.steps[0]["params"]["clip_end_seconds"] == 25.0
    assert kit.steps[1]["params"]["audio_format"] == "mp3"
    assert kit.output_style == "social_kit"

    # Watch live + summarize
    live_sum = parse_intent_deterministic(f"watch live and summarize key takeaways {u}", u)
    assert live_sum.action == "stream_and_summarize"
    assert live_sum.stream_mode == "video"

    # Listen live audio
    listen = parse_intent_deterministic(f"listen live to this podcast {u}", u)
    assert listen.action == "stream"
    assert listen.stream_mode == "audio"


async def test_automations_end_to_end_signed_webhook_and_cloud_status(
    auto_server: str,
    tmp_path: Path,
):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        auth_db_path=tmp_path / "auth.sqlite3",
        automations_db_path=tmp_path / "automations.sqlite3",
        enable_automation_scheduler=False,
        allow_private_addresses=True,
        min_free_disk_bytes=1024,
    )
    app = create_app(settings)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # 1. Status exposes Google & Cloudflare cloud integrations cleanly
        st = await client.get("/v1/status")
        assert st.status_code == 200
        cloud_ids = {c["id"] for c in st.json()["cloud_integrations"]}
        assert {
            "google-identity",
            "google-gemini-ai",
            "cloudflare-r2",
            "cloudflare-workers-ai",
            "cloudflare-turnstile",
        }.issubset(cloud_ids)

        # 2. Templates endpoint
        tpls = await client.get("/v1/automations/templates")
        assert tpls.status_code == 200
        assert len(tpls.json()) >= 4

        # 3. Create automation rule with signed webhook
        created = await client.post(
            "/v1/automations",
            json={
                "name": "Doc Brief Monitor",
                "target_url": f"{auto_server}/doc.pdf",
                "trigger_type": "interval",
                "interval_minutes": 60,
                "action_type": "ai_brief",
                "prompt": "summarize this document",
                "webhook_url": f"{auto_server}/webhook",
            },
        )
        assert created.status_code == 201
        rule = created.json()
        aid = rule["id"]
        assert rule["next_run_at"] is not None

        # 4. Run automation immediately -> executes AI Agent + delivers HMAC-SHA256 signed webhook!
        run_resp = await client.post(f"/v1/automations/{aid}/run")
        assert run_resp.status_code == 200
        rdata = run_resp.json()
        assert rdata["status"] == "completed"
        assert rdata["webhook_status"] == "http_200"
        assert len(_AutomationWebhookAndMediaServer.received_webhooks) == 1

        wh_headers, wh_raw = _AutomationWebhookAndMediaServer.received_webhooks[0]
        assert wh_headers["x-neon-sync-event"] == "automation.run.completed"
        sig_header = wh_headers["x-neon-sync-signature"]
        assert sig_header.startswith("sha256=")
        # Verify exact HMAC-SHA256 signature using stored secret!
        _, secret = app.state.container.automations.store.get(aid)
        expected_sig = hmac.new(secret.encode("utf-8"), wh_raw, hashlib.sha256).hexdigest()
        assert sig_header == f"sha256={expected_sig}"
        wh_body = json.loads(wh_raw)
        assert wh_body["service"] == "neon-sync"
        assert wh_body["automation_id"] == aid

        # 5. Run history persisted in SQLite
        runs = await client.get(f"/v1/automations/{aid}/runs")
        assert runs.status_code == 200
        assert len(runs.json()) == 1

        # 6. Toggle disable & delete
        patched = await client.patch(f"/v1/automations/{aid}", json={"enabled": False})
        assert patched.status_code == 200
        assert patched.json()["enabled"] is False
        assert patched.json()["next_run_at"] is None

        deleted = await client.delete(f"/v1/automations/{aid}")
        assert deleted.status_code == 200


async def test_cloudflare_turnstile_fail_closed_and_dev_bypass():
    # When unconfigured (local dev), passes seamlessly
    dev_cfg = Settings(cf_turnstile_secret=None)
    assert await verify_turnstile_token(token=None, expected_action="signup", client_ip="127.0.0.1", settings=dev_cfg) is True

    # When configured, missing or oversized token fails closed!
    prod_cfg = Settings(cf_turnstile_secret="test-secret", cf_turnstile_hostnames="app.neonsync.dev")
    with pytest.raises(TurnstileVerificationError):
        await verify_turnstile_token(token=None, expected_action="signup", client_ip="1.2.3.4", settings=prod_cfg)
    with pytest.raises(TurnstileVerificationError):
        await verify_turnstile_token(token="x" * 2100, expected_action="signup", client_ip="1.2.3.4", settings=prod_cfg)
