"""Tests for SQLiteJobStore durability, VTT/SRT transcript normalization & Web UI serving."""

from __future__ import annotations

from pathlib import Path

import httpx

from neon_sync.api import create_app
from neon_sync.config import Settings
from neon_sync.models import DownloadRequest, JobRecord, JobStatus
from neon_sync.pipeline import SQLiteJobStore, normalize_subtitle_to_transcript


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
        assert "history-drawer" in resp.text
        assert "/static/app.css" in resp.text
        assert "/static/app.js" in resp.text

        css = await client.get("/static/app.css")
        assert css.status_code == 200
        assert "--bg:" in css.text

        js = await client.get("/static/app.js")
        assert js.status_code == 200
        assert "/v1/probe" in js.text
        assert "/v1/jobs" in js.text
        assert "/v1/agent/run" in js.text
        assert "/v1/tier" in js.text
