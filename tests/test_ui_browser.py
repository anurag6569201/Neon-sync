"""Opt-in browser regressions: NEON_SYNC_BROWSER_TESTS=1 pytest tests/test_ui_browser.py."""

from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest
import uvicorn

from neon_sync.api import create_app
from neon_sync.config import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("NEON_SYNC_BROWSER_TESTS") != "1",
    reason="Set NEON_SYNC_BROWSER_TESTS=1 with Playwright Chromium installed.",
)


@pytest.fixture
def ui_server(tmp_path, monkeypatch):
    app = create_app(Settings(_env_file=None, storage_dir=tmp_path / "artifacts", job_db_path=None))
    # Metadata-only background checks must never contact external sites in UI regressions.
    from neon_sync.models import EngineProvenance, NormalizedMediaOutput, SourceInfo

    async def unavailable_probe(body):
        return NormalizedMediaOutput(
            job_id="size", original_url=body.url, url=body.url, content_type="video",
            source=SourceInfo(domain="example.com"), engine=EngineProvenance(engine_used="test", engine_version="test"),
        )

    monkeypatch.setattr(app.state.container.orchestrator, "probe", unavailable_probe)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        app.state.container.storage.close()


def test_playlist_picker_scrolls_without_panning_and_keeps_buttons_on_one_line(ui_server):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(ui_server) else route.abort())
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/v1/jobs?*", lambda route: route.fulfill(json=[]))
            page.route("**/v1/probe", lambda route: route.fulfill(json={
                "content_type": "playlist", "title": "Electrical Machines", "source": {"domain": "example.com"},
                "metadata": {"item_count_total": 50, "extra": {"collection_total_known": True}},
                "items": [{"index": i, "url": f"https://example.com/{i}", "title": f"Electrical Machines | Lecture {i}", "content_type": "video", "duration_seconds": 2840} for i in range(1, 51)],
            }))
            page.goto(ui_server, wait_until="networkidle")
            page.locator("#url-input").fill("https://example.com/playlist")
            page.locator("#download-btn").click()
            page.get_by_role("button", name="Playlist Video", exact=True).press("Enter")
            page.get_by_role("button", name="Custom Selection", exact=True).press("Enter")
            picker = page.locator("[data-pl-list]")
            picker.wait_for()
            for theme in ["dark", "light"]:
                if page.locator("html").get_attribute("data-theme") != theme:
                    page.locator("#workspace-menu > summary").click()
                    page.locator("#theme-btn").click()
                    page.keyboard.press("Escape")
                for selector in ['[data-pl-quick="1-5"]', '[data-pl-quick="all"]']:
                    button = page.locator(selector)
                    assert button.evaluate("el => {const range = document.createRange(); range.selectNodeContents(el); return range.getClientRects().length;}") == 1
                    assert button.evaluate("el => getComputedStyle(el).whiteSpace") == "nowrap"
                picker.evaluate("el => el.scrollTop = 0")
                picker.hover()
                before = page.locator("#world").get_attribute("style")
                page.mouse.wheel(0, 400)
                page.wait_for_function("document.querySelector('[data-pl-list]').scrollTop > 0")
                assert page.locator("#world").get_attribute("style") == before
                picker.evaluate("el => el.scrollTop = el.scrollHeight")
                page.mouse.wheel(0, 400)
                page.wait_for_timeout(100)
                assert page.locator("#world").get_attribute("style") == before
            page.locator('[data-pl-quick="all"]').click()
            assert page.locator("[data-pl-range]").input_value() == "1-50"
            page.locator('[data-pl-quick="1-5"]').click()
            assert page.locator("[data-pl-range]").input_value() == "1-5"
            assert not errors
        finally:
            browser.close()


