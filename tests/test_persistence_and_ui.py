"""Tests for SQLiteJobStore, Artifact Durability, Turnstile, Transcripts & Spatial Graph UI."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from neon_sync.api import create_app
from neon_sync.cloud import TurnstileVerificationError, verify_turnstile_token
from neon_sync.config import Settings
from neon_sync.models import DownloadRequest, JobRecord, JobStatus
from neon_sync.pipeline import (
    LocalArtifactStorage,
    SQLiteJobStore,
    normalize_subtitle_to_transcript,
)


def test_sqlite_job_store_persists_across_restarts(tmp_path: Path):
    db_file = tmp_path / "jobs.sqlite3"
    store1 = SQLiteJobStore(db_file)
    rec = JobRecord(
        job_id="job_persist_001",
        url="https://example.com/video.mp4",
        request=DownloadRequest(url="https://example.com/video.mp4", idempotency_key="k-123"),
        status=JobStatus.COMPLETED,
    )
    store1.save(rec)
    store1.set_idempotency("k-123", "job_persist_001")
    store1.close()

    # Reopen a fresh connection simulating server restart
    store2 = SQLiteJobStore(db_file)
    try:
        loaded = store2.get("job_persist_001")
        assert loaded is not None
        assert loaded.status == JobStatus.COMPLETED
        assert loaded.url == "https://example.com/video.mp4"
        assert store2.get_idempotency("k-123") == "job_persist_001"
        assert len(store2.list_recent(10)) == 1
    finally:
        store2.close()


def test_local_artifact_storage_survives_restart(tmp_path: Path):
    settings = Settings(storage_dir=tmp_path / "artifacts", artifact_ttl_seconds=3600)
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00\x00\x00\x18ftypmp42persist")

    store1 = LocalArtifactStorage(settings)
    art = store1.store(job_id="job_restart_1", source_path=src, filename="clip.mp4", mime_type="video/mp4")
    store1.close()

    store2 = LocalArtifactStorage(settings)
    try:
        resolved = store2.resolve("job_restart_1", "clip.mp4")
        assert resolved is not None
        assert resolved.sha256 == art.sha256
        assert resolved.disk_path is not None and resolved.disk_path.exists()
    finally:
        store2.close()


async def test_cloudflare_turnstile_fail_closed_and_dev_bypass():
    dev_cfg = Settings(cf_turnstile_secret=None)
    assert await verify_turnstile_token(token=None, expected_action="signup", client_ip="127.0.0.1", settings=dev_cfg) is True

    prod_cfg = Settings(cf_turnstile_secret="test-secret", cf_turnstile_hostnames="app.neonsync.dev")
    with pytest.raises(TurnstileVerificationError):
        await verify_turnstile_token(token=None, expected_action="signup", client_ip="1.2.3.4", settings=prod_cfg)
    with pytest.raises(TurnstileVerificationError):
        await verify_turnstile_token(token="x" * 2100, expected_action="signup", client_ip="1.2.3.4", settings=prod_cfg)


def test_normalize_vtt_and_srt_to_clean_transcript():
    vtt = """WEBVTT
Kind: captions
Language: en

NOTE auto-generated captions

00:00:01.000 --> 00:00:03.000 align:start
Hello <c>world</c>

00:00:03.000 --> 00:00:05.000
Hello world this is Neon Sync

00:00:05.000 --> 00:00:07.000
Hello world this is Neon Sync

00:00:07.500 --> 00:00:09.000
Built &amp; verified!
"""
    clean_vtt = normalize_subtitle_to_transcript(vtt)
    assert clean_vtt == "Hello world this is Neon Sync\nBuilt & verified!"

    srt = """1
00:00:00,500 --> 00:00:02,000
<i>First line</i>

