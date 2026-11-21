"""Security tests for URL SSRF validation, Safe Egress Proxy, subprocess & file guards."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

from neon_sync.models import ErrorCode
from neon_sync.security import (
    CancellationToken,
    FileValidationError,
    SafeEgressProxy,
    SubprocessCancelledError,
    SubprocessTimeoutError,
    UrlValidationError,
    build_content_disposition,
    detect_magic_mime,
    ensure_safe_path,
    is_public_ip,
    run_safe_subprocess,
    sanitize_filename,
    validate_and_canonicalize_url,
)


@pytest.mark.parametrize(
    ("ip", "expected"),
    [
        ("8.8.8.8", True),
        ("1.1.1.1", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("10.0.0.1", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),  # AWS/GCP/Azure IMDS
        ("100.64.0.1", False),       # CGNAT / Tailscale
        ("0.0.0.0", False),
        ("::1", False),
        ("fc00::1", False),
        ("fe80::1", False),
        ("::ffff:127.0.0.1", False),         # IPv4-mapped IPv6 loopback!
        ("::ffff:169.254.169.254", False),   # IPv4-mapped IPv6 IMDS!
        ("64:ff9b::10.0.0.1", False),        # NAT64
        ("not-an-ip", False),
    ],
)
def test_is_public_ip(ip: str, expected: bool):
    assert is_public_ip(ip) is expected


@pytest.mark.parametrize(
    ("bad_url", "expected_code"),
    [
        ("file:///etc/passwd", ErrorCode.ERR_INVALID_URL),
        ("ftp://example.com/file.zip", ErrorCode.ERR_INVALID_URL),
        ("javascript:alert(1)", ErrorCode.ERR_INVALID_URL),
        ("https://user:pass@example.com/v", ErrorCode.ERR_INVALID_URL),
        ("http://localhost:8000/admin", ErrorCode.ERR_SSRF_BLOCKED),
        ("http://127.0.0.1/secret", ErrorCode.ERR_SSRF_BLOCKED),
        ("http://169.254.169.254/latest/meta-data/", ErrorCode.ERR_SSRF_BLOCKED),
        ("http://[::ffff:127.0.0.1]/", ErrorCode.ERR_SSRF_BLOCKED),
        ("http://metadata.google.internal/computeMetadata/v1/", ErrorCode.ERR_SSRF_BLOCKED),
        ("http://2130706433/", ErrorCode.ERR_SSRF_BLOCKED),
        ("https://example.com:8080/video.mp4", ErrorCode.ERR_SSRF_BLOCKED),
    ],
)
def test_url_validator_blocks_unsafe_urls(bad_url: str, expected_code: ErrorCode):
    with pytest.raises(UrlValidationError) as exc_info:
        validate_and_canonicalize_url(bad_url)
    assert exc_info.value.code == expected_code


def test_url_canonicalization_normalizes_shortlinks_and_strips_trackers():
    v1 = validate_and_canonicalize_url("https://youtu.be/dQw4w9WgXcQ?si=abc&utm_source=x&t=30")
    assert v1.canonical_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30"
    assert v1.domain == "youtube.com"

    v2 = validate_and_canonicalize_url("https://vxtwitter.com/user/status/123?utm_medium=social#frag")
    assert v2.canonical_url == "https://x.com/user/status/123"
    assert v2.domain == "x.com"


async def test_safe_egress_proxy_blocks_localhost_and_imds():
    proxy = SafeEgressProxy(host="127.0.0.1", port=0, allow_private=False)
    await proxy.start()
    try:
        async with httpx.AsyncClient(proxy=proxy.url, timeout=5.0) as client:
            r1 = await client.get("http://127.0.0.1/secret")
            assert r1.status_code == 403
            assert b"Safe Egress Proxy" in r1.content

            r2 = await client.get("http://169.254.169.254/latest/meta-data/")
            assert r2.status_code == 403

            r3 = await client.get("http://localhost/admin")
            assert r3.status_code == 403

            # Non-80/443 port blocked
            r4 = await client.get("http://example.com:8080/")
            assert r4.status_code == 403

        assert proxy.blocked_count >= 4
    finally:
        await proxy.stop()


async def test_subprocess_guard_runs_timeout_and_cancels():
    res = await run_safe_subprocess([sys.executable, "-c", "print('hello_guard')"], timeout=5.0)
    assert res.returncode == 0
    assert "hello_guard" in res.stdout

    # Timeout enforcement
    with pytest.raises(SubprocessTimeoutError):
        await run_safe_subprocess([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)

    # Cooperative/preemptive cancellation enforcement
    token = CancellationToken()

    async def _cancel_soon():
        await asyncio.sleep(0.15)
        token.cancel("user requested cancel")

    asyncio.create_task(_cancel_soon())
    with pytest.raises(SubprocessCancelledError):
        await run_safe_subprocess(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=5.0,
            cancel_token=token,
        )


def test_file_guard_sanitization_traversal_and_magic_bytes(tmp_path: Path):
    assert sanitize_filename("../../etc/passwd") == "etc_passwd"
    assert sanitize_filename("My Video (1080p)!.mp4") == "My_Video_1080p_.mp4"
    cd = build_content_disposition("report 2026.pdf")
    assert cd.startswith('attachment; filename="report_2026.pdf"')

    safe = ensure_safe_path(tmp_path, tmp_path / "job1" / "out.mp4")
    assert str(safe).startswith(str(tmp_path.resolve()))
    with pytest.raises(FileValidationError):
        ensure_safe_path(tmp_path, tmp_path / ".." / "outside.txt")

    assert detect_magic_mime(b"%PDF-1.7\n...") == ("application/pdf", False)
    assert detect_magic_mime(b"\x89PNG\r\n\x1a\nxxxx") == ("image/png", False)
    assert detect_magic_mime(b"\x00\x00\x00\x18ftypmp42\x00\x00") == ("video/mp4", False)
    assert detect_magic_mime(b"\x7fELF\x02\x01\x01\x00") == ("application/x-elf", True)
    assert detect_magic_mime(b"<!DOCTYPE html><html>") == ("text/html", False)