def test_playlist_picker_loads_all_166_items_preserves_selection_and_submits_late_positions(ui_server):
    playwright = pytest.importorskip("playwright.sync_api")
    requests = []
    submissions = []

    def probe(route):
        start = route.request.post_data_json.get("playlist_start", 1)
        requests.append(start)
        if start == 51 and requests.count(51) == 1:
            route.fulfill(status=503, json={"detail": {"message": "Playlist temporarily unavailable"}})
            return
        end = min(start + 49, 166)
        route.fulfill(json={
            "content_type": "playlist", "title": "Electrical Machines", "source": {"domain": "example.com"},
            "metadata": {"item_count_total": 166, "extra": {
                "collection_total_known": True,
                "playlist_page": {"start": start, "end": end, "limit": 50, "has_more": end < 166, "next_start": end + 1 if end < 166 else None, "total": 166},
            }},
            "items": [{"index": i, "url": f"https://example.com/{i}", "title": f"Electrical Machines | Lecture {i}", "content_type": "video", "duration_seconds": 2840} for i in range(start, end + 1)],
        })

    def submit(route):
        submissions.append(route.request.post_data_json)
        route.fulfill(status=422, json={"detail": {"message": "Download request verified"}})

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1100}, reduced_motion="reduce")
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(ui_server) else route.abort())
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/v1/jobs?*", lambda route: route.fulfill(json=[]))
            page.route("**/v1/probe", probe)
            page.route("**/v1/jobs", submit)
            page.goto(ui_server, wait_until="networkidle")
            page.locator("#url-input").fill("https://example.com/playlist")
            page.locator("#download-btn").click()
            page.get_by_role("button", name="Playlist Video", exact=True).press("Enter")
            page.get_by_role("button", name="Custom Selection", exact=True).press("Enter")
            page.get_by_text("50 of 166 loaded", exact=True).wait_for()
            page.locator('[data-pl-chk="1"]').uncheck()
            page.locator("[data-pl-filter]").fill("Lecture 166")
            page.get_by_text("No matches in the loaded items.", exact=False).wait_for()
            page.get_by_role("button", name="Load more", exact=True).click()
            page.get_by_text("Playlist temporarily unavailable", exact=False).wait_for()
            assert page.locator("[data-pl-range]").input_value() == "2-5"
            page.get_by_role("button", name="Load more", exact=True).click()
            page.get_by_text("100 of 166 loaded", exact=True).wait_for()
            assert page.locator("[data-pl-range]").input_value() == "2-5"
            assert page.locator("[data-pl-filter]").input_value() == "Lecture 166"
            page.get_by_role("button", name="Load all", exact=True).click()
            page.get_by_text("166 of 166 loaded", exact=True).wait_for()
            assert page.locator("[data-pl-chk]").count() == 166
            assert page.locator('[data-pl-chk="166"]').is_visible()
            assert page.locator("[data-pl-load]").count() == 0
            assert requests == [1, 51, 51, 101, 151]
            assert page.locator("[data-pl-range]").input_value() == "2-5"
            page.locator('[data-pl-quick="all"]').click()
            assert page.locator("[data-run-inline-leaf]").is_disabled()
            page.get_by_text("166 selected. Choose up to", exact=False).wait_for()
            page.locator('[data-pl-quick="none"]').click()
            assert page.locator("[data-pl-range]").input_value() == ""
            assert page.locator("[data-pl-chk]:checked").count() == 0
            page.locator("[data-pl-range]").fill("151-166")
            assert page.locator('[data-pl-chk="166"]').is_checked()
            assert not page.locator("[data-run-inline-leaf]").is_disabled()
            if screenshot := os.environ.get("NEON_SYNC_UI_SCREENSHOT"):
                page.locator("[data-pl-filter]").fill("")
                page.locator("[data-pl-list]").evaluate("el => el.scrollTop = el.scrollHeight")
                page.locator("#workspace-menu > summary").click()
                page.locator("#theme-btn").click()
                page.keyboard.press("Escape")
                page.locator(".choice-has-preview").screenshot(path=screenshot)
            page.locator("[data-run-inline-leaf]").click()
            assert submissions == []
            page.get_by_role("button", name="720p · HD", exact=True).press("Enter")
            assert submissions == []
            page.get_by_role("button", name="MP4 · H.264", exact=True).press("Enter")
            page.get_by_text("Download request verified", exact=True).wait_for()
            assert submissions[-1]["playlist_items"] == "151-166"
            assert submissions[-1]["max_items"] == 50
            assert submissions[-1]["video_quality"] == "720"
            assert submissions[-1]["video_container"] == "mp4"
            assert not errors
        finally:
            browser.close()


