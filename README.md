# Neon Sync — Universal Media Extraction Engine

> **Architecture:** Modular Universal Engine Registry & Adapter Orchestrator (Option E + C)
> **Status:** Phase 1 & Phase 2 Implemented & Tested (8 Registered Engines, Safe Egress Proxy, Tier-3 Playwright Fallback, SQLite Persistence, Optional S3/R2 Storage, Web UI & Multi-Plane Docker)

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
| `GET /v1/health` | Service status + installed/missing engine overview |
| `GET /v1/formats` | Declarative catalog of supported qualities, codecs, containers, bitrates, image formats & presets |
| `GET /v1/engines` | List all registered engine manifests, capabilities, versions, and circuit/health telemetry |
| `POST /v1/engines/refresh` | Re-run live health & version checks across all registered adapters |
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
