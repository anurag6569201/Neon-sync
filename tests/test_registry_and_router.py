"""Tests for EngineRegistry, UrlRouter fallback escalation, circuit breakers & storage dedup."""

from __future__ import annotations

from pathlib import Path

import pytest

from neon_sync.adapters import AdapterError, DownloadArtifact, ExtractorAdapter, UrlContext
from neon_sync.config import Settings
from neon_sync.models import (
    ContentType,
    DownloadPlan,
    EngineCapability,
    EngineManifest,
    EngineProvenance,
    ErrorCode,
    HealthReport,
    InstallationStatus,
    InvocationMode,
    MatchScore,
    NormalizedMediaOutput,
    ProbeOptions,
    SourceInfo,
    SupportedDomains,
)
from neon_sync.pipeline import ExtractionOrchestrator, LocalArtifactStorage, UrlRouter
from neon_sync.registry import EngineRegistry
from neon_sync.security import CancellationToken, validate_and_canonicalize_url


class _StubEngine(ExtractorAdapter):
    def __init__(
        self,
        engine_id: str,
        *,
        priority: int = 800,
        confidence: int = 80,
        probe_error: AdapterError | None = None,
        content_type: ContentType = ContentType.VIDEO,
        capabilities: list[EngineCapability] | None = None,
    ) -> None:
        self._id = engine_id
        self._priority = priority
        self._confidence = confidence
        self._probe_error = probe_error
        self._content_type = content_type
        self._caps = capabilities or [
            EngineCapability.EXTRACT_METADATA,
            EngineCapability.DOWNLOAD_MEDIA,
        ]
        self.probe_calls = 0

    @property
    def id(self) -> str:
        return self._id

    def manifest(self) -> EngineManifest:
        return EngineManifest(
            id=self._id,
            name=self._id,
            version="1.0.0",
            tier=2,
            supported_content_types=["video", "gallery"],
            supported_domains=SupportedDomains(mode="wildcard"),
            capabilities=self._caps,
            priority=self._priority,
            installation_status=InstallationStatus.INSTALLED,
            license="MIT",
            invocation_mode=InvocationMode.SUBPROCESS,
        )

    def quick_match(self, ctx: UrlContext) -> MatchScore:
        return MatchScore(engine_id=self._id, confidence=self._confidence, reason="stub")

    async def probe(
        self,
        ctx: UrlContext,
        options: ProbeOptions,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> NormalizedMediaOutput:
        self.probe_calls += 1
        if self._probe_error is not None:
            raise self._probe_error
        return NormalizedMediaOutput(
            job_id=ctx.job_id,
            original_url=ctx.validated.original_url,
            url=ctx.url,
            source=SourceInfo(domain=ctx.domain),
            content_type=self._content_type,
            title=f"Extracted by {self._id}",
            engine=EngineProvenance(engine_used=self._id, engine_version="1.0.0"),
        )

    async def download(
        self,
        ctx: UrlContext,
        plan: DownloadPlan,
        *,
        progress_cb=None,
        cancel_token=None,
    ) -> DownloadArtifact:
        out = plan.output_dir / "sample.mp4"
        out.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00test")
        return DownloadArtifact(
            local_path=out,
            filename="sample.mp4",
            mime_type="video/mp4",
            size_bytes=out.stat().st_size,
        )

    async def health_check(self) -> HealthReport:
        return HealthReport(
            engine_id=self._id,
            installation_status=InstallationStatus.INSTALLED,
            version="1.0.0",
            healthy=True,
        )


async def test_router_escalates_on_unsupported_and_tracks_fallback_chain():
    reg = EngineRegistry()
    yt = _StubEngine(
        "yt-dlp",
        priority=900,
        confidence=90,
        probe_error=AdapterError(ErrorCode.ERR_UNSUPPORTED_URL, "No video found", engine_id="yt-dlp"),
    )
    gal = _StubEngine("gallery-dl", priority=880, confidence=75, content_type=ContentType.GALLERY)
    reg.register_extractor(yt)
    reg.register_extractor(gal)

    router = UrlRouter(reg)
    ctx = UrlContext(
        validated=validate_and_canonicalize_url("https://x.com/alice/status/999"),
        job_id="job_fb_1",
    )
    result = await router.probe_with_fallback(ctx, ProbeOptions())

    assert result.engine.engine_used == "gallery-dl"
    assert result.engine.fallback_chain == ["yt-dlp", "gallery-dl"]
    assert result.content_type == ContentType.GALLERY
    assert len(result.warnings) == 1
    assert result.warnings[0].code == "WARN_ENGINE_FALLBACK"


async def test_router_hard_stops_on_drm_without_calling_fallback():
    reg = EngineRegistry()
    primary = _StubEngine(
        "yt-dlp",
        priority=900,
        confidence=95,
        probe_error=AdapterError(ErrorCode.ERR_DRM_PROTECTED, "Widevine DRM", engine_id="yt-dlp"),
    )
    secondary = _StubEngine("gallery-dl", priority=880, confidence=70)
    reg.register_extractor(primary)
    reg.register_extractor(secondary)

    router = UrlRouter(reg)
    ctx = UrlContext(
        validated=validate_and_canonicalize_url("https://example.com/protected"),
        job_id="job_drm_1",
    )
    with pytest.raises(AdapterError) as exc_info:
        await router.probe_with_fallback(ctx, ProbeOptions())
    assert exc_info.value.code == ErrorCode.ERR_DRM_PROTECTED
    assert primary.probe_calls == 1
    assert secondary.probe_calls == 0  # Must NOT waste work escalating DRM!


def test_registry_circuit_breaker_opens_after_repeated_failures():
    reg = EngineRegistry()
    e1 = _StubEngine("yt-dlp", priority=900, confidence=90)
    e2 = _StubEngine("gallery-dl", priority=800, confidence=80)
    reg.register_extractor(e1)
    reg.register_extractor(e2)

    for _ in range(5):
        reg.record_outcome("yt-dlp", "example.com", success=False)

    assert reg.is_circuit_open("yt-dlp", "example.com") is True
    ctx = UrlContext(
        validated=validate_and_canonicalize_url("https://example.com/item"),
        job_id="job_cb_1",
    )
    candidates = reg.select_candidates(ctx)
    assert [c.adapter.id for c in candidates] == ["gallery-dl"]


def test_storage_sha256_deduplication_and_resolution(tmp_path: Path):
    settings = Settings(storage_dir=tmp_path / "artifacts", artifact_ttl_seconds=3600)
    storage = LocalArtifactStorage(settings)

    f1 = tmp_path / "a.pdf"
    f2 = tmp_path / "b.pdf"
    f1.write_bytes(b"%PDF-1.7 identical content")
    f2.write_bytes(b"%PDF-1.7 identical content")

    art1 = storage.store(job_id="job_1", source_path=f1, filename="first.pdf", mime_type="application/pdf")
    art2 = storage.store(job_id="job_2", source_path=f2, filename="second.pdf", mime_type="application/pdf")

    assert art1.sha256 == art2.sha256
    assert art1.disk_path == art2.disk_path  # Content-addressed deduplication!
    assert storage.resolve("job_1", "first.pdf") is not None
    assert storage.resolve("job_1", "../escape.pdf") is None


async def test_orchestrator_caches_probes_and_cleans_scratch(tmp_path: Path):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        min_free_disk_bytes=1024,
    )
    reg = EngineRegistry()
    stub = _StubEngine("yt-dlp", priority=900, confidence=95)
    reg.register_extractor(stub)
    storage = LocalArtifactStorage(settings)
    orch = ExtractionOrchestrator(settings, reg, storage)

    from neon_sync.models import DownloadRequest, ProbeRequest

    r1 = await orch.probe(ProbeRequest(url="https://youtu.be/abc123?si=track"))
    r2 = await orch.probe(ProbeRequest(url="https://www.youtube.com/watch?v=abc123"))
    assert stub.probe_calls == 1  # Second probe served from canonical cache!
    assert r1.title == r2.title

    dl = await orch.execute_download("job_clean_1", DownloadRequest(url="https://youtu.be/abc123"))
    assert dl.download.status == "completed"
    assert dl.download.sha256 is not None
    assert not (settings.scratch_dir / "job_clean_1").exists()  # Scratch purged!


async def test_playlist_page_cache_does_not_reuse_another_page(tmp_path: Path):
    from neon_sync.models import ProbeRequest

    settings = Settings(storage_dir=tmp_path / "storage")
    registry = EngineRegistry()
    stub = _StubEngine("yt-dlp", confidence=95)
    registry.register_extractor(stub)
    storage = LocalArtifactStorage(settings)
    orchestrator = ExtractionOrchestrator(settings, registry, storage)
    try:
        for start in [1, 51, 1, 51]:
            await orchestrator.probe(ProbeRequest(url="https://www.youtube.com/playlist?list=test", playlist_start=start))
        assert stub.probe_calls == 2
    finally:
        storage.close()