def _settle_ui(page):
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


def _file_probe(title):
    return {"title": title, "content_type": "file", "source": {"domain": "example.com"}, "available_formats": [{"mime_type": "application/pdf"}]}


@pytest.mark.parametrize("mode,selection", [("video", "68"), ("video", "1-3,68"), ("audio", "68"), ("audio", "2,68")])
def test_custom_playlist_requires_explicit_quality_and_format_before_submission(ui_server, mode, selection):
    playwright = pytest.importorskip("playwright.sync_api")
    submissions = []

    def submit(route):
        submissions.append(route.request.post_data_json)
        route.fulfill(status=422, json={"detail": {"message": "Selected settings verified"}})

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1600, "height": 1100}, reduced_motion="reduce")
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(ui_server) else route.abort())
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/v1/jobs?*", lambda route: route.fulfill(json=[]))
            page.route("**/v1/jobs", submit)
            page.route("**/v1/probe", lambda route: route.fulfill(json={
                "content_type": "playlist", "title": "Electrical Machines", "source": {"domain": "example.com"},
                "metadata": {"item_count_total": 166, "extra": {"collection_total_known": True}},
                "items": [{"index": i, "url": f"https://example.com/{i}", "title": f"Lecture {i}", "content_type": "video"} for i in range(1, 101)],
            }))
            page.goto(ui_server, wait_until="networkidle")
            page.locator("#url-input").fill("https://example.com/playlist")
            page.locator("#download-btn").click()
            page.get_by_role("button", name=f"Playlist {mode.title()}", exact=True).press("Enter")
            page.get_by_role("button", name="Custom Selection", exact=True).press("Enter")
            page.locator("[data-pl-range]").fill(selection)
            next_label = "Choose video quality" if mode == "video" else "Choose audio format"
            assert page.get_by_role("button", name=next_label, exact=True).is_visible()
            # Enter follows the same selection flow as the button, without queuing work.
            page.locator("[data-pl-range]").press("Enter")
            _settle_ui(page)
            assert submissions == []
            assert page.locator(".node-result").count() == 0
            if mode == "video":
                page.get_by_role("button", name="1080p · Full HD", exact=True).wait_for()
                page.get_by_role("button", name="480p · SD", exact=True).press("Enter")
                assert submissions == []
                # Changing the picker does not silently change an existing quality branch.
                page.locator("[data-pl-range]").fill("5-6")
                page.get_by_role("button", name="MP4 · H.264", exact=True).press("Enter")
            else:
                page.get_by_role("button", name="Opus", exact=True).press("Enter")
                assert submissions == []
                page.locator("[data-pl-range]").fill("5-6")
                page.get_by_role("button", name="192k · Balanced", exact=True).press("Enter")
            page.get_by_text("Selected settings verified", exact=True).wait_for()
            assert len(submissions) == 1
            assert submissions[0]["playlist_items"] == selection
            assert submissions[0]["mode"] == mode
            if mode == "video":
                assert submissions[0]["video_quality"] == "480"
                assert submissions[0]["video_container"] == "mp4"
                assert submissions[0]["video_codec"] == "h264"
            else:
                assert submissions[0]["audio_format"] == "opus"
                assert submissions[0]["audio_bitrate"] == "192k"
                assert submissions[0]["normalize_audio"] is False
            assert not errors
        finally:
            browser.close()


