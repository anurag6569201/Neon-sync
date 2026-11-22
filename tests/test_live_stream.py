"""Tests for Neon Sync Live Network Stream Resolver, Range Proxy, HLS Rewriter & VLC .m3u Export."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from neon_sync.api import create_app
from neon_sync.config import Settings

_FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + (b"NEONSYNCSTREAM" * 16)
_FAKE_M3U8 = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:6\n"
    "#EXTINF:6.0,\n"
    "seg_001.ts\n"
    "#EXT-X-ENDLIST\n"
)
_FAKE_TS = b"\x47\x40\x00\x10" + b"\xaa" * 184


class _LiveStreamMediaHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_HEAD(self) -> None:
        if self.path == "/movie.mp4":
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(_FAKE_MP4)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
        elif self.path == "/live/index.m3u8":
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(_FAKE_M3U8)))
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/movie.mp4":
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                start_s, end_s = rng.replace("bytes=", "").split("-", 1)
                start = int(start_s)
                end = int(end_s) if end_s else len(_FAKE_MP4) - 1
                chunk = _FAKE_MP4[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(_FAKE_MP4)}")
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(chunk)
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(_FAKE_MP4)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(_FAKE_MP4)
        elif self.path == "/live/index.m3u8":
            raw = _FAKE_M3U8.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == "/live/seg_001.ts":
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(len(_FAKE_TS)))
            self.end_headers()
            self.wfile.write(_FAKE_TS)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture()
def stream_media_server() -> str:
    server = HTTPServer(("127.0.0.1", 0), _LiveStreamMediaHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


async def test_live_stream_resolve_range_seek_and_vlc_m3u_playlist(
    stream_media_server: str,
    tmp_path: Path,
):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        auth_db_path=tmp_path / "auth.sqlite3",
        allow_private_addresses=True,
        min_free_disk_bytes=1024,
    )
    app = create_app(settings)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        mp4_url = f"{stream_media_server}/movie.mp4"

        # 1. Resolve live stream without downloading file to disk
        res = await client.post("/v1/stream/resolve", json={"url": mp4_url, "mode": "video"})
        assert res.status_code == 200
        data = res.json()
        assert data["service"] == "neon-sync"
        assert len(data["tracks"]) >= 1
        track = data["tracks"][0]
        assert track["protocol"] == "progressive"
        assert track["stream_url"].startswith("/v1/stream/play/ns_str_")

        # 2. HTTP 206 Partial Content byte-range seeking works like VLC!
        partial = await client.get(track["stream_url"], headers={"Range": "bytes=0-31"})
        assert partial.status_code == 206
        assert partial.content == _FAKE_MP4[:32]
        assert partial.headers["content-range"].startswith("bytes 0-31/")
        assert partial.headers["accept-ranges"] == "bytes"

        # 3. Full stream works too
        full = await client.get(track["stream_url"])
        assert full.status_code == 200
        assert full.content == _FAKE_MP4

        # 4. VLC / IINA .m3u playlist export points to Neon Sync's live stream URL
        m3u = await client.get(track["m3u_url"])
        assert m3u.status_code == 200
        assert "#EXTM3U" in m3u.text
        assert f"http://testserver{track['stream_url']}" in m3u.text


async def test_live_stream_hls_manifest_rewriting_and_signature_protection(
    stream_media_server: str,
    tmp_path: Path,
):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        auth_db_path=tmp_path / "auth.sqlite3",
        allow_private_addresses=True,
        min_free_disk_bytes=1024,
    )
    app = create_app(settings)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        hls_url = f"{stream_media_server}/live/index.m3u8"
        res = await client.post("/v1/stream/resolve", json={"url": hls_url, "mode": "video"})
        assert res.status_code == 200
        track = res.json()["tracks"][0]
        assert track["protocol"] == "hls"
        assert track["stream_url"].endswith("/manifest.m3u8")

        # Manifest rewrites relative seg_001.ts into signed Neon Sync proxy URL
        manifest = await client.get(track["stream_url"])
        assert manifest.status_code == 200
        assert "#EXTM3U" in manifest.text
        seg_line = next(ln for ln in manifest.text.splitlines() if ln.startswith("/v1/stream/hls/"))
        assert "sig=" in seg_line

        # Signed segment streams raw MPEG-TS bytes cleanly
        seg_resp = await client.get(seg_line)
        assert seg_resp.status_code == 200
        assert seg_resp.content == _FAKE_TS

        # Forged signature MUST be blocked with 403 (prevents open proxy / SSRF abuse!)
        forged = await client.get(
            f"/v1/stream/hls/{track['stream_id']}/asset",
            params={"u": "http://169.254.169.254/latest/meta-data/", "sig": "0" * 24},
        )
        assert forged.status_code == 403


async def test_live_stream_blocks_ssrf_at_resolve(tmp_path: Path):
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        auth_db_path=tmp_path / "auth.sqlite3",
        allow_private_addresses=False,
        min_free_disk_bytes=1024,
    )
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        blocked = await client.post(
            "/v1/stream/resolve",
            json={"url": "http://169.254.169.254/latest/meta-data/"},
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["code"] == "ERR_SSRF_BLOCKED"
