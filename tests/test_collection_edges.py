"""Playlist, gallery, caption, and range behavior at branch boundaries."""

from __future__ import annotations

import json
import zipfile
from unittest.mock import patch

import pytest
from PIL import Image
from pydantic import ValidationError

from neon_sync.adapters import AdapterError, GalleryDlAdapter, UrlContext, YtDlpAdapter
from neon_sync.adapters.gallery_dl import parse_gallery_dl_json
from neon_sync.adapters.ytdlp import normalize_ytdlp_json
from neon_sync.config import Settings
from neon_sync.models import DownloadPlan, DownloadRequest, ErrorCode, ProbeOptions
from neon_sync.models.selection import playlist_indices
from neon_sync.security import SubprocessResult, validate_and_canonicalize_url


@pytest.mark.parametrize("selection", ["0", "-1", "5-2", "1,,3", "1:9:0", "1-", ":5", "1-999999999"])
def test_invalid_or_unbounded_playlist_selection_is_rejected(selection):
    with pytest.raises(ValidationError):
        DownloadRequest(url="https://example.com/list", playlist_items=selection)


def test_playlist_ranges_preserve_positions_and_deduplicate():
    assert playlist_indices(" 51-53,52,60:64:2 ", limit=6) == [51, 52, 53, 60, 62, 64]


def test_playlist_browsing_is_independent_of_download_limit():
    adapter = YtDlpAdapter(Settings(max_playlist_items=3, playlist_probe_page_size=50))
    argv = adapter.build_probe_argv("https://example.com/list", ProbeOptions(playlist_start=151))
    assert argv[argv.index("--playlist-start") + 1] == "151"
    assert argv[argv.index("--playlist-end") + 1] == "200"


async def test_last_playlist_page_uses_absolute_positions_and_marks_completion():
    adapter = YtDlpAdapter(Settings())
    ctx = UrlContext(validate_and_canonicalize_url("https://example.com/list"), "j")
    payload = {"_type": "playlist", "playlist_count": 166, "entries": [
        {"id": str(index), "url": f"https://example.com/{index}"} for index in range(151, 167)
    ]}
    with patch("neon_sync.adapters.ytdlp.run_safe_subprocess", return_value=SubprocessResult(returncode=0, stdout=json.dumps(payload), stderr="")):
        result = await adapter.probe(ctx, ProbeOptions(playlist_start=151))
    assert [item.index for item in result.items] == list(range(151, 167))
    assert result.metadata.item_count_total == 166
    assert result.metadata.extra["playlist_page"] == {
        "start": 151, "end": 166, "limit": 50, "has_more": False, "next_start": None, "total": 166,
    }


def test_inaccessible_page_advances_to_later_accessible_items():
    ctx = UrlContext(validate_and_canonicalize_url("https://example.com/list"), "j")
    result = normalize_ytdlp_json({"_type": "playlist", "playlist_count": 166, "entries": [None] * 50}, ctx, version="1", probe_ms=0, playlist_start=51)
    assert result.items == []
    assert result.metadata.extra["playlist_page"]["next_start"] == 101


def test_unknown_total_stops_after_empty_later_page():
    ctx = UrlContext(validate_and_canonicalize_url("https://example.com/list"), "j")
    result = normalize_ytdlp_json({"_type": "playlist", "entries": []}, ctx, version="1", probe_ms=0, playlist_start=101)
    assert result.metadata.extra["playlist_page"]["has_more"] is False


@pytest.mark.parametrize("options", [
    {"clip_start_seconds": 15, "clip_end_seconds": 10},
    {"clip_start_seconds": 10, "clip_end_seconds": 10},
    {"gallery_range_start": 9, "gallery_range_end": 3},
    {"clip_start_seconds": float("nan")},
    {"clip_end_seconds": float("inf")},
    {"subtitle_languages": []},
])
def test_invalid_options_fail_before_work_is_queued(options):
    with pytest.raises(ValidationError):
        DownloadRequest(url="https://example.com/video", **options)


def test_clip_with_only_end_starts_at_zero():
    request = DownloadRequest(url="https://example.com/video", clip_end_seconds=8)
    assert request.clip_start_seconds == 0


def test_subtitle_playlist_selection_applies_to_the_requested_positions(tmp_path):
    adapter = YtDlpAdapter(Settings(max_playlist_items=3))
    plan = DownloadPlan(job_id="j", output_dir=tmp_path, subtitles_only=True, playlist_items="51-53")
    argv = adapter.build_download_argv("https://example.com/playlist", plan, collection=True)
    assert argv[argv.index("--playlist-items") + 1] == "51-53"
    assert "--playlist-end" not in argv
    assert "--ignore-errors" in argv
    with pytest.raises(AdapterError) as error:
        adapter.build_download_argv("https://example.com/playlist", plan.model_copy(update={"playlist_items": "1-4"}))
    assert error.value.code == ErrorCode.ERR_QUOTA_EXCEEDED