def test_clear_ignores_old_probe_even_when_the_same_link_is_added_again(ui_server):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(ui_server) else route.abort())
            page = context.new_page()
            errors, probes = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/v1/jobs?*", lambda route: route.fulfill(json=[]))
            def hold_probe(route):
                probes.append(route)
                page.evaluate("count => window.heldProbes = count", len(probes))
            page.route("**/v1/probe", hold_probe)
            page.goto(ui_server, wait_until="networkidle")
            for selector in ["#paste-btn", "#watch-live-btn", "#coord-readout", ".dock-telemetry"]:
                assert page.locator(selector).count() == 0
            page.locator("#url-input").fill("https://example.com/report.pdf")
            page.locator("#download-btn").click()
            page.wait_for_function("window.heldProbes === 1")
            page.get_by_text("Inspecting", exact=True).wait_for()
            page.locator("#clear-canvas-btn").click()
            assert page.locator(".node").count() == 0
            assert page.locator("#activity-status-text").inner_text() == "Idle"
            page.locator("#url-input").fill("https://example.com/report.pdf")
            page.locator("#download-btn").click()
            page.wait_for_function("window.heldProbes === 2")
            probes[0].fulfill(json=_file_probe("Old response"))
            _settle_ui(page)
            assert page.get_by_text("Old response", exact=True).count() == 0
            assert page.locator(".node-source").count() == 1
            assert page.locator(".node-choice").count() == 0
            assert page.locator("#activity-status-text").inner_text() == "Inspecting"
            probes[1].fulfill(json=_file_probe("Fresh response"))
            page.get_by_role("heading", name="Fresh response", exact=True).wait_for()
            assert page.locator(".node-source").count() == 1
            page.locator(".node-source [data-node-del]").click()
            assert page.locator(".node").count() == 0
            page.locator("#add-note-btn").click()
            page.locator("[data-note-input]").fill("Clear this note too")
            page.locator("#dock-batch-btn").click()
            page.locator("#batch-input").fill("https://example.com/a\nhttps://example.com/b")
            page.locator("#clear-batch-btn").click()
            assert page.locator("#batch-input").input_value() == ""
            assert page.locator("#batch-count").inner_text() == "0 links"
            page.locator("#batch-input").fill("https://example.com/a")
            page.locator("#clear-canvas-btn").click()
            assert page.locator(".node").count() == 0
            assert page.locator("#batch-input").input_value() == ""
            assert page.locator("#dock-batch-btn").get_attribute("aria-expanded") == "false"
            page.reload(wait_until="networkidle")
            assert page.locator(".node").count() == 0
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("pending", ["history", "submission"])
def test_clear_keeps_late_jobs_off_canvas_after_reload(ui_server, pending):
    playwright = pytest.importorskip("playwright.sync_api")
    job = {"job_id": "job_after_clear", "url": "https://example.com/report.pdf", "status": "queued", "request": {"url": "https://example.com/report.pdf", "mode": "direct"}}
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(ui_server) else route.abort())
            page = context.new_page()
            held, errors = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            def history(route):
                if pending == "history" and not held:
                    held.append(route)
                    page.evaluate("window.pendingHistory = true")
                else:
                    route.fulfill(json=[job] if held else [])
            def submission(route):
                held.append(route)
                page.evaluate("window.pendingSubmission = true")
            page.route("**/v1/jobs?*", history)
            page.route("**/v1/jobs", submission)
            page.route("**/v1/probe", lambda route: route.fulfill(json=_file_probe("Report")))
            page.goto(ui_server, wait_until="domcontentloaded" if pending == "history" else "networkidle")
            if pending == "submission":
                page.locator("#url-input").fill(job["url"])
                page.locator("#download-btn").click()
                page.get_by_role("button", name="Direct Download", exact=True).click()
                page.wait_for_function("window.pendingSubmission")
            else:
                page.wait_for_function("window.pendingHistory")
            page.locator("#clear-canvas-btn").click()
            held[0].fulfill(status=202 if pending == "submission" else 200, json=job if pending == "submission" else [job])
            page.wait_for_function("JSON.parse(localStorage.getItem('neon_sync_spatial_layout_v5')).hiddenJobIds.includes('job_after_clear')")
            _settle_ui(page)
            assert page.locator(".node").count() == 0
            page.reload(wait_until="networkidle")
            assert page.locator(".node").count() == 0
            page.locator("#recent-jobs-btn").click()
            page.get_by_role("button", name="View on canvas", exact=True).wait_for()
            assert not errors
        finally:
            browser.close()


