# Neon Sync — Universal Media Extraction Engine

> **Architecture:** Modular Universal Engine Registry & Adapter Orchestrator (Option E + C)
> **Status:** Phase 1 & Phase 2 Implemented & Tested (8 Registered Engines, Safe Egress Proxy, Tier-3 Playwright Fallback, SQLite Persistence, Optional S3/R2 Storage, Web UI & Multi-Plane Docker)

Paste a public link, inspect the available formats, and save a file. Neon Sync requires no account or paid plan. It routes across installed adapters automatically; coverage depends on the source and the features installed on your server. It cannot guarantee every service, authenticated source, region, or DRM-protected stream.

The UI's feature status shows actual runtime coverage and limits. **Jobs** lists recent work, restores active progress after a page reload, and lets you reopen results or retry failed downloads. If SSE disconnects, progress falls back to polling. Files expire after six hours by default; save a local copy before expiry.

Playlist previews load in pages, with **Load more** and **Load all** controls for the remaining entries. Selections and filters persist as items arrive. The 50-item default download limit applies to how many items are saved in one job, not which playlist positions can be browsed or selected: a range such as `151-166` is valid. The UI displays the selection limit before submission. API clients can request later metadata pages with `playlist_start` on `POST /v1/probe`; pagination details are returned in `metadata.extra.playlist_page`.

File sizes appear on sources, quality/format choices, playlist items and selected totals, download progress, and recent jobs. `~` marks an estimate from source sizes or duration and bitrate. Playlist selections automatically load item metadata in bounded batches without downloading media, and quality cards show the total beside the item range. While metadata loads, the UI shows **Calculating…**; incomplete totals show **Size unavailable** instead of a misleading subtotal. Failed item checks can be retried in the picker. Generated outputs may need encoding before their size is available. Completed artifacts and transcripts show their actual stored size.

Neon Sync is an API-first, security-hardened **Universal Media Extraction Engine** that accepts a URL, intelligently identifies its content type, selects the best extraction engine via a capability registry, executes downloads in isolated subprocesses routed through a **Safe Egress Proxy**, validates/post-processes media via zero-egress **FFmpeg**, and returns a canonical JSON contract + safe artifact downloads.

---

## Quick Start (Local Development)

```bash
# 1. Create virtualenv and install package + dev tools
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

# 2. Run the full test suite & linter
.venv/bin/ruff check src/ tests/
.venv/bin/pytest -v

# 3. Start the Safe Egress Proxy (terminal 1)
.venv/bin/neon-sync-proxy --host 127.0.0.1 --port 3128

# 4. Start the API Control Plane (terminal 2)
NEON_SYNC_EGRESS_PROXY_URL=http://127.0.0.1:3128 .venv/bin/neon-sync-api
```

For normal local use, install the extraction features and run both services together:

```bash
.venv/bin/pip install -e ".[engines,webpage]"
# Install ffmpeg (including ffprobe), aria2, and Deno using your system package manager.
# Optional dynamic pages, screenshots, and PDFs:
.venv/bin/pip install -e ".[browser]"
.venv/bin/python -m playwright install chromium
./scripts/run-local.sh
```

