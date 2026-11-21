# 07 — Strategy Comparison, Deployment Strategy & Phased Roadmap

---

## 1. Implementation Strategy Comparison (Options A – E)

We evaluated all five candidate implementation strategies across nine engineering, business, and operational dimensions:

* **Option A:** Build around `yt-dlp` only.
* **Option B:** Combine `yt-dlp` + `gallery-dl` + `aria2` + `FFmpeg` (hard-coded pipeline).
* **Option C:** Combine multiple engines + a Playwright browser-based fallback.
* **Option D:** Fork an existing multi-engine project (`Cobalt`, `MeTube`, `Grab`, `MaxDL`, `OmniFetch`) and customize it.
* **Option E:** Build our own independent **Universal Engine Registry & Orchestrator** and integrate tools strictly through modular adapters *(recommended combined with Option C's engine suite)*.

| Criterion | A: `yt-dlp` Only | B: `yt-dlp` + `gallery-dl` + `aria2` + `FFmpeg` | C: Multi-Engine + Browser Fallback | D: Fork Existing Project | **E (+C): Independent Registry + Modular Adapters + Browser Fallback** |
|---|---|---|---|---|---|
| **Content Coverage** | Medium (video/audio/playlists; misses galleries, PDFs, JS SPAs, live stream reliability) | High (covers ~85% of URLs; still misses JS-rendered SPAs & some live platforms) | **Very High (~95%+)** across video, audio, galleries, direct files, live streams & JS pages | Low–Medium (`MeTube`=yt-dlp only, `Cobalt`=21 sites, `MaxDL`/`Grab`=incomplete) | **Very High (~95%+)** and extensible to new engines (`N_m3u8DL-RE`, `Trafilatura`, future tools) |
| **Reliability** | High for video; fails hard on images/direct/SPAs | High for direct/social; no fallback for unknown JS players | High (multi-tier graceful escalation) | Medium–Low (inherits prototype bugs or single-node limits) | **Highest** (circuit breakers, health checks, confidence routing, swappable adapters) |
| **Development Effort** | Very Low (1–2 weeks) | Low–Medium (3–4 weeks) | Medium–High (6–10 weeks) | Low initially, **High long-term** fighting upstream architecture | **Medium (4–6 weeks MVP, 12–16 weeks full)** — clean investment with zero rewrite debt |
| **Performance** | Fast for video | Fast (dedicated tools per job) | Fast on Tier 1–2; heavy ONLY when Tier 3 browser triggers | Varies (`Cobalt` fast on 21 sites; `MaxDL`/`Grab` single-node bottleneck) | **Optimal** (Tier-1 sub-200ms probes prevent wasteful Tier-2/3 spawns; independent horizontal scaling) |
| **Scalability** | Easy to scale | Good (if queue-backed) | Requires separate browser worker autoscaling pool | **Poor** (`Grab`/`MaxDL`/`MeTube` are single-node designs) | **Excellent** (queue-segmented pools: probe, download, browser, zero-egress FFmpeg) |
| **Maintenance** | Low | Medium (track 4 tools) | Medium–High (track tools + Chromium updates) | **High** (merge conflicts with upstream; or dead forks) | **Clean & Predictable** (engine upgrades isolated inside single adapter containers + CI smoke tests) |
| **Security** | Medium (needs SSRF proxy & FFmpeg sandbox) | Medium–High (multiple network tools need unified egress proxy) | High complexity (Chromium sandbox + egress proxy + FFmpeg zero-egress) | **Weak** (`Grab`/`OmniFetch` lack SSRF proxy; `MeTube` docs admit `curl_cffi` bypasses socket guard) | **Security-First by Design** (mandatory Safe Egress Proxy + zero-egress media tier from Day 1) |
| **License Risk** | Low (Unlicense, isolate `mutagen` GPL-2.0) | Medium (GPL-2.0 in `gallery-dl`, `aria2`, FFmpeg → subprocess required) | Medium (managed cleanly via subprocess boundary) | **CRITICAL** (`Cobalt` & `MeTube` are **AGPL-3.0**!) | **Minimal & Audited** (strict subprocess boundary + zero AGPL + CI SBOM checks) |
| **Deployment Complexity** | Simple (1 container) | Moderate (API + worker + Redis + proxy) | Moderate–High (+ Playwright worker pool) | Low initially, painful to scale later | **Moderate & Cleanly Modular** (Docker Compose for dev/small scale → Kubernetes for production) |

### Strategic Verdict
- **Option A** fails the "Universal" product goal immediately (breaks on image galleries, PDFs, and JS players).
- **Option D** is disqualified by **AGPL-3.0** on mature web projects (`Cobalt`, `MeTube`) and prototype immaturity/single-node coupling on MIT projects (`Grab`, `MaxDL`, `OmniFetch`).
- **Option E combined with Option C's tiered engine lineup** is the clear winner: you get **Option B's speed in MVP (Phase 1)** while owning a **future-proof Registry & Adapter architecture** that cleanly slots in `Streamlink`, `N_m3u8DL-RE`, `Playwright`, and future extractors without ever rewriting the API, queue, or frontend.

---

## 2. Deployment Strategy

### 2.1 Container & Service Topology

Split workloads into **5 purpose-built container images** so heavy dependencies (Chromium, FFmpeg, Deno) never bloat the API tier and network privileges stay strictly minimized:

| Service / Pod | Base Image & Key Packages | Network Policy | Scaling Trigger |
|---|---|---|---|
| **1. `api-orchestrator`** | Slim Python 3.12 (FastAPI) or Go/TS | Inbound from Ingress/LB; Outbound ONLY to PostgreSQL, Redis, S3/R2 (NO public untrusted egress!) | HTTP RPS / CPU |
| **2. `safe-egress-proxy`** | Distroless Go/Rust static binary | Inbound ONLY from `worker-extract` & `worker-browser`; Outbound to Public Internet ports `80,443` ONLY (firewall drops RFC1918/IMDS/CGNAT) | Active connections / Bandwidth |
| **3. `worker-extract`** | Python 3.12-slim + `yt-dlp[default,curl-cffi]` + `Deno >=2.9` + `gallery-dl` + `streamlink` + `aria2c` + `N_m3u8DL-RE` + `trafilatura` | **Internal network ONLY** (no default internet route; all egress via `HTTP_PROXY=http://safe-egress-proxy:3128`) | Redis queue depth (`q:probe`, `q:download`) |
| **4. `worker-browser`** | Official `mcr.microsoft.com/playwright` (or Chrome channel) under gVisor/Kata | **Internal network ONLY** (egress strictly via `safe-egress-proxy`) | Redis queue depth (`q:browser`), max 2–4 contexts/pod |
| **5. `worker-media`** | Minimal Alpine/Debian + `FFmpeg` / `ffprobe` (`yt-dlp/FFmpeg-Builds` or distro) | **`network_mode: none` (ZERO external egress)** — speaks only to local volume or internal S3 endpoint via sidecar/presigned handoff | Redis queue depth (`q:media`) / CPU |
| **6. `pot-provider` *(Optional)*** | `brainicism/bgutil-ytdlp-pot-provider` | Inbound ONLY from `worker-extract`; egress via `safe-egress-proxy` | YouTube request volume |

### 2.2 Operational Best Practices
- **Continuous Engine Freshness Pipeline:** Extraction sites break weekly. Run a **nightly CI smoke-test workflow** against canary public URLs (e.g., Creative Commons video, public image gallery, HLS test stream, PDF, static webpage). When `yt-dlp`, `gallery-dl`, or `streamlink` releases a fix, automatically build, canary-test, and rolling-deploy the `worker-extract` image—zero downtime for the API plane.
- **Dev vs. Production Parity:**
  - **Local / Small Production:** Single `docker-compose.yml` with two Docker bridge networks (`internal_net` with `internal: true` for workers, `egress_net` for `safe-egress-proxy` and `api`), Redis, Postgres, and MinIO.
  - **Scale Production:** Kubernetes (EKS/GKE/Hetzner K8s) with `NetworkPolicy` enforcement, KEDA autoscaling on Redis queue depths, ephemeral NVMe `emptyDir` scratch volumes with size limits, Cloudflare R2 / AWS S3 temporary storage with 6–24h lifecycle expiration, and isolated download subdomain.
- **Observability:** Structured JSON logs (with URLs scrubbed of auth tokens/cookies), Prometheus metrics (`probe_latency_ms{engine}`, `extraction_success_rate{engine,domain}`, `fallback_rate`, `egress_proxy_blocked_total{reason}`, `scratch_disk_free_bytes`), and OpenTelemetry traces spanning `API → Queue → Worker → Proxy → Media Worker`.

---

## 3. Phased Development Roadmap

### Phase 1 — Foundation & MVP (Weeks 1–6)
**Goal:** Ship a production-safe, API-first Universal Media Extraction Engine covering ~85% of real-world URLs with Day-1 security and licensing discipline.

* **Core Platform:**
  * Implement Universal Engine Registry (`engine-manifest.schema.json`) and common `ExtractorAdapter` interface.
  * Implement canonical `NormalizedMediaOutput` JSON contract (`normalized-media-output.schema.json`).
  * Build Control Plane REST API (`/v1/probe`, `/v1/jobs`, `/v1/jobs/{id}`, `/v1/jobs/{id}/events` via SSE, `/v1/engines`) backed by PostgreSQL + Redis queues.
* **MVP Engines (4 + 1 Post-Processor):**
  * `direct-http` (fast `HEAD`/range probe + static HTML OG/JSON-LD via `Trafilatura`)
  * `aria2` subprocess adapter (large direct files, PDFs, archives)
  * `yt-dlp` subprocess adapter (+ `Deno` + `yt-dlp-ejs` + `curl_cffi`)
  * `gallery-dl` subprocess adapter (with `--range` item caps)
  * `ffmpeg` / `ffprobe` zero-egress validation, lossless remuxing, audio extraction, thumbnail extraction
* **Mandatory Security & Infra:**
  * Go/Rust Safe Egress Proxy + Docker `internal: true` worker isolation.
  * Per-job scratch directories, magic-byte validation, S3/R2 temporary storage with TTL, isolated download origin with `Content-Disposition: attachment`.
  * Rate limits, quotas, cancellation (`SIGKILL` process group), and CI SBOM license gate.

### Phase 2 — High-Reliability Expansion & Browser Fallback (Weeks 7–12)
**Goal:** Push content coverage to ~95%+, add live streams, fast segmented streams, and JS-rendered media fallback.

* **New Adapters:**
  * `Playwright` Tier-3 Browser Fallback pool (network manifest sniffing, DOM/Shadow DOM/iframe inspection, EME DRM detection hook, `context.route` bloat blocking, handoff to Tier-2 downloader).
  * `Streamlink` adapter (live stream metadata & duration-capped recording).
  * `N_m3u8DL-RE` adapter (high-speed multi-threaded HLS/DASH/ISM downloading).
  * Optional `bgutil-ytdlp-pot-provider` sidecar for YouTube PO tokens.
* **Enhanced Capabilities:**
  * Intelligent domain-level circuit breakers & outdated-extractor auto-detection alerts.
  * Multi-file gallery ZIP packaging (`ZIP_STORED` + manifest).
  * Subtitle & transcript normalization (`.vtt`/`.srt` → structured tracks + plain text).
  * Nightly canary smoke-test & auto-update CI pipeline for extractors.

### Phase 3 — Advanced Media Processing & Multi-Client Ecosystem (Weeks 13–18)
**Goal:** Productize into a reusable platform powering web, mobile, desktop, and API developers.

* **Media Features:**
  * Video clipping/trimming, preset transcode profiles (H.264/HEVC/WebM/MP3/Opus/FLAC), chapter splitting, thumbnail contact sheets.
  * Encrypted per-user **Credential / Cookie Vault** (strictly opt-in, AES-256-GCM at rest, ephemeral per-job injection, never logged or shared across tenants) for user-owned authenticated content.
  * Content-addressed SHA-256 artifact deduplication & singleflight inflight request coalescing.
* **Client Ecosystem:**
  * Decoupled Reference Web App, OpenAPI-generated TypeScript/Python/Swift/Kotlin SDKs, Webhook delivery callbacks for B2B/API users, and optional Desktop companion app (using LGPL FFmpeg build or on-demand engine installer).

### Phase 4 — Scale, Hardening & Enterprise Operations (Ongoing)
* Kubernetes multi-region deployment with gVisor/Firecracker isolation for browser & media pods.
* Optional egress proxy pool rotation (compliance-reviewed ISP/egress routing for rate-limit resilience).
* Optional Malware/AV scanning sidecar (ClamAV) for direct binary downloads.
* Optional GPU-accelerated ASR transcription (Whisper) when public captions are unavailable.