def test_playlist_normalization_keeps_gaps_and_mixed_media_and_skips_inaccessible():
    ctx = UrlContext(validate_and_canonicalize_url("https://example.com/list"), "j")
    result = normalize_ytdlp_json({
        "_type": "playlist", "playlist_count": 5,
        "entries": [
            None,
            {"id": "audio", "url": "https://example.com/song.mp3", "ext": "mp3", "playlist_index": 2},
            {"id": "private", "url": "https://example.com/private", "availability": "private"},
            {"id": "video", "url": "https://example.com/film.mp4", "ext": "mp4", "playlist_index": 4},
            {"id": "missing-url"},
        ],
    }, ctx, version="1", probe_ms=1)
    assert [(item.index, item.content_type) for item in result.items] == [(2, "audio"), (4, "video")]
    assert result.metadata.item_count_total == 5
    assert result.metadata.extra["collection_unavailable_count"] == 3


@pytest.mark.parametrize("entries", [[], [None], [{"id": "no-url"}]])
def test_empty_or_inaccessible_playlists_get_clear_errors(entries):
    ctx = UrlContext(validate_and_canonicalize_url("https://example.com/list"), "j")
    with pytest.raises(AdapterError, match="empty or has no accessible"):
        normalize_ytdlp_json({"_type": "playlist", "entries": entries}, ctx, version="1", probe_ms=0)


async def test_multilanguage_playlist_captions_are_delivered_together(tmp_path):
    adapter = YtDlpAdapter(Settings())
    ctx = UrlContext(validate_and_canonicalize_url("https://example.com/list"), "j", is_collection=True)
    plan = DownloadPlan(job_id="j", output_dir=tmp_path, subtitles_only=True, playlist_items="1-2")

    async def download(*args, **kwargs):
        for name in ["one.en.vtt", "one.hi.vtt", "two.en.vtt"]:
            (tmp_path / name).write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello")
        return SubprocessResult(returncode=0, stdout="", stderr="ERROR: private item skipped")

    with patch("neon_sync.adapters.ytdlp.run_safe_subprocess", side_effect=download):
        artifact = await adapter.download(ctx, plan)
    assert artifact.filename == "captions_archive.zip"
    assert artifact.warnings[0].code == "WARN_COLLECTION_PARTIAL"
    with zipfile.ZipFile(artifact.local_path) as archive:
        assert set(archive.namelist()) == {"one.en.vtt", "one.hi.vtt", "two.en.vtt"}


async def test_combined_playlist_quota_is_enforced_before_archiving(tmp_path):
    adapter = YtDlpAdapter(Settings())
    ctx = UrlContext(validate_and_canonicalize_url("https://example.com/list"), "j", is_collection=True)
    plan = DownloadPlan(job_id="j", output_dir=tmp_path, max_bytes=100)

    async def download(*args, **kwargs):
        for name in ["one.mp4", "two.mp4"]:
            (tmp_path / name).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 60)
        return SubprocessResult(returncode=0, stdout="", stderr="")

    with (
        patch("neon_sync.adapters.ytdlp.run_safe_subprocess", side_effect=download),
        pytest.raises(AdapterError) as error,
    ):
        await adapter.download(ctx, plan)
    assert error.value.code == ErrorCode.ERR_QUOTA_EXCEEDED


async def test_gallery_converts_and_resizes_each_photo_before_archiving(tmp_path):
    adapter = GalleryDlAdapter(Settings())
    ctx = UrlContext(validate_and_canonicalize_url("https://imgur.com/a/test"), "j")
    plan = DownloadPlan(job_id="j", output_dir=tmp_path, mode="image", image_format="webp", image_max_width=64)

    async def download(*args, **kwargs):
        for name in ["one.png", "two.png"]:
            Image.new("RGB", (128, 96), "red").save(tmp_path / name)
        return SubprocessResult(returncode=0, stdout="", stderr="")

    with patch("neon_sync.adapters.gallery_dl.run_safe_subprocess", side_effect=download):
        artifact = await adapter.download(ctx, plan)
    with zipfile.ZipFile(artifact.local_path) as archive:
        images = [name for name in archive.namelist() if name.endswith(".webp")]
        assert len(images) == 2
        for name in images:
            with archive.open(name) as file, Image.open(file) as image:
                assert image.size == (64, 48)
        assert not any(name.endswith(".png") for name in archive.namelist())


def test_single_video_gallery_item_uses_video_branch():
    ctx = UrlContext(validate_and_canonicalize_url("https://example.com/post"), "j")
    result = parse_gallery_dl_json(
        '[[3, "https://example.com/clip.mp4", {"extension": "mp4"}]]', ctx, version="1", probe_ms=0,
    )
    assert result.content_type == "video"


async def test_metadata_without_media_cannot_be_reported_as_success(tmp_path):
    ctx = UrlContext(validate_and_canonicalize_url("https://example.com/video"), "j")
    async def download(*args, **kwargs):
        (tmp_path / "info.json").write_text('{"title": "unavailable"}')
        return SubprocessResult(returncode=0, stdout="", stderr="")
    with (
        patch("neon_sync.adapters.ytdlp.run_safe_subprocess", side_effect=download),
        pytest.raises(AdapterError, match="Only metadata"),
    ):
        await YtDlpAdapter(Settings()).download(ctx, DownloadPlan(job_id="j", output_dir=tmp_path))