def test_compact_toolbar_and_batch_controls_fit_small_viewports(ui_server):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(ui_server) else route.abort())
            page = context.new_page()
            page.route("**/v1/jobs?*", lambda route: route.fulfill(json=[]))
            page.goto(ui_server, wait_until="networkidle")
            for width in [1440, 1200, 768, 390, 320]:
                page.set_viewport_size({"width": width, "height": 900})
                for selector in ["#recent-jobs-btn", "#status-pill", "#workspace-menu > summary", "#url-input", "#download-btn", "#clear-canvas-btn"]:
                    box = page.locator(selector).bounding_box()
                    assert box and box["x"] >= 0 and box["x"] + box["width"] <= width + 1, (width, selector, box)
                page.locator("#dock-batch-btn").click()
                page.locator("#batch-input").fill("https://example.com/test")
                page.locator("#clear-canvas-btn").click()
                assert page.locator("#batch-input").input_value() == ""
                assert page.locator("#dock-batch-btn").get_attribute("aria-expanded") == "false"
            page.locator("#workspace-menu > summary").click()
            page.locator("#autosave-pill").click()
            assert page.locator("#autosave-pill").get_attribute("aria-pressed") == "false"
            page.locator("#theme-btn").click()
            assert page.locator("html").get_attribute("data-theme") == "light"
            page.keyboard.press("Escape")
            assert not page.locator("#workspace-menu").evaluate("el => el.open")
            assert page.locator(".toast").filter(has_text="Canvas cleared").count() <= 1
            if screenshot := os.environ.get("NEON_SYNC_TOOLBAR_SCREENSHOT"):
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.screenshot(path=screenshot)
        finally:
            browser.close()


def test_video_quality_cards_show_sizes_without_double_counting_audio(ui_server):
    playwright = pytest.importorskip("playwright.sync_api")
    mb = 1024 * 1024
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1600, "height": 1100}, reduced_motion="reduce")
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(ui_server) else route.abort())
            page = context.new_page()
            page.route("**/v1/jobs?*", lambda route: route.fulfill(json=[]))
            page.route("**/v1/probe", lambda route: route.fulfill(json={
                "content_type": "video", "title": "Sized video", "duration_seconds": 60,
                "available_formats": [
                    {"format_id": "v1080", "container": "mp4", "mime_type": "video/mp4", "has_video": True, "has_audio": False, "height": 1080, "video_codec": "avc1", "requires_muxing": True, "size_bytes": 20 * mb},
                    {"format_id": "v720", "container": "mp4", "mime_type": "video/mp4", "has_video": True, "has_audio": False, "height": 720, "video_codec": "avc1", "requires_muxing": True, "size_bytes": 8 * mb},
                    {"format_id": "audio", "container": "m4a", "mime_type": "audio/mp4", "has_video": False, "has_audio": True, "audio_bitrate_kbps": 128, "size_bytes": mb},
                ],
            }))
            page.goto(ui_server, wait_until="networkidle")
            page.locator("#url-input").fill("https://example.com/video")
            page.locator("#download-btn").click()
            page.get_by_role("heading", name="Sized video", exact=True).wait_for()
            assert page.locator("[data-source-size]").inner_text() == "~21.0 MB"
            page.get_by_role("button", name="Video", exact=True).press("Enter")
            page.get_by_role("button", name="Video + Audio", exact=True).press("Enter")
            quality = page.locator(".node-choice").filter(has=page.get_by_role("button", name="720p · HD", exact=True))
            assert quality.locator("[data-choice-size]").inner_text() == "~9.0 MB"
            page.get_by_role("button", name="720p · HD", exact=True).press("Enter")
            container = page.locator(".node-choice").filter(has=page.get_by_role("button", name="MP4 · H.264", exact=True))
            assert container.locator("[data-choice-size]").inner_text() == "~9.0 MB"
            page.get_by_role("button", name="Audio", exact=True).press("Enter")
            page.get_by_role("button", name="MP3", exact=True).press("Enter")
            bitrate = page.locator(".node-choice").filter(has=page.get_by_role("button", name="192k · Balanced", exact=True))
            assert bitrate.locator("[data-choice-size]").inner_text() == "~1.4 MB"
        finally:
            browser.close()


