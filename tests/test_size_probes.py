"""Progressive playlist size metadata, independent failures, and bounded engine work."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from neon_sync.adapters import AdapterError
from neon_sync.api import create_app
from neon_sync.config import Settings
from neon_sync.models import (
    EngineProvenance,
    ErrorCode,
    NormalizedMediaOutput,
    ProbeRequest,
    SourceInfo,
)
from neon_sync.pipeline import SlidingWindowRateLimiter


def metadata(url: str) -> NormalizedMediaOutput:
    return NormalizedMediaOutput(
        job_id="size", original_url=url, url=url, content_type="video", size_bytes=1024,
        source=SourceInfo(domain="example.com"), engine=EngineProvenance(engine_used="yt-dlp", engine_version="test"),
    )


@pytest.fixture
async def size_app(tmp_path):
    app = create_app(Settings(
        _env_file=None, storage_dir=tmp_path / "artifacts", job_db_path=None,
        max_concurrent_probes=2,
    ))
    try:
        yield app
    finally:
        app.state.container.storage.close()


async def test_size_stream_deduplicates_and_isolates_unavailable_items(size_app, monkeypatch):
    calls = []

    async def probe(body):
        calls.append(body)
        if body.url.endswith("private"):
            raise AdapterError(ErrorCode.ERR_AUTH_REQUIRED, "yt-dlp requires sign-in", engine_id="yt-dlp")
        await asyncio.sleep(0.01)
        return metadata(body.url)

    monkeypatch.setattr(size_app.state.container.orchestrator, "probe", probe)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=size_app), base_url="http://test") as client:
        response = await client.post("/v1/probe/sizes", json={"urls": [
            "https://example.com/ok", "https://example.com/private", "https://example.com/ok",
        ]})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    results = [json.loads(line) for line in response.text.splitlines()]
    assert len(results) == len(calls) == 2
    assert results[0]["error"]["code"] == "ERR_AUTH_REQUIRED"
    assert "yt-dlp" not in response.text
    assert results[1]["probe"]["size_bytes"] == 1024
    assert all(body.max_items == 1 and not body.include_subtitles for body in calls)


async def test_size_batches_are_bounded_and_report_rate_limit_retry(size_app, monkeypatch):
    async def probe(body):
        return metadata(body.url)

    c = size_app.state.container
    monkeypatch.setattr(c.orchestrator, "probe", probe)
    c.probe_limiter = SlidingWindowRateLimiter(1, 60)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=size_app), base_url="http://test") as client:
        assert (await client.post("/v1/probe/sizes", json={"urls": ["https://example.com/a"] * 6})).status_code == 422
        assert (await client.post("/v1/probe/sizes", json={"urls": ["x" * 4097]})).status_code == 422
        assert (await client.post("/v1/probe/sizes", json={"urls": ["https://example.com/a"]})).status_code == 200
        limited = await client.post("/v1/probe/sizes", json={"urls": ["https://example.com/b"]})
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"


async def test_metadata_engine_concurrency_is_bounded_across_batches(size_app, monkeypatch):
    orchestrator = size_app.state.container.orchestrator
    active = maximum = calls = 0
    saturated = asyncio.Event()
    release = asyncio.Event()

    async def probe(ctx, options, **kwargs):
        nonlocal active, maximum, calls
        calls += 1
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            saturated.set()
        try:
            await release.wait()
            return metadata(ctx.url)
        finally:
            active -= 1

    monkeypatch.setattr(orchestrator._router, "probe_with_fallback", probe)
    tasks = [asyncio.create_task(orchestrator.probe(ProbeRequest(url=f"https://example.com/{index}"))) for index in range(10)]
    # A shared request must also reuse its existing in-flight probe.
    tasks.append(asyncio.create_task(orchestrator.probe(ProbeRequest(url="https://example.com/0"))))
    try:
        await asyncio.wait_for(saturated.wait(), 2)
        assert active == calls == maximum == 2
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert maximum == 2
    assert calls == 10
