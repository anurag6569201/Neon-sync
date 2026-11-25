"""Deep hardening tests for partial / single-purpose extractions across Neon Sync."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from neon_sync.adapters import (
    AdapterError,
    DownloadArtifact,
    ExtractorAdapter,
    FfmpegAdapter,
    UrlContext,
    YtDlpAdapter,
)
from neon_sync.adapters.ytdlp import normalize_ytdlp_json
from neon_sync.config import Settings
from neon_sync.models import (
    ContentType,
    DownloadPlan,
    DownloadRequest,
    DownloadStatus,
    EngineCapability,
    EngineManifest,
    EngineProvenance,
    ErrorCode,
    FormatInfo,
    HealthReport,
    InstallationStatus,
    InvocationMode,
    MatchScore,
    NormalizedMediaOutput,
    ProbeOptions,
    SourceInfo,
    SubtitleTrack,
    SupportedDomains,
)
from neon_sync.pipeline import (
    ExtractionOrchestrator,
    LocalArtifactStorage,
    convert_json3_to_vtt,
    convert_vtt_to_srt,
    normalize_subtitle_to_transcript,
)
from neon_sync.registry import EngineRegistry
from neon_sync.security import SubprocessResult, validate_and_canonicalize_url


def test_normalize_ytdlp_json_prefers_vtt_and_srt_over_json3():
    validated = validate_and_canonicalize_url("https://youtu.be/YW7Uc5_ixtM")
    ctx = UrlContext(validated=validated, job_id="job_sub_pref")
    raw = {
        "id": "YW7Uc5_ixtM",
        "title": "Demo Video",
        "extractor": "youtube",
        "webpage_url": "https://www.youtube.com/watch?v=YW7Uc5_ixtM",
        "formats": [],
        "subtitles": {
            "en": [
                {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?v=abc&fmt=json3", "name": "English"},
                {"ext": "srv3", "url": "https://www.youtube.com/api/timedtext?v=abc&fmt=srv3", "name": "English"},
                {"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?v=abc&fmt=vtt", "name": "English"},
                {"ext": "srt", "url": "https://www.youtube.com/api/timedtext?v=abc&fmt=srt", "name": "English"},
            ]
        },
        "automatic_captions": {
            "hi": [
                {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?v=abc&lang=hi&fmt=json3"},
                {"ext": "srt", "url": "https://www.youtube.com/api/timedtext?v=abc&lang=hi&fmt=srt"},
            ]
        },
    }
    out = normalize_ytdlp_json(raw, ctx, version="2026.01.01", probe_ms=100)
    by_lang = {s.language: s for s in out.subtitles}
    assert by_lang["en"].url is not None and "fmt=vtt" in by_lang["en"].url
    assert by_lang["hi"].url is not None and "fmt=srt" in by_lang["hi"].url


def test_json3_to_vtt_and_vtt_to_srt_and_transcript():
    sample_json3 = json.dumps(
        {
            "events": [
                {
                    "tStartMs": 1000,
                    "dDurationMs": 2500,
                    "segs": [{"utf8": "Hello "}, {"utf8": "Neon Sync"}],
                },
                {
                    "tStartMs": 4000,
                    "dDurationMs": 2000,
                    "segs": [{"utf8": "Single purpose extraction."}],
                },
            ]
        }
    )
    vtt = convert_json3_to_vtt(sample_json3)
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:03.500" in vtt
    assert "Hello Neon Sync" in vtt

    srt = convert_vtt_to_srt(vtt)
    assert "1\n00:00:01,000 --> 00:00:03,500\nHello Neon Sync" in srt
    assert "2\n00:00:04,000 --> 00:00:06,000\nSingle purpose extraction." in srt

    txt = normalize_subtitle_to_transcript(sample_json3)
    assert "Hello Neon Sync" in txt
    assert "Single purpose extraction." in txt
    assert "-->" not in txt


def test_ytdlp_partial_extraction_argv_flags(tmp_path: Path):
    yt = YtDlpAdapter(Settings())

    # 1. Subtitles-only uses --sleep-subtitles 2 to avoid YouTube 429s
    sub_argv = yt.build_download_argv(
        "https://youtu.be/YW7Uc5_ixtM",
        DownloadPlan(job_id="j1", output_dir=tmp_path, subtitles_only=True, subtitle_languages=["en"]),
    )
    assert "--sleep-subtitles" in sub_argv and "2" in sub_argv

    # 2. Video clip uses --download-sections + --force-keyframes-at-cuts
    clip_argv = yt.build_download_argv(
        "https://youtu.be/YW7Uc5_ixtM",
        DownloadPlan(
            job_id="j2",
            output_dir=tmp_path,
            mode="video",
            clip_start_seconds=15.0,
            clip_end_seconds=45.0,
        ),
    )
    assert "--download-sections" in clip_argv
    assert "*15.000-45.000" in clip_argv
    assert "--force-keyframes-at-cuts" in clip_argv

    # 3. Audio clip + normalize_audio skips -x (so FFmpeg does ONE loudnorm+encode pass) & omits keyframe flag
    aud_norm_argv = yt.build_download_argv(
        "https://youtu.be/YW7Uc5_ixtM",
        DownloadPlan(
            job_id="j3",
            output_dir=tmp_path,
            mode="audio",
            audio_format="mp3",
            audio_bitrate="320k",
            normalize_audio=True,
            clip_start_seconds=5.0,
            clip_end_seconds=20.0,
        ),
    )
    assert "-x" not in aud_norm_argv
    assert "--download-sections" in aud_norm_argv and "*5.000-20.000" in aud_norm_argv
    assert "--force-keyframes-at-cuts" not in aud_norm_argv

    # 4. Audio without normalize_audio still uses -x --audio-format mp3
    aud_plain_argv = yt.build_download_argv(
        "https://youtu.be/YW7Uc5_ixtM",
        DownloadPlan(
            job_id="j4",
            output_dir=tmp_path,
            mode="audio",
            audio_format="mp3",
            audio_bitrate="192k",
            normalize_audio=False,
        ),
    )
    assert "-x" in aud_plain_argv and "mp3" in aud_plain_argv

    # 5. Livestream max duration uses --download-sections *0-N
    live_argv = yt.build_download_argv(
        "https://www.twitch.tv/demo",
        DownloadPlan(job_id="j5", output_dir=tmp_path, mode="video", livestream_max_duration=180),
    )
    assert "--download-sections" in live_argv and "*0-180" in live_argv

    # 6. Muted video_only uses --remux-video instead of --merge-output-format
    muted_argv = yt.build_download_argv(
        "https://youtu.be/YW7Uc5_ixtM",
        DownloadPlan(
            job_id="j6",
            output_dir=tmp_path,
            mode="video",
            video_only=True,
            video_container="mp4",
        ),
    )
    assert "--remux-video" in muted_argv and "mp4" in muted_argv
    assert "--merge-output-format" not in muted_argv


def test_ffmpeg_clip_argv_container_safe_faststart(tmp_path: Path):
    ff = FfmpegAdapter(Settings())

    mp4_argv = ff.build_clip_argv(
        tmp_path / "in.mp4",
        tmp_path / "out.mp4",
        start_seconds=5,
        end_seconds=15,
        max_output_bytes=10_000_000,
    )
    assert "-movflags" in mp4_argv and "+faststart" in mp4_argv

    for ext in (".mp3", ".webm", ".mkv", ".flac", ".wav", ".opus"):
        argv = ff.build_clip_argv(
            tmp_path / f"in{ext}",
            tmp_path / f"out{ext}",
            start_seconds=5,
            end_seconds=15,
            max_output_bytes=10_000_000,
        )
        assert "-movflags" not in argv, f"-movflags should not be passed for {ext}"


async def test_ytdlp_download_packages_multi_item_playlist_into_zip(tmp_path: Path):
    yt = YtDlpAdapter(Settings())
    validated = validate_and_canonicalize_url("https://www.youtube.com/playlist?list=PL123")
    ctx = UrlContext(validated=validated, job_id="job_pl_zip")
    out_dir = tmp_path / "dl"
    plan = DownloadPlan(job_id="job_pl_zip", output_dir=out_dir, mode="video", playlist_items="1-3")

    async def _fake_subproc(argv, **kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "vid1.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"A" * 64)
        (out_dir / "vid2.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"B" * 64)
        (out_dir / "vid3.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"C" * 64)
        (out_dir / "vid1.en.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHi\n", encoding="utf-8")
        return SubprocessResult(returncode=0, stdout="", stderr="")

    with patch("neon_sync.adapters.ytdlp.run_safe_subprocess", side_effect=_fake_subproc):
        artifact = await yt.download(ctx, plan)

    assert artifact.filename == "playlist_archive.zip"
    assert artifact.mime_type == "application/zip"
    assert len(artifact.extra_files) == 1 and artifact.extra_files[0].suffix == ".vtt"
    with zipfile.ZipFile(artifact.local_path, "r") as zf:
        assert sorted(zf.namelist()) == ["vid1.en.vtt", "vid1.mp4", "vid2.mp4", "vid3.mp4"]


class _StubAdapter(ExtractorAdapter):
    def __init__(self, adapter_id: str, probe_output: NormalizedMediaOutput) -> None:
        self._id = adapter_id
        self._probe_output = probe_output
        self.download_calls: list[DownloadPlan] = []

    @property
    def id(self) -> str:
        return self._id

    def manifest(self) -> EngineManifest:
        return EngineManifest(
            id=self._id,
            name=self._id,
            version="1.0.0",
            tier=2,
            supported_content_types=["video", "audio"],
            supported_domains=SupportedDomains(mode="wildcard"),
            protocols=["https"],
            capabilities=[EngineCapability.EXTRACT_METADATA, EngineCapability.DOWNLOAD_MEDIA],
            priority=950,
            installation_status=InstallationStatus.INSTALLED,
            license="MIT",
            invocation_mode=InvocationMode.SUBPROCESS,
        )

    def quick_match(self, ctx: UrlContext) -> MatchScore:
        return MatchScore(engine_id=self._id, confidence=99, reason="stub")

    async def probe(self, ctx: UrlContext, options: ProbeOptions, *, cancel_token=None) -> NormalizedMediaOutput:
        return self._probe_output.model_copy(deep=True)

    async def download(self, ctx: UrlContext, plan: DownloadPlan, *, progress_cb=None, cancel_token=None) -> DownloadArtifact:
        self.download_calls.append(plan)
        plan.output_dir.mkdir(parents=True, exist_ok=True)
        out = plan.output_dir / ("clip.m4a" if plan.mode == "audio" else "clip.mp4")
        out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 128)
        return DownloadArtifact(
            local_path=out,
            filename=out.name,
            mime_type="audio/mp4" if plan.mode == "audio" else "video/mp4",
            size_bytes=out.stat().st_size,
        )

    async def health_check(self) -> HealthReport:
        return HealthReport(
            engine_id=self._id,
            installation_status=InstallationStatus.INSTALLED,
            version="1.0.0",
            healthy=True,
            detail="stub",
        )


def _make_stub_probe(size_bytes: int = 5_000_000_000) -> NormalizedMediaOutput:
    return NormalizedMediaOutput(
        job_id="probe_stub",
        original_url="https://youtu.be/YW7Uc5_ixtM",
        url="https://www.youtube.com/watch?v=YW7Uc5_ixtM",
        source=SourceInfo(domain="youtu.be", platform="youtube", external_id="YW7Uc5_ixtM"),
        content_type=ContentType.VIDEO,
        title="Big 4K Video",
        duration_seconds=3600.0,
        size_bytes=size_bytes,
        available_formats=[
            FormatInfo(
                format_id="313",
                container="webm",
                mime_type="video/webm",
                height=2160,
                size_bytes=size_bytes,
                has_video=True,
                has_audio=False,
            ),
            FormatInfo(
                format_id="18",
                container="mp4",
                mime_type="video/mp4",
                height=360,
                size_bytes=50_000,
                has_video=True,
                has_audio=True,
            ),
        ],
        subtitles=[
            SubtitleTrack(
                language="en",
                name="English",
                is_auto_generated=False,
                formats=["vtt", "json3"],
                url="https://www.youtube.com/api/timedtext?v=YW7Uc5_ixtM&lang=en&fmt=json3",
            )
        ],
        engine=EngineProvenance(engine_used="yt-dlp", engine_version="1.0.0"),
    )


async def test_orchestrator_direct_subtitle_fastpath_retries_429_and_bypasses_4k_quota(tmp_path: Path):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        max_job_bytes=1_000_000,  # 1 MB cap vs 5 GB 4K video!
        min_free_disk_bytes=1024,
    )
    stub = _StubAdapter("yt-dlp", _make_stub_probe(size_bytes=5_000_000_000))
    registry = EngineRegistry()
    registry.register_extractor(stub)
    storage = LocalArtifactStorage(settings)
    orch = ExtractionOrchestrator(settings, registry, storage)

    json3_body = json.dumps(
        {
            "events": [
                {"tStartMs": 500, "dDurationMs": 1500, "segs": [{"utf8": "Zero 429 failure!"}]},
            ]
        }
    )
    resp_429 = MagicMock(status_code=429, content=b"", text="", headers={"retry-after": "1"})
    resp_200 = MagicMock(status_code=200, content=json3_body.encode(), text=json3_body, headers={})

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.get = AsyncMock(side_effect=[resp_429, resp_200])

    with (
        patch("neon_sync.pipeline.orchestrator.httpx.AsyncClient", return_value=mock_client),
        patch("neon_sync.pipeline.orchestrator.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await orch.execute_download(
            "job_sub_direct",
            DownloadRequest(
                url="https://youtu.be/YW7Uc5_ixtM",
                mode="video",
                subtitles_only=True,
                subtitle_languages=["en"],
                subtitle_format="srt",
                generate_transcript=True,
            ),
        )

    assert result.download.status == DownloadStatus.COMPLETED
    assert result.download.filename is not None and result.download.filename.endswith(".srt")
    assert result.transcript is not None and result.transcript.artifact_url is not None
    # Subprocess downloader was NEVER spawned!
    assert len(stub.download_calls) == 0
    assert mock_client.get.await_count == 2


async def test_orchestrator_quota_blocks_full_4k_but_allows_partial_extractions(tmp_path: Path):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        max_job_bytes=1_000_000,
        min_free_disk_bytes=1024,
    )
    stub = _StubAdapter("yt-dlp", _make_stub_probe(size_bytes=5_000_000_000))
    registry = EngineRegistry()
    registry.register_extractor(stub)
    storage = LocalArtifactStorage(settings)
    orch = ExtractionOrchestrator(settings, registry, storage)

    # Full best quality exceeds quota -> rejected
    with pytest.raises(AdapterError) as exc_info:
        await orch.execute_download(
            "job_full_4k",
            DownloadRequest(url="https://youtu.be/YW7Uc5_ixtM", mode="video", video_quality="best"),
        )
    assert exc_info.value.code == ErrorCode.ERR_QUOTA_EXCEEDED

    # Audio-only, lower video quality, and clip are all allowed!
    res_aud = await orch.execute_download(
        "job_part_aud",
        DownloadRequest(url="https://youtu.be/YW7Uc5_ixtM", mode="audio", audio_format="m4a"),
    )
    assert res_aud.download.status == DownloadStatus.COMPLETED

    res_clip = await orch.execute_download(
        "job_part_clip",
        DownloadRequest(
            url="https://youtu.be/YW7Uc5_ixtM",
            mode="video",
            video_quality="best",
            clip_start_seconds=10.0,
            clip_end_seconds=25.0,
        ),
    )
    assert res_clip.download.status == DownloadStatus.COMPLETED


async def test_orchestrator_no_double_clip_and_sequential_audio_loudnorm(tmp_path: Path):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        max_job_bytes=100_000_000,
        min_free_disk_bytes=1024,
    )

    class _MockFfmpeg(FfmpegAdapter):
        def __init__(self) -> None:
            super().__init__(settings)
            self.calls: list[str] = []

        def manifest(self) -> EngineManifest:
            m = super().manifest()
            m.installation_status = InstallationStatus.INSTALLED
            return m

        async def clip_media(self, input_path: Path, output_path: Path, **kwargs) -> Path:
            self.calls.append("clip_media")
            output_path.write_bytes(input_path.read_bytes())
            return output_path

        async def extract_audio(self, input_path: Path, output_path: Path, **kwargs) -> Path:
            self.calls.append(f"extract_audio:norm={kwargs.get('normalize_audio')}")
            output_path.write_bytes(b"ID3" + b"\x00" * 64)
            return output_path

        async def probe_file(self, path: Path, **kwargs) -> dict:
            return {"duration_seconds": 15.0}

    # Case A: yt-dlp handles clipping via --download-sections -> NO FFmpeg clip_media, YES FFmpeg loudnorm
    ff_a = _MockFfmpeg()
    reg_a = EngineRegistry()
    reg_a.register_extractor(_StubAdapter("yt-dlp", _make_stub_probe(size_bytes=50_000)))
    reg_a.register_postprocessor(ff_a)
    orch_a = ExtractionOrchestrator(settings, reg_a, LocalArtifactStorage(settings))
    res_a = await orch_a.execute_download(
        "job_yt_trim_norm",
        DownloadRequest(
            url="https://youtu.be/YW7Uc5_ixtM",
            mode="audio",
            audio_format="mp3",
            normalize_audio=True,
            clip_start_seconds=10.0,
            clip_end_seconds=25.0,
        ),
    )
    assert res_a.download.status == DownloadStatus.COMPLETED
    assert "clip_media" not in ff_a.calls
    assert "extract_audio:norm=True" in ff_a.calls

    # Case B: direct-http does NOT clip -> FFmpeg runs BOTH clip_media AND extract_audio sequentially!
    ff_b = _MockFfmpeg()
    reg_b = EngineRegistry()
    reg_b.register_extractor(_StubAdapter("direct-http", _make_stub_probe(size_bytes=50_000)))
    reg_b.register_postprocessor(ff_b)
    orch_b = ExtractionOrchestrator(settings, reg_b, LocalArtifactStorage(settings))
    res_b = await orch_b.execute_download(
        "job_http_trim_norm",
        DownloadRequest(
            url="https://cdn.example.com/episode.m4a",
            mode="audio",
            audio_format="mp3",
            normalize_audio=True,
            clip_start_seconds=10.0,
            clip_end_seconds=25.0,
        ),
    )
    assert res_b.download.status == DownloadStatus.COMPLETED
    assert ff_b.calls == ["clip_media", "extract_audio:norm=True"]