def test_playlist_sizes_load_automatically_and_update_every_quality_inline(ui_server):
    playwright = pytest.importorskip("playwright.sync_api")
    mb = 1024 * 1024
    inspections = []
    held = []
    hold = True

    def fulfill_sizes(route):
        urls = route.request.post_data_json["urls"]
        inspections.extend(urls)
        assert len(urls) <= 5
        route.fulfill(content_type="application/x-ndjson", body="".join(json.dumps({
            "url": url, "probe": {"content_type": "video", "duration_seconds": 120, "available_formats": [
                {"format_id": "v1080", "height": 1080, "has_video": True, "has_audio": True, "size_bytes": 20 * mb},
                {"format_id": "v720", "height": 720, "has_video": True, "has_audio": True, "size_bytes": 5 * mb},
                {"format_id": "v480", "height": 480, "has_video": True, "has_audio": True, "size_bytes": mb},
            ]},
        }) + "\n" for url in urls))

    def sizes(route):
        if hold:
            held.append(route)
        else:
            fulfill_sizes(route)

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1600, "height": 1100}, reduced_motion="reduce")
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(ui_server) else route.abort())
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/v1/jobs?*", lambda route: route.fulfill(json=[]))
            page.route("**/v1/probe", lambda route: route.fulfill(json={
                "content_type": "playlist", "title": "Electrical Machines", "metadata": {"item_count_total": 166},
                "items": [{"index": i, "url": f"https://example.com/{i}", "title": f"Lecture {i}", "content_type": "video", **({"size_bytes": 516 * mb} if i == 1 else {})} for i in range(1, 51)],
            }))
            page.route("**/v1/probe/sizes", sizes)
            page.goto(ui_server, wait_until="networkidle")
            page.locator("#url-input").fill("https://example.com/playlist")
            page.locator("#download-btn").click()
            page.get_by_role("button", name="Playlist Video", exact=True).press("Enter")
            page.get_by_role("button", name="First 50 Items", exact=True).press("Enter")
            quality = page.locator(".node-choice").filter(has=page.get_by_role("button", name="720p · HD", exact=True))
            assert quality.locator("[data-choice-size]").inner_text() == "Calculating…"
            page.wait_for_timeout(400)
            assert len(held) == 2
            assert "unknown" not in quality.inner_text()
            hold = False
            for route in held:
                fulfill_sizes(route)
            page.wait_for_function("[...document.querySelectorAll('[data-choice-size]')].some(el => el.textContent === '~250.0 MB')")
            assert quality.locator("[data-choice-size]").inner_text() == "~250.0 MB"
            subtitle = quality.locator(".choice-sub")
            assert "Items 1-50" in subtitle.inner_text()
            assert "Balanced" not in subtitle.inner_text()
            assert not page.locator(".choice-size").count()
            assert subtitle.evaluate("el => { const a=el.firstElementChild.getBoundingClientRect(), b=el.querySelector('[data-choice-size]').getBoundingClientRect(); return Math.abs(a.top-b.top)<2; }")
            assert len(inspections) == len(set(inspections)) == 50
            full_hd = page.locator(".node-choice").filter(has=page.get_by_role("button", name="1080p · Full HD", exact=True))
            assert full_hd.locator("[data-choice-size]").inner_text() == "~1000.0 MB"
            full_hd.get_by_role("button", name="1080p · Full HD", exact=True).press("Enter")
            assert len(inspections) == 50
            if screenshot := os.environ.get("NEON_SYNC_SIZE_SCREENSHOT"):
                page.locator("#workspace-menu > summary").click()
                page.locator("#theme-btn").click()
                page.keyboard.press("Escape")
                full_hd.screenshot(path=screenshot)
            assert not errors
        finally:
            browser.close()


