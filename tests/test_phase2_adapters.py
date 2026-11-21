"""Tests for Phase-2 adapters: Streamlink (live streams) and N_m3u8DL-RE (HLS/DASH)."""

from __future__ import annotations

from pathlib import Path

import pytest

from neon_sync.adapters.base import AdapterError, UrlContext
from neon_sync.adapters.nm3u8dl import NM3u8DlReAdapter, inspect_manifest_security_and_variants
from neon_sync.adapters.streamlink import StreamlinkAdapter, normalize_streamlink_json
from neon_sync.config import Settings
from neon_sync.models import ContentType, ErrorCode
from neon_sync.security import validate_and_canonicalize_url


def test_streamlink_json_normalization_and_argv_security(tmp_path: Path):
    settings = Settings(egress_proxy_url="http://127.0.0.1:3128", max_livestream_duration_seconds=900)
    adapter = StreamlinkAdapter(settings)

    validated = validate_and_canonicalize_url("https://www.twitch.tv/shroud")
    ctx = UrlContext(validated=validated, job_id="job_live_1")
    assert adapter.quick_match(ctx).confidence == 96

    # Twitch VOD should score lower than live channel so yt-dlp wins VODs
    vod_ctx = UrlContext(
        validated=validate_and_canonicalize_url("https://www.twitch.tv/videos/123456"),
        job_id="job_vod_1",
    )
    assert adapter.quick_match(vod_ctx).confidence < 60

    probe_argv = adapter.build_probe_argv(ctx.url)
    assert "--json" in probe_argv
    assert "--no-config" in probe_argv
    assert "--http-proxy" in probe_argv
    assert probe_argv[-2] == "--"

    rec_argv = adapter.build_record_argv(ctx.url, "1080p60", tmp_path / "live.ts", 900)
    assert "--hls-duration" in rec_argv
    assert "00:15:00" in rec_argv

    raw = {
        "plugin": "twitch",
        "metadata": {"id": "live-42", "author": "shroud", "title": "Ranked Match", "category": "Gaming"},
        "streams": {
            "1080p60": {"type": "hls", "url": "https://video-weaver.example/1080.m3u8"},
            "720p": {"type": "hls", "url": "https://video-weaver.example/720.m3u8"},
            "best": {"type": "hls", "url": "https://video-weaver.example/1080.m3u8"},
        },
    }
    norm = normalize_streamlink_json(raw, ctx, version="8.6.3", probe_ms=610)
    assert norm.content_type == ContentType.LIVESTREAM
    assert norm.is_live is True
    assert norm.author is not None and norm.author.name == "shroud"
    assert len(norm.available_formats) == 2
    assert norm.available_formats[0].height == 1080
    assert norm.available_formats[0].fps == 60.0


def test_nm3u8dl_manifest_security_blocks_ssrf_and_drm():
    # 1. Malicious local-file/concat SSRF playlist blocked!
    malicious_m3u8 = "#EXTM3U\n#EXTINF:10.0,\nconcat:file:///etc/passwd\n"
    with pytest.raises(AdapterError) as exc1:
        inspect_manifest_security_and_variants(malicious_m3u8, "https://cdn.example.com/bad.m3u8")
    assert exc1.value.code == ErrorCode.ERR_SSRF_BLOCKED

    # 2. FairPlay / SAMPLE-AES DRM blocked cleanly!
    drm_m3u8 = (
        "#EXTM3U\n"
        '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key",KEYFORMAT="com.apple.streamingkeydelivery"\n'
        "#EXTINF:6.0,\nseg1.ts\n"
    )
    with pytest.raises(AdapterError) as exc2:
        inspect_manifest_security_and_variants(drm_m3u8, "https://cdn.example.com/drm.m3u8")
    assert exc2.value.code == ErrorCode.ERR_DRM_PROTECTED

    # 3. Widevine DASH DRM blocked cleanly!
    drm_mpd = '<MPD><ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/></MPD>'
    with pytest.raises(AdapterError) as exc3:
        inspect_manifest_security_and_variants(drm_mpd, "https://cdn.example.com/drm.mpd")
    assert exc3.value.code == ErrorCode.ERR_DRM_PROTECTED

    # 4. Valid master HLS playlist parsed cleanly into formats!
    valid_m3u8 = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720\n"
        "720p.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=5500000,RESOLUTION=1920x1080\n"
        "1080p.m3u8\n"
    )
    info = inspect_manifest_security_and_variants(valid_m3u8, "https://cdn.example.com/master.m3u8")
    assert info["protocol"] == "hls"
    assert info["is_live"] is False
    assert len(info["formats"]) == 2
    assert info["formats"][1].resolution == "1920x1080"


def test_nm3u8dl_argv_and_matching(tmp_path: Path):
    settings = Settings(egress_proxy_url="http://127.0.0.1:3128")
    adapter = NM3u8DlReAdapter(settings)
    ctx = UrlContext(
        validated=validate_and_canonicalize_url("https://cdn.example.com/video/master.m3u8"),
        job_id="job_hls_1",
    )
    assert adapter.quick_match(ctx).confidence == 97

    argv = adapter.build_download_argv(ctx.url, tmp_path, "stream_abc", is_live=True)
    assert "--custom-proxy" in argv
    assert "--live-record-limit" in argv
    assert "--thread-count" in argv


def test_playwright_sniffer_classification_segment_filter_and_js_shell():
    from neon_sync.adapters.browser_fallback import (
        PlaywrightFallbackAdapter,
        classify_sniffed_request,
        detect_js_shell_html,
        is_segment_url,
        rank_sniffed_candidates,
    )

    # Raw stream chunks must be ignored
    assert is_segment_url("https://cdn.example.com/seg_0042.ts") is True
    assert is_segment_url("https://cdn.example.com/video/init.mp4") is True
    assert is_segment_url("https://cdn.example.com/chunk.m4s?bytestart=1024") is True
    assert is_segment_url("https://cdn.example.com/master.m3u8") is False

    # Ad tracker video ignored
    assert classify_sniffed_request("https://doubleclick.net/ad.mp4", "video/mp4", 500_000) is None
    # Tiny probe chunk ignored
    assert classify_sniffed_request("https://cdn.example.com/probe.mp4", "video/mp4", 1024) is None

    c_hls = classify_sniffed_request("https://cdn.example.com/master.m3u8", "application/vnd.apple.mpegurl")
    c_mp4 = classify_sniffed_request("https://cdn.example.com/video.mp4", "video/mp4", 5_000_000)
    c_vtt = classify_sniffed_request("https://cdn.example.com/en.vtt", "text/vtt")
    assert c_hls is not None and c_mp4 is not None and c_vtt is not None

    ranked = rank_sniffed_candidates([c_vtt, c_mp4, c_hls, c_hls])
    assert len(ranked) == 3
    assert ranked[0].kind == "hls"  # Master manifest ranked above progressive & subtitle
    assert ranked[1].kind == "video"
    assert ranked[2].kind == "subtitle"

    assert detect_js_shell_html('<html><body><div id="__next"></div></body></html>') is True
    assert detect_js_shell_html("<html><body><p>Static blog post</p></body></html>") is False

    # Manifest carries requires_browser=True so Tier-3 is skipped unless opted in
    pw = PlaywrightFallbackAdapter(Settings())
    assert pw.manifest().cost_profile.requires_browser is True
    assert pw.manifest().tier == 3
