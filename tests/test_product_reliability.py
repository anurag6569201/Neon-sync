"""User-visible failure recovery and admission-control regressions."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from neon_sync.api import create_app
from neon_sync.api.branding import build_public_capabilities
from neon_sync.config import Settings
from neon_sync.models import DownloadRequest, HealthState, JobRecord, JobStatus, ProgressEvent
from neon_sync.pipeline import InMemoryJobStore, JobManager, LocalArtifactStorage, SQLiteJobStore


@pytest.fixture
async def product_app(tmp_path, monkeypatch):
    app = create_app(Settings(
        storage_dir=tmp_path / "artifacts", scratch_dir=tmp_path / "scratch",
        rate_limit_downloads_per_hour=2, max_pending_jobs=2,
    ))

    async def blocked_download(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.container.orchestrator, "execute_download", blocked_download)
    try:
        yield app
    finally:
        await app.state.container.job_manager.shutdown()
        app.state.container.storage.close()


async def test_batch_rejection_creates_no_jobs_and_consumes_no_partial_budget(product_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=product_app), base_url="http://testserver",
    ) as client:
        # There is room in the queue, but only two quota tokens remain.
        product_app.state.container.settings.max_pending_jobs = 10
        rejected = await client.post("/v1/jobs/batch", json={
            "urls": [f"https://example.com/{i}.mp4" for i in range(3)],
        })
        assert rejected.status_code == 429
        assert (await client.get("/v1/jobs")).json() == []
        accepted = await client.post("/v1/jobs/batch", json={
            "urls": ["https://example.com/one.mp4", "https://example.com/two.mp4"],
        })
        assert accepted.status_code == 202
        assert len(accepted.json()) == 2


async def test_full_queue_rejects_batch_atomically_and_allows_idempotent_retry(product_app):
    product_app.state.container.settings.max_pending_jobs = 1
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=product_app), base_url="http://testserver",
    ) as client:
        rejected = await client.post("/v1/jobs/batch", json={
            "urls": ["https://example.com/a.mp4", "https://example.com/b.mp4"],
        })
        assert rejected.status_code == 503
        assert rejected.headers["retry-after"] == "10"
        assert (await client.get("/v1/jobs")).json() == []
        payload = {"url": "https://example.com/a.mp4", "idempotency_key": "retry-safe"}
        accepted = await client.post("/v1/jobs", json=payload)
        retry = await client.post("/v1/jobs", json=payload)
        assert retry.status_code == accepted.status_code == 202
        assert retry.json()["job_id"] == accepted.json()["job_id"]
        assert (await client.post("/v1/jobs", json={"url": "https://example.com/b.mp4"})).status_code == 503
        assert len((await client.get("/v1/jobs")).json()) == 1


@pytest.mark.parametrize("persistent", [False, True])
async def test_restart_recovers_every_interrupted_job_without_changing_completed(tmp_path, persistent):
    store = SQLiteJobStore(tmp_path / "jobs.db") if persistent else InMemoryJobStore()
    request = DownloadRequest(url="https://example.com/video.mp4")
    for i in range(70):
        store.save(JobRecord(job_id=f"job_{i}", url=request.url, request=request, status=JobStatus.DOWNLOADING))
    store.save(JobRecord(job_id="done", url=request.url, request=request, status=JobStatus.COMPLETED))
    if persistent:
        store.close()
        store = SQLiteJobStore(tmp_path / "jobs.db")
    manager = JobManager(Settings(), None, store=store)
    try:
        assert manager.recover_interrupted_jobs() == 70
        assert manager.recover_interrupted_jobs() == 0
        assert store.list_unfinished() == []
        assert store.get("done").status == JobStatus.COMPLETED
        record = store.get("job_0")
        assert record.status == JobStatus.FAILED
        assert record.progress.stage == "interrupted"
        assert record.request == request
    finally:
        if persistent:
            store.close()


async def test_cancel_before_first_task_turn_releases_queue_slot():
    manager = JobManager(Settings(max_pending_jobs=1), None)
    job = await manager.submit(DownloadRequest(url="https://example.com/a.mp4"))
    cancelled = await manager.cancel(job.job_id)
    assert cancelled.status == JobStatus.CANCELLED
    assert manager.active_count == 0
    manager.ensure_capacity()
    assert not manager._tasks


async def test_sse_does_not_lose_completion_while_consumer_handles_snapshot():
    store = InMemoryJobStore()
    request = DownloadRequest(url="https://example.com/a.mp4")
    store.save(JobRecord(
        job_id="job_race", url=request.url, request=request,
        progress=ProgressEvent(job_id="job_race", status=JobStatus.QUEUED, stage="queued"),
    ))
    manager = JobManager(Settings(), None, store=store)
    stream = manager.subscribe("job_race")
    assert (await anext(stream)).status == JobStatus.QUEUED
    completed = ProgressEvent(job_id="job_race", status=JobStatus.COMPLETED, stage="completed")
    await manager._broadcast("job_race", completed)
    await manager._broadcast("job_race", None)
    assert await asyncio.wait_for(anext(stream), 0.2) == completed
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert not manager._subscribers


async def test_runtime_capabilities_are_free_and_report_partial_installation(product_app):
    from neon_sync.api.app import build_default_registry

    manifests = build_default_registry(Settings()).list_manifests()
    for manifest in manifests:
        manifest.health_status.state = HealthState.DEGRADED
    capabilities = build_public_capabilities(manifests)
    assert not any(cap.status == "operational" for cap in capabilities)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=product_app), base_url="https://media.example.org",
    ) as client:
        response = await client.get("/v1/capabilities")
        assert response.status_code == 200
        data = response.json()
        assert data["limits"]["batch_urls"] == 10
        assert "No Neon Sync account" in data["support_note"]
        assert "DRM-protected" in data["support_note"]


def test_local_download_links_use_current_origin_unless_explicitly_configured(tmp_path: Path):
    source = tmp_path / "source.txt"
    source.write_text("portable download")
    for index, base in enumerate(["", "https://files.example.org/media"]):
        storage = LocalArtifactStorage(Settings(storage_dir=tmp_path / str(index), artifact_base_url=base))
        try:
            artifact = storage.store(job_id="job_1", source_path=source, filename="my file.txt", mime_type="text/plain")
            assert artifact.download_url == f"{base}/v1/artifacts/job_1/my_file.txt"
        finally:
            storage.close()