def test_playlist_missing_metadata_has_no_partial_subtotal_or_unknown_counter(ui_server):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
            page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(ui_server) else route.abort())
            page.route("**/v1/jobs?*", lambda route: route.fulfill(json=[]))
            page.route("**/v1/probe", lambda route: route.fulfill(json={
                "content_type": "playlist", "metadata": {"item_count_total": 2}, "items": [
                    {"index": 1, "url": "https://example.com/1", "content_type": "video", "size_bytes": 1024},
                    {"index": 68, "url": "https://example.com/68", "content_type": "video"},
                ],
            }))
            page.goto(ui_server, wait_until="networkidle")
            page.locator("#url-input").fill("https://example.com/playlist")
            page.locator("#download-btn").click()
            page.get_by_role("button", name="Playlist Video", exact=True).press("Enter")
            page.get_by_role("button", name="Custom Selection", exact=True).press("Enter")
            page.locator("[data-pl-range]").fill("1,68")
            page.wait_for_function("document.querySelector('[data-pl-total-size]').textContent === 'Selected size: Size unavailable'")
            assert "unknown" not in page.locator("[data-pl-total-size]").inner_text()
            assert page.locator("[data-pl-range]").input_value() == "1,68"
        finally:
            browser.close()


def test_download_cards_jobs_and_transcripts_show_actual_bytes(ui_server):
    playwright = pytest.importorskip("playwright.sync_api")
    mb = 1024 * 1024
    jobs = [
        {"job_id": "active", "url": "https://example.com/active.pdf", "status": "downloading", "request": {}, "progress": {"bytes_downloaded": mb, "bytes_total": 4 * mb, "status": "downloading"}},
        {"job_id": "done", "url": "https://example.com/done.pdf", "status": "completed", "request": {}, "result": {"title": "Done", "download": {"filename": "done.pdf", "artifact_url": "/done.pdf", "mime_type": "application/pdf", "size_bytes": 3 * mb}, "transcript": {"artifact_url": "/transcript.txt", "size_bytes": 1024}}},
    ]
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1100}, reduced_motion="reduce")
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(ui_server) else route.abort())
            page = context.new_page()
            page.route("**/v1/jobs?*", lambda route: route.fulfill(json=jobs))
            page.route("**/v1/jobs/active", lambda route: route.fulfill(json=jobs[0]))
            page.goto(ui_server, wait_until="networkidle")
            page.get_by_text("1.0 MB / 4.0 MB", exact=True).wait_for()
            page.locator("#recent-jobs-btn").click()
            page.locator("#recent-jobs-list .job-size").filter(has_text="3.0 MB").wait_for()
            assert page.locator("#recent-jobs-list .job-size").filter(has_text="1.0 MB / 4.0 MB").count() == 1
            page.locator("#recent-jobs-list .service-capability").filter(has_text="done.pdf").get_by_role("button", name="View on canvas", exact=True).click()
            assert page.locator("[data-node-id='job_done'] [data-job-size]").inner_text() == "3.0 MB"
            assert page.get_by_role("link", name="Transcript .txt · 1.0 KB", exact=True).count() == 1
        finally:
            browser.close()


