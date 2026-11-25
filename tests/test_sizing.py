"""Sizes remain exact, approximate, or unavailable across provider metadata."""

from __future__ import annotations

import pytest

from neon_sync.adapters import UrlContext
from neon_sync.adapters.direct_http import _parse_rss_or_atom_feed
from neon_sync.adapters.gallery_dl import parse_gallery_dl_json
from neon_sync.adapters.ytdlp import normalize_ytdlp_json
from neon_sync.models.sizing import source_size
from neon_sync.security import validate_and_canonicalize_url


@pytest.mark.parametrize("raw,expected", [
    ({"filesize": 0}, (0, False)),
    ({"filesize": 123, "filesize_approx": 456}, (123, False)),
    ({"filesize_approx": 456}, (456, True)),
    ({"duration": 60, "tbr": 128}, (960000, True)),
    ({"duration": 60, "vbr": 1000, "abr": 128}, (8460000, True)),
    ({"filesize": -5}, (None, False)),
    ({"filesize": float("nan")}, (None, False)),
    ({"duration": 1e308, "tbr": 1e308}, (None, False)),
])
def test_source_sizes_preserve_accuracy(raw, expected):
    assert source_size(raw) == expected


def test_live_duration_is_not_used_as_a_fixed_file_size():
    assert source_size({"duration": 60, "tbr": 128}, live=True) == (None, False)
    assert source_size({"filesize": 123}, live=True) == (None, False)


def test_video_formats_and_playlist_entries_keep_size_provenance():
    ctx = UrlContext(validate_and_canonicalize_url("https://example.com/list"), "j")
    result = normalize_ytdlp_json({
        "_type": "playlist", "playlist_count": 2, "entries": [
            {"url": "https://example.com/a", "filesize": 123},
            {"url": "https://example.com/b", "duration": 60, "tbr": 128},
        ],
        "duration": 60,
        "formats": [{"format_id": "audio", "ext": "m4a", "vcodec": "none", "acodec": "aac", "tbr": 128}],
    }, ctx, version="1", probe_ms=1)
    assert result.items[0].size_bytes == 123
    assert result.items[0].size_is_approx is False
    assert result.items[1].size_bytes == 960000
    assert result.items[1].size_is_approx is True
    assert result.available_formats[0].size_bytes == 960000
    assert result.available_formats[0].size_is_approx is True


def test_gallery_exposes_known_image_bytes():
    ctx = UrlContext(validate_and_canonicalize_url("https://example.com/post"), "j")
    result = parse_gallery_dl_json('[[3,"https://example.com/image.jpg",{"filesize":12345,"extension":"jpg"}]]', ctx, version="1", probe_ms=0)
    assert result.items[0].size_bytes == result.size_bytes == result.available_formats[0].size_bytes == 12345
    assert result.size_is_approx is False


def test_rss_enclosure_lengths_become_item_sizes():
    feed = _parse_rss_or_atom_feed('<rss><channel><title>Podcast</title><item><title>Episode</title><enclosure url="https://example.com/ep.mp3" type="audio/mpeg" length="12345" /></item></channel></rss>', "https://example.com/feed")
    assert feed["items"][0].size_bytes == 12345
