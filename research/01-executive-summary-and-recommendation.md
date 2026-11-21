# 01 — Executive Summary & Final Recommendation

> **Verification Basis:** Live repository inspection via GitHub API, source code trees, `LICENSE`/`COPYING` files, `pyproject.toml`/`package.json` manifests, and release histories as of **November 2026**.

---

## 1. Executive Summary

Building a production-grade **Universal Media Extraction Engine** that reliably handles arbitrary user-submitted URLs—spanning video platforms, audio, playlists, image galleries, live streams, HLS/DASH manifests, direct files, PDFs, and JavaScript-rendered webpages—cannot be solved cleanly by a single monolithic tool or by writing site scrapers from scratch. Platform anti-bot defenses (YouTube's PO tokens and JS `n`-signature challenges, TLS JA3/JA4 fingerprinting, rotating API tokens) require continuous, community-scale maintenance that only flagship open-source extractors can sustain.

Our source-level investigation of **14 target repositories** and **13 additional maintained open-source projects** yields four decisive conclusions:

1. **No single extractor covers the whole web.** `yt-dlp` dominates video/audio (~941 extractor files, ~1,800+ sites) but deliberately avoids image galleries and multi-photo posts; `gallery-dl` fills the image/gallery gap (258 extractor files, 300+ sites); `Streamlink` excels at live broadcasts (136 plugins); `aria2` / async HTTP handles direct binaries and documents; `FFmpeg` is irreplaceable for validation, remuxing, clipping, and conversion; and `Playwright` is required as an isolated fallback for JS-rendered pages and embedded players.
2. **Forking existing web downloaders is a dead end.** The two most mature web UIs (`imputnet/cobalt` and `alexta69/metube`) are **AGPL-3.0**, creating severe copyleft obligations for a proprietary commercial SaaS product. Meanwhile, newer MIT-licensed multi-engine tools (`S-k404/grab`, `star1133/media-downloader`, `TheRealGudPerson/OmniFetch`) are early-stage single-node prototypes (0–1 stars, created Oct–Nov 2026) that offer **excellent architectural ideas to borrow** (CDP EME DRM detection, Safe Egress Proxy, parallel probe confidence scoring) but are unsuitable as a production base.
3. **Subprocess isolation solves both Licensing and Security.** Multiple critical engines are copyleft (`gallery-dl` is **GPL-2.0**, `aria2` is **GPL-2.0**, standard `FFmpeg` builds are **GPL-2.0+/3.0**, and even Unlicense `yt-dlp[default]` bundles `mutagen` under **GPL-2.0**). Wrapping every engine behind a **strict subprocess / worker-container adapter boundary** simultaneously protects proprietary orchestrator code from GPL derivative-work contagion and contains memory/CPU/crash/exploit blast radii.
4. **Security must be network-enforced, not just URL-validated.** Arbitrary URL submission turns media extractors into high-impact SSRF and Local File Read (LFR) primitives (e.g., crafted `.m3u8` playlists, HTTP 302 redirects to `169.254.169.254`, DNS rebinding, and `curl_cffi` bypassing Python socket hooks). Production deployment requires **zero-direct-egress worker sandboxes** routed exclusively through an **IP-validating Safe Egress Proxy** plus OS-level firewall rules.

---

## 2. Ranked List of Engines & Tools

Ranked by production value, maintenance velocity, coverage, programmatic ergonomics, and architectural fit:

| Rank | Project | Role in Engine | License | Latest Activity (Verified Nov 2026) | Verdict |
|---:|---|---|---|---|---|
| **1** | **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** (+ [yt-dlp/ejs](https://github.com/yt-dlp/ejs) & Deno) | Primary Video / Audio / Playlist / Subtitle / Generic Extractor | Unlicense *(note: `mutagen` dep is GPL-2.0; binaries GPLv3+)* | Release `2026.10.26`, commit `2026-11-21` (209.6k ★) | **Integrate (Core)** — Subprocess adapter |
| **2** | **[FFmpeg](https://github.com/FFmpeg/FFmpeg)** | Media Validation (`ffprobe`), Muxing, Transcoding, Audio Extraction, Clipping, Thumbnails | LGPL-2.1+ default / GPL-2.0+ with `--enable-gpl` | Tag `n9.1`, commit `2026-11-21` (67.8k ★) | **Integrate (Core)** — Subprocess in sandboxed media worker |
| **3** | **[gallery-dl](https://github.com/mikf/gallery-dl)** | Primary Image, Gallery, Album & Multi-Photo Social Extractor | **GPL-2.0** | Release `v1.32.21`, commit `2026-11-21` (20.6k ★) | **Integrate (Core)** — Subprocess adapter only (no Python `import`) |
| **4** | **[Playwright](https://github.com/microsoft/playwright)** | Tier-3 Browser Fallback: JS Rendering, Network Sniffing, Embedded Players, OG/JSON-LD, DRM Detection | Apache-2.0 | Release `v1.65.1`, commit `2026-11-21` (100.7k ★) | **Integrate (Fallback Tier)** — Isolated ephemeral browser workers |
| **5** | **[Streamlink](https://github.com/streamlink/streamlink)** | Live Stream Extraction & Continuous Recording | BSD-2-Clause | Release `8.6.3`, commit `2026-11-18` (12.0k ★) | **Integrate (Phase 2)** — Live-stream specialist adapter |
| **6** | **[aria2](https://github.com/aria2/aria2)** | High-Speed Multi-Connection & Resumable Direct File Downloader | **GPL-2.0** | Release `1.37.0` (2023-11), commit `2026-06-25` (43.6k ★) | **Integrate (Direct Large Files)** — CLI subprocess per job (avoid shared open RPC) |
| **7** | **[N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE)** *(Bonus Discovery)* | High-Performance Multi-Threaded HLS / DASH / ISM Stream Downloader | MIT | Release `v0.6.0-beta`, commit `2026-11-04` (9.1k ★) | **Integrate (Optional Phase 2)** — Faster HLS/DASH segment engine than yt-dlp/FFmpeg |
| **8** | **[Trafilatura](https://github.com/adbar/trafilatura)** *(Bonus Discovery)* | Public Webpage Text, Article, Canonical Metadata & Feed Extraction | Apache-2.0 | Release `v2.3.1`, commit `2026-11-20` (7.2k ★) | **Integrate (Webpage Metadata)** — Lightweight HTML/OG/JSON-LD & article extractor |
| **9** | **[bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)** *(Bonus Discovery)* | YouTube Proof-of-Origin (PO) Token Sidecar for `yt-dlp` | **GPL-3.0** | Release `2.0.2`, commit `2026-10-07` (911 ★) | **Integrate (Optional Sidecar)** — Separate HTTP container if YouTube bot-gating hits server IPs |
| **10** | **[Puppeteer](https://github.com/puppeteer/puppeteer)** | Alternative Headless Browser Engine | Apache-2.0 | Release `v25.18.1`, commit `2026-11-21` (95.9k ★) | **Do Not Use (Superseded by Playwright)** — Less mature multi-context isolation & request routing |
| **11** | **[Wget2](https://github.com/rockdaboot/wget2)** / **[GNU Wget](https://github.com/mirror/wget)** | Direct HTTP/FTP Download & Mirroring | GPL-3.0 (`wget`) / LGPL-3.0 lib + GPL-3.0 CLI (`wget2`) | `wget2` `v2.3.0` (`2026-11-05`); `wget` `v1.24.5` (`2024-06`) | **Avoid for Core** — Replaced by native async HTTP client (`httpx`/`undici`) + `aria2` |
| **12** | **[Lux](https://github.com/iawia002/lux)** *(Bonus Discovery)* | Go Video Downloader (strong on Chinese platforms: Bilibili, Youku, iQIYI) | MIT | Release `v0.24.1`, commit `2025-12-29` (31.9k ★) | **Optional Niche Adapter** — Only if targeting CN platforms where yt-dlp lags |
| **13** | **[Cobalt](https://github.com/imputnet/cobalt)** | Fast API-first Social Media Downloader & Tunnel Proxy | **AGPL-3.0** | `v11.7.1`, commit `2026-04-06` (46.4k ★) | **Avoid Embedding / Study Architecture Only** — AGPL-3.0 license risk + only 21 services |
| **14** | **[MeTube](https://github.com/alexta69/metube)** | Self-Hosted Web UI for `yt-dlp` | **AGPL-3.0** | Release `2026.11.14` (15.3k ★) | **Avoid Forking / Borrow Ideas** — AGPL-3.0; single-engine (`yt-dlp` only) |
| **15** | **[MaxDL](https://github.com/star1133/media-downloader)**, **[Grab](https://github.com/S-k404/grab)**, **[OmniFetch](https://github.com/TheRealGudPerson/OmniFetch)** | Multi-Engine Orchestration Reference Projects | MIT | Created Oct–Nov 2026 (0–1 ★) | **Borrow Specific Patterns Only** — Too early/single-node for production fork |
| **16** | **[youtube-dl](https://github.com/ytdl-org/youtube-dl)** | Legacy Video Extractor | Unlicense | Last release `2021.12.17`, ~10 commits/year (141.6k ★) | **Avoid Completely** — Superseded by `yt-dlp` |

---

## 3. Verified Repository Directory

### Core Evaluated Repositories (Prompt List)
1. **yt-dlp:** https://github.com/yt-dlp/yt-dlp
2. **youtube-dl:** https://github.com/ytdl-org/youtube-dl
3. **gallery-dl:** https://github.com/mikf/gallery-dl
4. **aria2:** https://github.com/aria2/aria2
5. **GNU Wget (mirror):** https://github.com/mirror/wget
6. **Streamlink:** https://github.com/streamlink/streamlink
7. **FFmpeg:** https://github.com/FFmpeg/FFmpeg
8. **Cobalt:** https://github.com/imputnet/cobalt
9. **Grab:** https://github.com/S-k404/grab
10. **MeTube:** https://github.com/alexta69/metube
11. **MaxDL (`star1133/media-downloader`):** https://github.com/star1133/media-downloader
12. **OmniFetch:** https://github.com/TheRealGudPerson/OmniFetch
13. **Playwright:** https://github.com/microsoft/playwright
14. **Puppeteer:** https://github.com/puppeteer/puppeteer

### Additional High-Value Maintained Repositories Discovered
15. **N_m3u8DL-RE** (HLS/DASH/ISM fast segmented stream downloader): https://github.com/nilaoda/N_m3u8DL-RE
16. **yt-dlp/ejs** (External JS challenge solver scripts for yt-dlp): https://github.com/yt-dlp/ejs
17. **bgutil-ytdlp-pot-provider** (YouTube Proof-of-Origin token sidecar): https://github.com/Brainicism/bgutil-ytdlp-pot-provider
18. **yt-dlp/FFmpeg-Builds** (Patched static FFmpeg GPL/LGPL builds): https://github.com/yt-dlp/FFmpeg-Builds
19. **Trafilatura** (Webpage text & metadata extraction): https://github.com/adbar/trafilatura
20. **Mozilla Readability** (Article DOM parsing): https://github.com/mozilla/readability
21. **ArchiveBox** (Multi-extractor URL archiving architecture reference): https://github.com/ArchiveBox/ArchiveBox
22. **Media Downloader (`mhogomchungu/media-downloader`)** (Mature C++/Qt multi-engine adapter reference): https://github.com/mhogomchungu/media-downloader
23. **Lux** (Go multi-site video downloader): https://github.com/iawia002/lux
24. **YouTube.js** (TypeScript InnerTube API client used by Cobalt): https://github.com/LuanRT/YouTube.js
25. **Wget2** (Modern HTTP/2 successor to Wget): https://github.com/rockdaboot/wget2
26. **ytarchive** (Dedicated YouTube live-stream archiver): https://github.com/Kethsar/ytarchive
27. **spotDL** (Spotify metadata → YouTube audio matcher): https://github.com/spotDL/spotify-downloader

---

## 4. Recommended Production Stack

| Layer | Recommended Technology | Rationale |
|---|---|---|
| **Control Plane / Public API** | **FastAPI (Python 3.12+)** or **Go / TypeScript (Node 22+)** *(FastAPI recommended for unified tooling with Python workers; Go/TS equally viable since adapters are process-isolated)* | Clean OpenAPI/REST + WebSocket/SSE progress streaming, independent of frontend (web, mobile, desktop, CLI). |
| **Job Queue & State** | **Redis + BullMQ (TS) or Dramatiq/ARQ/Celery (Python)** + **PostgreSQL** | Durable job metadata, priority queues (fast probe vs heavy download vs browser fallback), dead-letter queues, cancellation tokens, idempotency keys. |
| **Safe Egress Gateway** | **Custom Go or Rust HTTP/CONNECT Egress Proxy** (inspired by MaxDL `safe_proxy.py`) + **Docker/K8s NetworkPolicy** | Forces all DNS resolution and IP validation into one choke point; neutralizes SSRF, DNS rebinding, and `curl_cffi`/FFmpeg resolver bypasses. |
| **Tier-1 Fast Direct & HTML Probe** | **Native Async HTTP (`httpx` / `undici`)** + **`Trafilatura` / `lxml`** | Sub-200ms `HEAD`/range check for direct files, PDFs, images, `.m3u8`/`.mpd`, and static Open Graph / JSON-LD metadata before spawning heavy tools. |
| **Tier-2 Specialized Extractors** | **`yt-dlp`** (+ `Deno >=2.9` + `yt-dlp-ejs` + `curl_cffi`), **`gallery-dl`**, **`Streamlink`**, **`N_m3u8DL-RE`** (optional) | Industry-standard coverage via uniform subprocess adapters (`--dump-single-json`, `--dump-json`, `--json`). |
| **Tier-3 Browser Fallback** | **Playwright (Chromium)** in dedicated isolated worker pool | Only invoked on explicit fallback when Tier-1/2 fail or detect JS-only shells; handles network manifest sniffing, DOM `<video>`/`<audio>`, OG/JSON-LD, and EME DRM detection. |
| **Downloader & Post-Processor** | **`aria2c`** (direct large files), **`yt-dlp` native/fragment downloader**, **`FFmpeg` / `ffprobe`** (sandboxed transcode worker) | Parallel chunking, resume support, magic-byte + `ffprobe` validation, lossless remuxing, audio extraction, clipping, thumbnail generation. |
| **Storage & Delivery** | **Ephemeral NVMe Scratch** (per-job tmpfs/quota dir) → **S3 / Cloudflare R2 / MinIO** with **lifecycle TTL** + **Pre-signed URLs on isolated download domain** | Zero stateful disk bloat on workers, resumable client downloads via HTTP Range, isolated origin prevents XSS/cookie theft from untrusted files. |

---

## 5. MVP Recommendation (Phase 1 — 4 to 6 Weeks)

To ship value rapidly without compromising security or future extensibility, build the **MVP around the Universal Engine Registry from Day 1** with **4 engines + 1 egress proxy**:

1. **Core Orchestrator & Engine Registry:** Abstract `ExtractorAdapter` interface + normalized JSON output contract (`normalized-media-output.schema.json`).
2. **Engines Included in MVP:**
   * `direct-http` (async `HEAD`/stream + `aria2c` for large files — handles PDFs, images, archives, raw `.mp4`/`.mp3`, HTML OG/JSON-LD metadata).
   * `yt-dlp` adapter (+ `Deno` + `yt-dlp-ejs` + `curl_cffi` — handles video, audio, playlists, subtitles, HLS/DASH).
   * `gallery-dl` adapter (subprocess — handles multi-image posts, albums, galleries).
   * `ffmpeg` post-processing adapter (`ffprobe` validation, remuxing, audio extraction, thumbnail generation).
3. **Mandatory Day-1 Security:** Safe Egress Proxy, URL scheme/IP validator, non-root containerized workers with `no-new-privileges`, per-job disk/time/RAM limits, rate limiting, and signed S3/R2 downloads with `Content-Disposition: attachment`.
4. **Defer to Phase 2:** `Streamlink` (live recording), `Playwright` browser fallback pool, `N_m3u8DL-RE` accelerator, `bgutil` PO-token sidecar, clipping/transcode presets, and user cookie vault.

---

## 6. Final Architectural Recommendation

> **Adopt Option E (Independent Universal Engine Registry & Adapter Orchestrator) combined with Option C (Multi-Engine + Sandboxed Browser Fallback).**

* **Reuse, don't rewrite extraction:** Delegate site-specific reverse engineering entirely to `yt-dlp`, `gallery-dl`, and `Streamlink`, updated via automated CI smoke-test pipelines.
* **Decouple everything via adapters:** Every engine implements a strict `probe()`, `extract()`, `download()`, `cancel()`, and `health()` contract returning the normalized schema. Any engine (e.g., swapping `aria2` or adding `N_m3u8DL-RE`) can be replaced with zero changes to API consumers or frontends.
* **Isolate processes for Security AND Licensing:** Run every extractor and FFmpeg inside unprivileged, ephemeral, egress-proxied subprocesses/containers. Never import GPL Python packages (`gallery-dl`, `mutagen`) into proprietary orchestrator code, and never fork AGPL-3.0 projects (`cobalt`, `metube`).
* **Design API-first:** Expose clean versioned REST + SSE/WebSocket APIs (`/v1/probe`, `/v1/jobs`, `/v1/jobs/{id}/events`, `/v1/engines`) so future web, iOS/Android, desktop, and third-party API clients share one battle-tested backend.