def test_playlist_size_rate_limit_retries_and_clear_aborts_pending_checks(ui_server):
    playwright = pytest.importorskip("playwright.sync_api")
    requests = []

    def sizes(route):
        requests.append(route)
        if len(requests) == 1:
            route.fulfill(status=429, headers={"Retry-After": "1"}, json={"detail": {"message": "Retry shortly"}})
        # Hold the retry response until after the canvas has been cleared.

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
            page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(ui_server) else route.abort())
            page.route("**/v1/jobs?*", lambda route: route.fulfill(json=[]))
            page.route("**/v1/probe", lambda route: route.fulfill(json={
                "content_type": "playlist", "metadata": {"item_count_total": 1},
                "items": [{"index": 1, "url": "https://example.com/1", "content_type": "video"}],
            }))
            page.route("**/v1/probe/sizes", sizes)
            page.goto(ui_server, wait_until="networkidle")
            page.locator("#url-input").fill("https://example.com/playlist")
            page.locator("#download-btn").click()
            page.get_by_role("button", name="Playlist Video", exact=True).press("Enter")
            page.get_by_role("button", name="Custom Selection", exact=True).press("Enter")
            page.wait_for_timeout(600)
            assert len(requests) == 1
            assert "Calculating…" in page.locator("[data-pl-total-size]").inner_text()
            page.wait_for_timeout(1200)
            assert len(requests) == 2
            page.locator("#clear-canvas-btn").click()
            assert page.locator(".node").count() == 0
            requests[1].fulfill(content_type="application/x-ndjson", body=json.dumps({
                "url": "https://example.com/1", "probe": {"content_type": "video", "size_bytes": 1024},
            }) + "\n")
            page.wait_for_timeout(400)
            assert page.locator(".node").count() == 0
            assert page.locator("#activity-status-text").inner_text() == "Idle"
            assert len(requests) == 2
        finally:
            browser.close()


def test_url_prefix_auto_capture_and_quick_capture_dialog(ui_server):
    playwright = pytest.importorskip("playwright.sync_api")
    probed_urls = []

    def handle_probe(route):
        payload = route.request.post_data_json
        probed_urls.append(payload.get("url"))
        route.fulfill(json={
            "content_type": "video",
            "title": "Captured Prefix Video",
            "source": {"domain": "example.com"},
            "available_formats": [{"format_id": "22", "ext": "mp4", "height": 720, "filesize": 12_000_000}],
        })

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(ui_server) else route.abort())
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/v1/jobs?*", lambda route: route.fulfill(json=[]))
            page.route("**/v1/probe", handle_probe)

            # Direct address-bar prefix capture redirects to /?url=..., probes immediately, and cleans URL to /
            page.goto(f"{ui_server}/https://example.com/watch?v=demo42", wait_until="networkidle")
            page.get_by_text("Captured Prefix Video", exact=False).wait_for()
            assert probed_urls == ["https://example.com/watch?v=demo42"]
            assert page.url == f"{ui_server}/"

            # Open Quick Capture dialog from Options menu and verify live origin prefix & bookmarklet
            page.locator("#workspace-menu > summary").click()
            page.locator("#quick-capture-btn").click()
            dialog = page.locator("#quick-capture-dialog")
            dialog.wait_for(state="visible")
            assert f"{ui_server}/https://youtu.be/..." in page.locator("#capture-prefix-example").inner_text()
            bm_href = page.locator("#bookmarklet-link").get_attribute("href")
            assert bm_href and bm_href.startswith("javascript:") and f"{ui_server}/?url=" in bm_href
            page.locator("#close-quick-capture").click()
            assert not errors
        finally:
            browser.close()