2
00:00:02,100 --> 00:00:04,000
Second line
"""
    assert normalize_subtitle_to_transcript(srt) == "First line\nSecond line"


async def test_web_ui_served_at_root_with_sqlite_backing(tmp_path: Path):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        job_db_path=tmp_path / "state" / "jobs.db",
        min_free_disk_bytes=1024,
    )
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Neon Sync" in resp.text
        assert "gravity-field" in resp.text
        assert "threads-svg" in resp.text
        assert "dev-console-modal" not in resp.text
        assert "automations-modal" not in resp.text
        assert "history-drawer" not in resp.text
        assert "auth-modal" not in resp.text
        assert "account-modal" not in resp.text
        assert "upgrade-modal" not in resp.text
        assert "/static/app.css" in resp.text
        assert "/static/app.js" in resp.text

        css = await client.get("/static/app.css")
        assert css.status_code == 200
        assert "--bg:" in css.text

        js = await client.get("/static/app.js")
        assert js.status_code == 200
        assert "/v1/probe" in js.text
        assert "/v1/jobs" in js.text
        assert "/v1/stream/resolve" in js.text
        assert "/v1/tier" not in js.text
        assert "/v1/auth/" not in js.text
        assert "/v1/agent/run" not in js.text
        assert "/v1/logs" not in js.text


async def test_pwa_assets_share_target_and_url_prefix_capture(tmp_path: Path):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        job_db_path=tmp_path / "state" / "jobs.db",
        min_free_disk_bytes=1024,
    )
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        # PWA manifest, service worker, and icons
        manifest_resp = await client.get("/manifest.webmanifest")
        assert manifest_resp.status_code == 200
        assert "application/manifest+json" in manifest_resp.headers["content-type"]
        manifest = manifest_resp.json()
        assert manifest["name"] == "Neon Sync"
        assert manifest["display"] == "standalone"
        assert manifest["share_target"]["action"] == "/share"
        assert any(icon["src"] == "/static/icon-192.png" for icon in manifest["icons"])

        sw_resp = await client.get("/sw.js")
        assert sw_resp.status_code == 200
        assert sw_resp.headers.get("service-worker-allowed") == "/"
        assert "no-cache" in sw_resp.headers.get("cache-control", "")
        assert 'url.pathname.startsWith("/v1/")' in sw_resp.text

        png_resp = await client.get("/apple-touch-icon.png")
        assert png_resp.status_code == 200
        assert png_resp.content.startswith(b"\x89PNG\r\n\x1a\n")

        icon_512 = await client.get("/static/icon-512.png")
        assert icon_512.status_code == 200
        assert icon_512.content.startswith(b"\x89PNG\r\n\x1a\n")

        fav_resp = await client.get("/favicon.ico")
        assert fav_resp.status_code == 200
        assert "<svg" in fav_resp.text

        # Share Target receiver handles direct url, embedded text url, and rejects non-http schemes
        share_direct = await client.get("/share", params={"url": "https://example.com/watch?v=123"})
        assert share_direct.status_code == 302
        assert share_direct.headers["location"] == "/?url=https%3A%2F%2Fexample.com%2Fwatch%3Fv%3D123"

        share_text = await client.get("/share", params={"text": "Watch this https://youtu.be/abc123!"})
        assert share_text.status_code == 302
        assert share_text.headers["location"] == "/?url=https%3A%2F%2Fyoutu.be%2Fabc123"

        share_bad = await client.get("/share", params={"url": "javascript:alert(1)"})
        assert share_bad.status_code == 302
        assert share_bad.headers["location"] == "/"

        # Instant URL prefix route (both full https:// and proxy-collapsed https:/) preserves query string
        prefix_full = await client.get("/https://www.youtube.com/watch?v=abc123&t=42")
        assert prefix_full.status_code == 302
        assert prefix_full.headers["location"] == "/?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3Dabc123%26t%3D42"

        prefix_collapsed = await client.get("/https:/youtu.be/abc123")
        assert prefix_collapsed.status_code == 302
        assert prefix_collapsed.headers["location"] == "/?url=https%3A%2F%2Fyoutu.be%2Fabc123"

        # Non-HTTP paths stay strict 404 (never catch-all)
        for bad_path in ("/docs", "/unknown-route", "/javascript:alert(1)", "/ftp://example.com/file.zip"):
            assert (await client.get(bad_path)).status_code == 404

        # HTML wires up manifest and Quick Capture dialog
        html = (await client.get("/")).text
        assert 'rel="manifest" href="/manifest.webmanifest"' in html
        assert "quick-capture-dialog" in html
        assert "bookmarklet-link" in html