Open [Neon Sync](http://127.0.0.1:8000/). The launcher persists job history in `data/jobs.sqlite3` and stops both child services when it exits. An incomplete download after a server restart is marked interrupted and can be retried with the original settings. The queue allows 100 pending/running jobs by default. Batch quota/capacity rejections create no partial jobs; repeated single-job submissions can use `idempotency_key` to safely retrieve the original job.

Local artifacts use relative download URLs by default, so they work on the domain serving the app. Set `NEON_SYNC_ARTIFACT_BASE_URL` only when downloads need an explicit external base URL. S3/R2 credentials are optional; local storage works without any cloud account. When constructing `Settings()` programmatically, set `job_db_path` to persist history (the launcher and Docker configuration already do this).

This version shares job history across visitors to the same installation. Use it locally or within a trusted group; independent user workspaces and distributed job processing are not implemented. Run one API worker per job database.

Browser transport and size calculations can be checked with `node --test tests/browser_*.test.cjs` in addition to the Python suite.
With Playwright Chromium installed, run `NEON_SYNC_BROWSER_TESTS=1 .venv/bin/pytest -q tests/test_ui_browser.py` to verify playlist scrolling and layout in both themes.

### Multi-Plane Docker Deployment (Network-Segmented)

```bash
docker compose up --build
```

`docker-compose.yml` isolates extraction workers on an **`internal: true`** Docker network with **no direct internet gateway**; all outbound traffic is forced through `safe-egress-proxy`.

---

## REST & SSE API Endpoints (`/v1`)

| Method & Path | Description |
|---|---|
| `GET /` | **Neon Sync Studio Web UI** (presets, tabbed video/audio/image/subtitle/clip settings, format picker, batch queue, live SSE progress) |
| `GET /v1/health` | Core service status and capability counts |
| `GET /v1/capabilities` | Runtime feature availability, source limitations, and installation limits |
| `GET /v1/formats` | Declarative catalog of supported qualities, codecs, containers, bitrates, image formats & presets |
| `POST /v1/probe` | Non-downloading metadata & format probe (canonical cached, SSRF-checked, normalized output) |
| `POST /v1/jobs` | Enqueue an async extraction/download job with advanced video/audio/image/subtitle/clipping options |
| `POST /v1/jobs/batch` | Validate & enqueue up to 10 URLs at once sharing advanced extraction settings |
| `GET /v1/jobs` / `GET /v1/jobs/{job_id}` | List or inspect job status, progress, and normalized output |
| `GET /v1/jobs/{job_id}/events` | Real-time **Server-Sent Events (SSE)** progress stream |
| `POST /v1/jobs/{job_id}/cancel` | Preemptively cancel job (`SIGTERM` → `SIGKILL` entire subprocess group) |
| `GET /v1/artifacts/{job_id}/{filename}` | Safe artifact download (`Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, `CSP: default-src 'none'; sandbox`) |

---

## Source Layout

```text
src/neon_sync/
├── config.py                  # Pydantic Settings (quotas, timeouts, proxy, rate limits)
├── models/                    # NormalizedMediaOutput, EngineManifest, JobRecord (JSON-Schema verified)
├── security/
│   ├── url_validator.py       # Scheme/port checks, IPv4/IPv6/mapped/CGNAT/IMDS blocking, canonicalization
│   ├── egress_proxy.py        # Safe HTTP/CONNECT proxy (atomic DNS resolve -> global IP verify -> direct IP dial)
│   ├── subprocess_guard.py    # argv-only exec, env secret scrubbing, process-group SIGTERM/SIGKILL cancellation
│   └── file_guard.py          # Filename sanitization, traversal prevention, disk floor, magic-byte validation
├── registry/
│   └── registry.py            # Universal Engine Registry (composite scoring, health checks, domain circuit breakers)
├── adapters/
│   ├── base.py                # Abstract ExtractorAdapter & PostProcessorAdapter interfaces
│   ├── direct_http.py         # Tier-1 Fast HEAD probe, small direct files, static OG / JSON-LD / HTML5 media
│   ├── aria2.py               # Tier-1 Large direct files (ephemeral CLI, RPC/torrent/metalink disabled)
│   ├── ytdlp.py               # Tier-2 Video/audio/playlists/subtitles (isolated subprocess, outdated detection)
│   ├── gallery_dl.py          # Tier-2 Image galleries/albums (strict GPL-2.0 CLI isolation, --range cap, ZIP packager)
│   └── ffmpeg.py              # Zero-egress ffprobe/FFmpeg (-protocol_whitelist file,pipe before -i)
├── pipeline/
│   ├── router.py              # Composite-scored URL router & hard-stop/fallback escalation planner
│   ├── storage.py             # Content-addressed SHA-256 artifact store with TTL cleanup
│   ├── queue.py               # Async JobManager with bounded concurrency, SSE pub/sub, idempotency & rate limits
│   └── orchestrator.py        # End-to-end 11-stage extraction & post-processing orchestrator
└── api/
    ├── app.py                 # FastAPI app factory & lifespan
    └── routes.py              # /v1 REST & SSE routes
```

---

## Research & Architecture Documentation

All pre-implementation research, source-verified repository audits, licensing analysis, security threat models, and schemas live in [`research/`](research/):

| Document | Deliverables Covered | Summary |
|---|---|---|
| **[01 — Executive Summary & Final Recommendation](research/01-executive-summary-and-recommendation.md)** | `#1`, `#2`, `#3`, `#8`, `#13`, `#18` | Ranked engines, verified repo directory, recommended stack, MVP scope, and final architectural recommendation. |
| **[02 — Engine Deep Dives & Comparison](research/02-engine-deep-dives.md)** | `#4`, `#5`, `#19`, `#20` | Source-verified analysis of all 14 core repos + 13 additional projects, ideas to borrow, and projects to avoid. |
| **[03 — Content Coverage & Browser Fallback](research/03-content-coverage-and-browser-fallback.md)** | `#6`, `#14` | Reliability matrix across 17 content categories and Playwright browser fallback architecture. |
| **[04 — System Architecture, Registry & Pipeline](research/04-system-architecture-and-pipeline.md)** | `#9`, `#10`, `#11`, `#12`, `#13` | 4-plane system design, Universal Engine Registry, URL routing algorithm, normalized schema, and download/FFmpeg infrastructure. |
| **[05 — Security & Abuse Prevention](research/05-security-and-abuse-prevention.md)** | `#15` | Defense-in-depth blueprint against SSRF, DNS rebinding, malicious media files, browser escape, and abuse. |
| **[06 — Licensing & Compliance Matrix](research/06-licensing-and-compliance-matrix.md)** | `#7` | Source-verified license audit incl. transitive dependencies (`mutagen`, FFmpeg GPL/LGPL, AGPL traps) and compliance rules. |
| **[07 — Strategy Comparison, Deployment & Roadmap](research/07-strategy-comparison-and-roadmap.md)** | `#11`, `#16`, `#17`, `#18` | Evaluation of Strategies A–E, containerized deployment topology, and 4-phase roadmap. |

### Machine-Readable Contract Schemas
- **[normalized-media-output.schema.json](research/schemas/normalized-media-output.schema.json)**
- **[engine-manifest.schema.json](research/schemas/engine-manifest.schema.json)**
- **[example-normalized-output.json](research/schemas/example-normalized-output.json)**
