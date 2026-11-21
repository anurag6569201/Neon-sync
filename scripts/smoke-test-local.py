#!/usr/bin/env python3
"""Live local end-to-end smoke test against a running Neon Sync server."""

from __future__ import annotations

import sys
import time

import httpx

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def main() -> int:
    with httpx.Client(base_url=BASE_URL, timeout=45.0) as client:
        print(f"=== 1. Health & Engine Registry ({BASE_URL}) ===")
        health = client.get("/v1/health")
        health.raise_for_status()
        hdata = health.json()
        print("Health:", hdata)

        engines = client.get("/v1/engines").json()
        for e in engines:
            print(
                f"  - {e['id']:14} | tier={e['tier']} | status={e['installation_status']:10} "
                f"| v={e['version']:12} | {e['license']}"
            )

        print("\n=== 2. SSRF Security Guard Check ===")
        ssrf = client.post("/v1/probe", json={"url": "http://169.254.169.254/latest/meta-data/"})
        assert ssrf.status_code == 403, f"Expected 403, got {ssrf.status_code}"
        print("  OK: IMDS SSRF blocked ->", ssrf.json())

        print("\n=== 3. Live Webpage Probe (https://example.com) ===")
        web = client.post("/v1/probe", json={"url": "https://example.com"})
        web.raise_for_status()
        wdata = web.json()
        print(
            f"  OK: type={wdata['content_type']} | title={wdata['title']!r} "
            f"| engine={wdata['engine']['engine_used']} ({wdata['engine']['probe_duration_ms']}ms)"
        )

        print("\n=== 4. Live YouTube Probe via yt-dlp + Deno + Safe Egress Proxy ===")
        yt_url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
        yt = client.post("/v1/probe", json={"url": yt_url})
        if yt.status_code == 200:
            ydata = yt.json()
            print(
                f"  OK: title={ydata['title']!r} | duration={ydata['duration_seconds']}s "
                f"| formats={len(ydata['available_formats'])} | engine={ydata['engine']['engine_used']} "
                f"({ydata['engine']['probe_duration_ms']}ms)"
            )
        else:
            print(f"  NOTE: YouTube probe returned HTTP {yt.status_code}: {yt.text[:300]}")

        print("\n=== 5. Live End-to-End Download Job (small public file) ===")
        job_resp = client.post(
            "/v1/jobs",
            json={"url": "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"},
        )
        job_resp.raise_for_status()
        job_id = job_resp.json()["job_id"]
        print(f"  Submitted job: {job_id}")

        final_job = None
        for _ in range(60):
            jr = client.get(f"/v1/jobs/{job_id}").json()
            if jr["status"] in {"completed", "failed", "cancelled"}:
                final_job = jr
                break
            time.sleep(0.25)

        assert final_job is not None, "Job did not finish in time"
        print(f"  Final status: {final_job['status']}")
        if final_job["status"] == "completed":
            dl = final_job["result"]["download"]
            print(f"  Artifact: {dl['filename']} ({dl['size_bytes']} bytes, sha256={dl['sha256'][:16]}...)")
            art = client.get(dl["artifact_url"])
            art.raise_for_status()
            assert art.content.startswith(b"%PDF-"), "Expected valid PDF bytes"
            print(f"  OK: Verified PDF download + Content-Disposition={art.headers.get('content-disposition')}")
        else:
            print("  Job error:", final_job.get("error"))
            return 1

    print("\nAll live local smoke tests passed!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
