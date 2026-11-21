# Universal Media Extraction Engine — Research & Architecture Hub

> **Research Date:** November 2026
> **Status:** Research Complete (Pre-Implementation Planning)
> **Objective:** Define the architecture, engine composition, security boundaries, licensing compliance, and roadmap for a reusable, production-grade **Universal Media Extraction Engine** capable of powering web apps, mobile apps, desktop clients, and public/private APIs.

---

## Navigation

| Document | Deliverables Covered | Summary |
|---|---|---|
| **[01 — Executive Summary & Final Recommendation](research/01-executive-summary-and-recommendation.md)** | `#1 Executive Summary`, `#2 Ranked Engines`, `#3 Repo Links`, `#8 Recommended Stack`, `#18 MVP`, `#13 Final Recommendation` | High-level verdict, ranked engine shortlist, recommended production stack, MVP scope, and concrete final architecture recommendation. |
| **[02 — Engine Deep Dives & Comparison](research/02-engine-deep-dives.md)** | `#4 Comparison Table`, `#5 Architecture of Promising Projects`, `#19 Ideas to Borrow`, `#20 Projects Not to Use` | Source-code verified analysis of all 14 core repositories plus 13 additional maintained projects (internals, APIs, Docker, auth, limits, verdicts). |
| **[03 — Content Coverage & Browser Fallback](research/03-content-coverage-and-browser-fallback.md)** | `#6 Content Coverage Matrix`, `#14 Browser Fallback Architecture` | Reliability matrix across 17 content types and full Playwright-based browser sniffing, DOM/OG/JSON-LD extraction, and EME DRM detection design. |
| **[04 — System Architecture, Registry & Pipeline](research/04-system-architecture-and-pipeline.md)** | `#9 System Architecture`, `#10 Engine Registry`, `#11 URL Routing Algorithm`, `#12 Normalized Output Schema`, `#13 Queue & Worker Architecture` | Modular adapter-based system design, capability-driven engine registry, low-waste URL probing & routing algorithm, normalized JSON schema, and download/FFmpeg storage pipeline. |
| **[05 — Security & Abuse Prevention](research/05-security-and-abuse-prevention.md)** | `#15 Security Architecture` | Defense-in-depth blueprint against SSRF, DNS rebinding, malicious HLS/media files, browser escape, disk/CPU exhaustion, and unsafe file delivery. |
| **[06 — Licensing & Compliance Matrix](research/06-licensing-and-compliance-matrix.md)** | `#7 Licensing Matrix` | Source-verified license audit including transitive dependencies (`mutagen`, FFmpeg codec builds, PyInstaller bundles, AGPL traps) and proprietary SaaS compliance rules. |
| **[07 — Strategy Comparison, Deployment & Roadmap](research/07-strategy-comparison-and-roadmap.md)** | `#11 Strategies A–E`, `#16 Deployment Strategy`, `#17 Development Roadmap`, `#18 MVP Plan` | Side-by-side evaluation of Options A–E, Docker/Kubernetes multi-tier deployment topology, and phased 4-stage delivery plan. |

### Machine-Readable Schemas & Contracts

| Schema File | Purpose |
|---|---|
| **[normalized-media-output.schema.json](research/schemas/normalized-media-output.schema.json)** | JSON Schema (Draft 2020-12) defining the engine-agnostic normalized media extraction & download response. |
| **[engine-manifest.schema.json](research/schemas/engine-manifest.schema.json)** | JSON Schema defining the capability manifest every engine adapter registers at startup. |
| **[example-normalized-output.json](research/schemas/example-normalized-output.json)** | Complete realistic payload demonstrating multi-format video, subtitles, metadata, warnings, and engine provenance. |

---

## TL;DR Verdict

* **Strategy Selected:** **Option E + Option C** — Build an **independent, proprietary Universal Engine Registry & Orchestrator** integrating best-in-class open-source tools strictly through **isolated subprocess/worker adapters**, with a **sandboxed Playwright browser fallback**.
* **Core Engine Suite:**
  1. **`yt-dlp`** (+ `yt-dlp-ejs` & `Deno` + `curl_cffi`) — Primary video, audio, playlists, channels, subtitles, and generic media (~941 extractor modules / ~1,800+ sites).
  2. **`gallery-dl`** — Primary image galleries, albums, multi-photo social posts, and artwork platforms (~258 extractor modules / 300+ sites).
  3. **`Streamlink`** — Live broadcast extraction & recording (136 live-platform plugins; BSD-2-Clause).
  4. **`N_m3u8DL-RE`** *(optional high-speed stream tier)* — High-throughput HLS/DASH/ISM segmented stream downloader (MIT, single binary).
  5. **`aria2`** *(+ native async HTTP client)* — Resumable, multi-connection direct file & document downloads.
  6. **`FFmpeg` / `ffprobe`** — Universal remuxing, transcoding, audio extraction, clipping, thumbnails, and file validation.
  7. **`Playwright`** *(Chromium + optional `trafilatura`)* — Sandboxed Tier-3 fallback for JS-rendered pages, embedded players, manifest sniffing (`.m3u8`/`.mpd`), Open Graph/JSON-LD metadata, and EME DRM detection.
* **Critical License Findings Verified from Source:**
  * **`imputnet/cobalt`** and **`alexta69/metube`** are **AGPL-3.0** — **do not fork or embed** in a proprietary SaaS product.
  * **`mikf/gallery-dl`** is **GPL-2.0** (not public domain) and **`yt-dlp[default]`** pulls in **`mutagen` (GPL-2.0)** — **invoke all GPL/LGPL engines via strict subprocess/container boundaries**, never via in-process Python `import` or C linking inside proprietary application code.
* **Critical Security Finding:** Python socket monkey-patching is bypassed by native TLS clients (`curl_cffi`/libcurl) and FFmpeg. **All worker egress must be forced through a dedicated Safe Egress Proxy** that resolves DNS, verifies global unicast IPs (blocking RFC1918, loopback, link-local/IMDS, CGNAT, and IPv4-mapped IPv6), and dials the verified IP directly—backed by container network egress firewalls.
# Neon-sync
