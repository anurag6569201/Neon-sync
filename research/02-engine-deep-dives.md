# 02 — Engine Deep Dives & Comparison

> Every metric, directory count, dependency, and license below was verified directly from the upstream GitHub repositories in **November 2026**.

---

## 1. Master Comparison Matrix

| Project | Category | Language | Stars | Latest Release / Last Commit | License (Verified) | Extract vs Download | Programmatic Integration | Docker / Linux | Production Fit | Verdict |
|---|---|---|---:|---|---|---|---|---|---|---|
| **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** | Video / Audio / Generic | Python ≥3.11 | 209.6k | `2026.10.26` / `2026-11-21` | **Unlicense** *(deps: `mutagen` GPL-2.0; binaries GPLv3+)* | Both (`--dump-single-json` + native/fragment/external downloaders) | CLI JSON subprocess or Python API (`YoutubeDL`) | Excellent | High (with Deno + egress proxy) | **USE (Core)** |
| **[youtube-dl](https://github.com/ytdl-org/youtube-dl)** | Video / Audio | Python | 141.6k | `2021.12.17` / `2026-11-06` | **Unlicense** | Both | CLI / Python API | Yes | Low (stale releases, broken throttling) | **AVOID** |
| **[gallery-dl](https://github.com/mikf/gallery-dl)** | Images / Galleries / Albums | Python | 20.6k | `v1.32.21` / `2026-11-21` | **GPL-2.0** | Both (`-j` JSON probe + HTTP/text/ytdl downloaders) | CLI subprocess (recommended for GPL) or Python `DataJob` | Excellent | High (via subprocess) | **USE (Core)** |
| **[aria2](https://github.com/aria2/aria2)** | Direct / Multi-conn / BitTorrent | C++ | 43.6k | `1.37.0` (`2023-11`) / `2026-06-25` | **GPL-2.0** | Download only | CLI subprocess or JSON-RPC/XML-RPC | Excellent | High (as ephemeral CLI; careful with RPC) | **USE (Direct Files)** |
| **[GNU Wget](https://github.com/mirror/wget)** | Direct / Recursive Mirror | C | 467 *(mirror)* | `v1.24.5` / `2024-06-09` | **GPL-3.0** | Download only | CLI only | Excellent | Low for multi-conn API backend | **AVOID** |
| **[Wget2](https://github.com/rockdaboot/wget2)** *(Bonus)* | Direct HTTP/2 / Mirror | C | 806 | `v2.3.0` / `2026-11-05` | **GPL-3.0** (CLI) / **LGPL-3.0** (`libwget`) | Download only | CLI or C `libwget` | Excellent | Medium (overkill vs `httpx`+`aria2`) | **OPTIONAL** |
| **[Streamlink](https://github.com/streamlink/streamlink)** | Live Streams | Python ≥3.10 | 12.0k | `8.6.3` / `2026-11-18` | **BSD-2-Clause** | Both (`--json` probe + stream piping/recording) | CLI subprocess or Python `Streamlink()` API | Excellent | High | **USE (Live Tier)** |
| **[FFmpeg](https://github.com/FFmpeg/FFmpeg)** | Media Processing / Validation | C / ASM | 67.8k | `n9.1` / `2026-11-21` | **LGPL-2.1+** default / **GPL-2.0+/3.0** with x264/x265 | Process + direct protocol fetch (disable network in transcode tier) | CLI (`ffmpeg`/`ffprobe`) or `libav*` C libs | Excellent | Essential (must sandbox) | **USE (Core)** |
| **[Cobalt](https://github.com/imputnet/cobalt)** | Social Media API / Proxy | Node.js / Svelte | 46.4k | `v11.7.1` / `2026-04-06` | **AGPL-3.0** | Extract + streaming tunnel mux | REST HTTP API (`POST /`, `/tunnel`) | Excellent (official Docker) | Medium (21 sites; AGPL blocks proprietary use) | **DO NOT EMBED** |
| **[Grab](https://github.com/S-k404/grab)** | Multi-Engine Desktop/CLI | Python / Swift / JS | 1 | No release / `2026-11-18` | **MIT** | Orchestrates yt-dlp + gallery-dl + aria2 + CDP browser | Local HTTP GUI + CLI | macOS-centric | Low (brand new, desktop focus) | **BORROW IDEAS** |
| **[MeTube](https://github.com/alexta69/metube)** | Web UI for yt-dlp | Python / Angular | 15.3k | `2026.11.14` / `2026-11-14` | **AGPL-3.0** | Wraps `yt-dlp` only | REST + Socket.IO web app | Excellent (Docker) | High for self-hosted, unusable as proprietary base | **DO NOT FORK** |
| **[MaxDL](https://github.com/star1133/media-downloader)** | Multi-Engine Web App | Python (FastAPI) | 0 | No release / `2026-10-28` | **MIT** | Orchestrates yt-dlp + pytubefix + gallery-dl + aria2 | FastAPI REST | Linux-friendly | Low (early prototype, single-process queue) | **BORROW IDEAS** |
| **[OmniFetch](https://github.com/TheRealGudPerson/OmniFetch)** | Multi-Engine Termux CLI | Python (stdlib) | 0 | No release / `2026-11-14` | **MIT** | Probes & routes yt-dlp, streamlink, gallery-dl, aria2, cobalt | CLI only | Linux/Termux | Low (CLI prototype) | **BORROW IDEAS** |
| **[Playwright](https://github.com/microsoft/playwright)** | Browser Automation | TypeScript / Python / Go | 100.7k | `v1.65.1` / `2026-11-21` | **Apache-2.0** | Render + DOM/Network sniffing | First-class Python/TS/Go/Java SDKs | Excellent (official images) | High (with pool + egress proxy) | **USE (Fallback)** |
| **[Puppeteer](https://github.com/puppeteer/puppeteer)** | Browser Automation | TypeScript | 95.9k | `v25.18.1` / `2026-11-21` | **Apache-2.0** | Render + DOM/Network sniffing | Node.js SDK (CDP / WebDriver BiDi) | Good | Medium (Playwright has stronger isolation & Python API) | **AVOID (Use Playwright)** |
| **[N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE)** *(Bonus)* | HLS / DASH / ISM Streams | C# (.NET AOT) | 9.1k | `v0.6.0-beta` / `2026-11-04` | **MIT** | Download + live record + mux | Single native CLI binary | Excellent | High (fast segmented streams) | **USE (Optional Tier)** |
| **[Trafilatura](https://github.com/adbar/trafilatura)** *(Bonus)* | Webpage Text & Metadata | Python | 7.2k | `v2.3.1` / `2026-11-20` | **Apache-2.0** | HTML/OG/JSON-LD/Article extraction | Python library + CLI | Excellent | High | **USE (Webpages)** |
| **[ArchiveBox](https://github.com/ArchiveBox/ArchiveBox)** *(Bonus)* | Web Archiving Orchestrator | Python | 29.1k | `v0.9.77` / `2026-11-21` | **MIT** | Plugin-based multi-extractor archiver | CLI / Django Web | Excellent | High (archival; study plugin hooks) | **BORROW IDEAS** |
| **[Lux](https://github.com/iawia002/lux)** *(Bonus)* | Video Downloader (CN focus) | Go | 31.9k | `v0.24.1` / `2025-12-29` | **MIT** | Both (49 extractors) | Go library + single CLI binary | Excellent | Medium (slower 2026 updates) | **OPTIONAL NICHE** |

---

## 2. Detailed Repository Deep Dives

### 2.1 Video & Audio Extraction

#### 1. `yt-dlp/yt-dlp` — https://github.com/yt-dlp/yt-dlp
* **What it does:** Flagship audio/video metadata extractor and downloader supporting **941 extractor modules** (verified in `yt_dlp/extractor/`) covering ~1,800+ websites, plus generic HTML5/embed extraction.
* **Languages & Tech:** Pure Python (`requires-python >= 3.11` as of `2026.10.26`). Optional dependencies in `pyproject.toml`: `yt-dlp-ejs==0.8.0` (Unlicense), `deno>=2.9.8` (MIT), `curl-cffi` (MIT, TLS JA3/JA4 browser impersonation), `mutagen` (**GPL-2.0**, audio tagging), `pycryptodomex`, `requests`, `urllib3`, `websockets`, `brotli`.
* **Protocols & Content:** HTTP/HTTPS, HLS (`.m3u8`), DASH (`.mpd`), ISM (Smooth Streaming), RTMP, F4M, WebSocket, MHTML storyboards, live chat, subtitles (manual & auto-generated), chapters, SponsorBlock segments, playlists, channels, thumbnails.
* **Internal Architecture:** Modular 4-stage pipeline verified in source tree:
  1. `yt_dlp/networking/` — Pluggable HTTP backends (`_curlcffi.py`, `_requests.py`, `_urllib.py`, `_websockets.py`, `impersonate.py`).
  2. `yt_dlp/extractor/` — 941 site modules inheriting from `InfoExtractor`, each defining `_VALID_URL` regexes and `_real_extract()`. Includes `GenericIE` for arbitrary webpages.
  3. `yt_dlp/downloader/` — 17 specialized downloaders (`http.py`, `hls.py`, `dash.py`, `fragment.py` with concurrent fragments, `external.py` bridging to `aria2c`/`ffmpeg`/`curl`, `rtmp.py`, `ism.py`).
  4. `yt_dlp/postprocessor/` — 10 post-processors (`ffmpeg.py`, `embedthumbnail.py`, `metadataparser.py`, `sponsorblock.py`, `modify_chapters.py`, `exec.py`).
* **Modern 2025–2026 Requirement (Critical Finding):** YouTube extraction now relies on **External JavaScript (EJS)** via `yt-dlp-ejs` and an external JS runtime—**Deno (recommended & enabled by default)**, Node.js, or QuickJS—to solve player signature/`n`-throttling challenges. Server/datacenter IPs frequently also require **Proof-of-Origin (PO) tokens** via plugins like `Brainicism/bgutil-ytdlp-pot-provider` (HTTP sidecar) or residential/ISP proxies + cookies.
* **Integration:** Programmatic via `yt_dlp.YoutubeDL(opts).extract_info(url, download=False)` or isolated CLI subprocess (`yt-dlp --dump-single-json --skip-download --no-playlist ...`). **Subprocess is strongly preferred in production** for crash/memory isolation, clean cancellation (`SIGKILL` process group), and avoiding `mutagen` GPL-2.0 in-process contamination.
* **Extensibility:** Native Python namespace plugin system (`yt_dlp/plugins.py`) loading `yt_dlp_plugins.extractor` and `yt_dlp_plugins.postprocessor` from config directories or installed packages.
* **Auth & Cookies:** Supports Netscape `cookies.txt`, `--cookies-from-browser`, `.netrc`, OAuth/PO-token plugins, custom HTTP headers.
* **Limitations:** Weak/non-existent support for pure image galleries/albums; `GenericIE` cannot execute JavaScript SPAs; YouTube/Instagram/TikTok heavily rate-limit datacenter IPs; `--exec` and unsafe `--output` templates are security hazards if exposed to user input; `curl_cffi` bypasses Python socket hooks (requires egress proxy!).
* **Verdict:** **USE AS PRIMARY VIDEO/AUDIO ENGINE** via isolated CLI subprocess adapter with Deno + `yt-dlp-ejs` + `curl_cffi`.

#### 2. `ytdl-org/youtube-dl` — https://github.com/ytdl-org/youtube-dl
* **What it does:** Original Python video downloader (811 extractor files).
* **Maintenance & Activity:** Effectively in minimal maintenance mode. Last formal GitHub release was **`2021.12.17`** (~5 years ago); commit log shows only ~10 commits across an entire year (`2025-11` to `2026-11`). Lacks `yt-dlp`'s EJS runtime, `curl_cffi` TLS impersonation, parallel fragment downloading, SponsorBlock, PO-token plugin architecture, and rapid breakage fixes.
* **License:** Unlicense.
* **Verdict:** **AVOID COMPLETELY.** Strictly superseded by `yt-dlp`.

---

### 2.2 Image & Gallery Extraction

#### 3. `mikf/gallery-dl` — https://github.com/mikf/gallery-dl
* **What it does:** Specialized image, photo album, comic/manga, artwork, and social-media gallery extractor covering **258 extractor modules** (`docs/supportedsites.md` is 66 KB — 300+ sites including Instagram, X/Twitter, Reddit, Imgur, Pixiv, DeviantArt, Pinterest, Flickr, Tumblr, Bluesky, ArtStation, Danbooru/boorus, MangaDex).
* **Languages & Tech:** Python 3, minimal dependencies (`requests`).
* **Internal Architecture:** Verified from source tree:
  * `gallery_dl/extractor/` — 258 modules; URL routing via `gallery_dl.extractor.find(url)`.
  * `gallery_dl/downloader/` — `http.py`, `text.py`, and `ytdl.py` (can optionally delegate embedded videos to `yt-dlp`).
  * `gallery_dl/postprocessor/` — 14 post-processors (`metadata.py`, `zip.py`, `ugoira.py` for Pixiv animations via FFmpeg, `hash.py`, `classify.py`, `compare.py`, `exec.py`).
* **Extraction vs Download:** Clean separation! CLI `-j` / `--dump-json` or `-g` / `--get-urls` extracts metadata & direct media URLs without downloading; Python API has `DataJob` vs `DownloadJob`.
* **Auth & Cookies:** Extensive per-site auth (OAuth, API keys, cookies.txt, username/password) configured via JSON config file (`-c`).
* **License (CRITICAL VERIFIED CORRECTION):** `LICENSE` file is **GNU GPL v2.0** (NOT public domain / Unlicense!).
* **Production Integration:** **Must invoke via CLI subprocess** (`gallery-dl --dump-json --range 1-100 ...`) rather than `import gallery_dl` inside proprietary Python code, ensuring strict GPL process isolation AND memory/timeout containment. Always enforce `--range` caps to prevent a user submitting a 50,000-image profile from exhausting disk/bandwidth.
* **Limitations:** No JS rendering for arbitrary unsupported sites; strict pagination/rate limits on Instagram/X/Pixiv without cookies; large galleries can explode item counts if uncapped.
* **Verdict:** **USE AS PRIMARY IMAGE & GALLERY ENGINE** via subprocess adapter with item-count and byte quotas.

---

### 2.3 Direct Downloads

#### 4. `aria2/aria2` — https://github.com/aria2/aria2
* **What it does:** Ultra-lightweight C++ multi-protocol, multi-connection download accelerator supporting HTTP/HTTPS, FTP, SFTP, BitTorrent, and Metalink.
* **Languages & Tech:** C++11 (`libcurl`-independent custom event loop, GnuTLS or OpenSSL, SQLite optional). Low memory footprint (~10–30 MB RAM).
* **Capabilities:** Segmented parallel connections (`-x 8 -s 8`), resumable downloads (`-c`), speed caps (`--max-download-limit`), checksum verification, custom headers/cookies, HTTP proxy support (`--all-proxy`), JSON-RPC & XML-RPC daemon mode.
* **Maintenance:** Stable/mature low-velocity maintenance (`release-1.37.0` from Nov 2023; minor security/GnuTLS fixes in May–June 2026). Very battle-tested.
* **License:** **GPL-2.0** (`COPYING`).
* **Security Caveat (CRITICAL):** Never expose `aria2c --enable-rpc` beyond localhost, and preferably **do NOT run a shared multi-tenant RPC daemon** because `aria2.addUri` accepts arbitrary `dir`/`out` paths and BitTorrent/FTP/local schemes. Instead, spawn **per-job ephemeral `aria2c` CLI subprocesses** with `--enable-rpc=false`, `--allow-overwrite=false`, `--auto-file-renaming=false`, explicit `-d <job_sandbox_dir> -o <uuid_filename>`, `--all-proxy=<safe_egress_proxy>`, and BitTorrent/Metalink disabled (`--follow-torrent=false --follow-metalink=false`).
* **Verdict:** **USE FOR LARGE DIRECT FILE DOWNLOADS** (e.g., >10 MB files, ISOs, ZIPs, PDFs, direct MP4s) as an isolated per-job CLI subprocess; use a native async HTTP client (`httpx`/`undici`) for probes and small files.

#### 5. `mirror/wget` (GNU Wget) & `rockdaboot/wget2`
* **What they do:** Classic GNU C downloaders supporting HTTP/HTTPS/FTP and recursive website mirroring (`-m`, `-r`). `wget2` (`v2.3.0`, Nov 2026) adds HTTP/2, multithreading, and `libwget` (LGPL-3.0).
* **License:** `wget` is **GPL-3.0**; `wget2` CLI is **GPL-3.0** (`COPYING`) and `libwget` is **LGPL-3.0** (`COPYING.LESSER`).
* **Limitations:** No media extraction intelligence; single-connection in `wget` 1.x; recursive mirroring is dangerous on untrusted user URLs (spider traps, unbounded disk growth).
* **Verdict:** **AVOID FOR CORE PIPELINE.** Native async HTTP (`httpx`/`undici`) + `aria2c` cover direct downloads faster, cleaner, and with tighter programmatic control.

---

### 2.4 Live Streams

#### 6. `streamlink/streamlink` — https://github.com/streamlink/streamlink
* **What it does:** Specialized live-stream extractor with **136 platform plugins** (`src/streamlink/plugins/` — Twitch, YouTube Live, Kick, Vimeo Live, TikTok Live, AfreecaTV/SOOP, Bilibili Live, PlutoTV, etc.) plus generic `hls://` and `dash://` protocol handlers.
* **Languages & Tech:** Python `>=3.10`, `requests`, `lxml`, `trio`, `trio-websocket`, `pycryptodome`, `isodate`. Very actively maintained (`8.6.3`, Nov 2026, only 72 open issues!).
* **Internal Architecture:** Clean plugin registry (`session.plugins.match_url(url)`) returning quality-keyed stream objects (`HLSStream`, `DASHStream`, `MuxedStream` via FFmpeg, `HTTPStream`). Supports `--json` for fast non-downloading metadata & stream enumeration, `--hls-live-restart`, `--hls-duration` (crucial for capping server recording time!), and `--output` direct recording.
* **License:** **BSD-2-Clause** (Permissive! Safe even for direct Python import or commercial bundling, though subprocess execution is still preferred for worker isolation).
* **Limitations:** Focused strictly on live/VOD streaming video; does not handle playlists, image galleries, or static metadata rich enough for VOD archiving (where `yt-dlp` is richer); live recordings have unbounded length unless capped by max duration & byte quotas.
* **Verdict:** **USE FOR LIVE STREAMS (Phase 2).** Best-in-class live-stream stability and permissive BSD-2-Clause license.

---

### 2.5 Media Processing

#### 7. `FFmpeg/FFmpeg` — https://github.com/FFmpeg/FFmpeg
* **What it does:** The universal Swiss Army knife for multimedia probing (`ffprobe`), container remuxing (`-c copy`), transcoding, audio extraction, video clipping/trimming, thumbnail/sprite generation, subtitle muxing, and HLS/DASH assembly.
* **Languages & Tech:** C and platform-specific assembly; active daily commits (`n9.1`, Nov 2026).
* **License (CRITICAL NUANCE):** Verified directly from `LICENSE.md`:
  * Default build: **LGPL-2.1+** (or **LGPL-3.0** with `--enable-version3`).
  * With `--enable-gpl` (required for `libx264`, `libx265`, `libxvid`, `libvidstab`, and GPL filters like `vf_delogo`/`vf_cropdetect`): **GPL-2.0+** (or **GPL-3.0** with `--enable-version3`).
  * With `--enable-nonfree` (e.g., `libfdk-aac`): **Unredistributable** (server-side internal use only; cannot ship binaries).
  * `yt-dlp/FFmpeg-Builds` publishes patched Linux x64/arm64 static builds (both GPL and LGPL variants).
* **Security Risks (CRITICAL):** Historical and recent CVEs (CVE-2016-1897/1898, CVE-2026-107697) allow malicious `.m3u8`/crafted media files to trigger SSRF, Local File Read (`concat:`, `subfile:`), or memory corruption in C demuxers. **Rule:** In the media-processing tier, invoke `ffmpeg`/`ffprobe` inside a **zero-network-egress sandbox** with `-protocol_whitelist file,pipe` (strictly local files only—never let post-processing FFmpeg touch the network!), `seccomp`, and strict CPU/RAM/wall-clock limits.
* **Verdict:** **USE AS MANDATORY CORE MEDIA ENGINE** via isolated CLI subprocesses.

---

### 2.6 Alternative Extractors & APIs

#### 8. `imputnet/cobalt` — https://github.com/imputnet/cobalt
* **What it does:** Fast, ad-free Node.js media downloader & streaming proxy supporting **21 major social platforms** (`api/src/processing/services/`: Bilibili, Bluesky, Dailymotion, Facebook, Instagram, Loom, Newgrounds, OK, Pinterest, Reddit, Rutube, Snapchat, SoundCloud, Streamable, TikTok, Tumblr, Twitch, Twitter, Vimeo, VK, YouTube).
* **Internal Architecture (Verified from `api/src/`):**
  * Express 4 + Undici HTTP dispatcher + `isolated-vm` (sandboxed JS execution!) + `ffmpeg-static` + `hls-parser` + `ipaddr.js` + `YouTube.js` (InnerTube).
  * Clean URL canonicalization in `api/src/processing/url.js` (normalizing `youtu.be`, `x.com`/`vxtwitter`, `pin.it`, `b23.tv`, `dai.ly`, `clips.twitch.tv`).
  * Stateless streaming **tunnel architecture** (`api/src/core/itunnel.js`, `stream/stream.js`) that pipes remuxed media directly to the client without storing files permanently on disk.
  * Built-in Turnstile, JWT, HMAC secrets, Redis rate limiting, and cookie rotation manager (`processing/cookie/manager.js`).
* **License:** **AGPL-3.0** (`LICENSE` & `api/package.json`).
* **Limitations:** Only 21 platforms (no generic extractor, no playlists/channels, no arbitrary files/galleries); public API is closed/protected; **AGPL-3.0 requires releasing full network-service source code if modified/integrated into a SaaS**.
* **Verdict:** **DO NOT USE OR FORK.** Study its URL canonicalizer, cookie pool manager, and streaming tunnel pattern, then re-implement cleanly in our own codebase.

---

### 2.7 Multi-Engine Orchestration References

#### 9. `S-k404/grab` — https://github.com/S-k404/grab
* **What it does:** MIT-licensed Python multi-engine batch downloader (created Nov 2026, 1 ★) combining `yt-dlp`, `gallery-dl`, `aria2c`, `curl_cffi`, and a **direct Chrome DevTools Protocol (CDP) headless browser sniffer** (`grab/browser.py`), plus a Chrome extension, macOS Swift wrapper, and local web GUI.
* **Standout Ideas Verified in Source Code:**
  * **EME DRM Detection (`grab/browser.py`):** Monkey-patches `navigator.requestMediaKeySystemAccess` at page init to capture DRM key systems (`window.__grabDrm`) and report DRM cleanly instead of hanging.
  * **Shadow DOM Media Discovery (`_TRIGGER_MEDIA_JS`):** Recursively traverses `el.shadowRoot` to find hidden `<video>`/`<audio>` tags and clicks common player selectors (`.vjs-big-play-button`, `.plyr__control--overlaid`, `.jw-display-icon-container`).
  * **Unified Global Speed-Cap Proxy (`grab/limiter.py`):** Routes all heterogeneous tools (`yt-dlp`, `gallery-dl`, `aria2c`) through a single local token-bucket HTTP proxy so bandwidth caps apply globally across engines.
  * **Self-Healing Outdated-Extractor Detection (`grab/updater.py`):** Pattern-matches error messages (`"nsig"`, `"signature extraction"`, `"challenge solving"`) to flag stale `yt-dlp` versions.
* **Limitations:** Single-user desktop/macOS design (`127.0.0.1`, local browser binaries, no SSRF egress protection, monolithic `core.py` at 121 KB).
* **Verdict:** **DO NOT FORK; BORROW BROWSER SNIFFING, DRM DETECTION & OUTDATED-ERROR PATTERNS.**

#### 10. `alexta69/metube` — https://github.com/alexta69/metube
* **What it does:** Mature, wildly popular (15.3k ★, release `2026.11.14`) self-hosted web UI for `yt-dlp` built with Python (`aiohttp` + `python-socketio`) and Angular 22.
* **Standout Ideas Verified in Source Code:**
  * **`app/url_guard.py`:** Honest, deeply engineered SSRF analysis documenting why Python `socket.getaddrinfo` guards fail against `curl_cffi` and remote-DNS proxies, and why network isolation is mandatory.
  * **`app/format_catalog.py` & `app/dl_formats.py`:** Single-source-of-truth declarative format catalog shared by API validation, yt-dlp mapping, and frontend UI.
  * **`app/state_store.py`:** Atomic temp-file + rename JSON persistence with explicit `errno` handling (`ENOSPC`/`EIO` never silently corrupt state).
  * **`app/download_options.py`:** Immutable frozen `DownloadOptions` value object that redacts secrets (`video_password`, `cookiefile`) from `__repr__`.
* **License:** **AGPL-3.0** (`LICENSE` verified!).
* **Limitations:** Single-engine (`yt-dlp` only); no multi-tenant isolation; AGPL-3.0 prevents proprietary SaaS reuse.
* **Verdict:** **DO NOT FORK; STUDY ITS SSRF ANALYSIS, FORMAT CATALOG & SECRET-SAFE OPTIONS DESIGN.**

#### 11. `star1133/media-downloader` (MaxDL) — https://github.com/star1133/media-downloader
* **What it does:** MIT-licensed FastAPI multi-engine web downloader (created Oct 2026, 0 ★) combining `yt-dlp`, `pytubefix` (YouTube fallback worker), `gallery-dl`, `aria2`, and `ffmpeg`.
* **Standout Ideas Verified in Source Code:**
  * **`safe_proxy.py` (247 lines — pure gold):** Asyncio HTTP/CONNECT egress proxy restricting ports to `{80, 443}`, resolving DNS inside the proxy, rejecting any non-global IP (`not ip.is_global`), and dialing the validated IP directly (`asyncio.open_connection(address, port, family=family)`). Eliminates TOCTOU DNS rebinding for every downstream CLI tool!
  * **Strict Production Abuse Quotas (`app.py`):** Hard-coded guardrails: 300 items/job, 2 GiB/job, 30 min max runtime, 10 GiB min free disk floor before accepting work, 6-hour auto-TTL cleanup, 8 jobs/IP/hour.
* **Limitations:** Brand-new single-process prototype (in-memory queue, `app.py` monolith, no distributed workers, Python proxy throughput ceiling under high load, `ip.is_global` needs extra checks for IPv4-mapped IPv6/CGNAT).
* **Verdict:** **DO NOT FORK; ADOPT ITS SAFE EGRESS PROXY & HARD QUOTA ARCHITECTURE (REBUILT IN GO/RUST FOR SCALE).**

#### 12. `TheRealGudPerson/OmniFetch` — https://github.com/TheRealGudPerson/OmniFetch
* **What it does:** MIT-licensed, zero-dependency Python stdlib CLI orchestrator for Termux (created Nov 2026, 0 ★) supporting `yt-dlp`, `streamlink`, `gallery-dl`, `aria2`, and optional user-configured `cobalt`.
* **Standout Ideas Verified in Source Code:**
  * **`ProbeResult` & Parallel Confidence Ranking (`omnifetch/models.py`, `engines.py`):** Clean adapter abstraction where every engine returns `(engine, status, supported, confidence, message, metadata)` and all engines are launched strictly with `subprocess.run(argv_list)` (never `shell=True`).
  * **Explicit Ethical & License Guardrails:** Refuses to hit Cobalt's public API without user-owned instance configuration; explicitly refuses DRM/paywall bypass.
* **Limitations:** Termux CLI-only; probes all installed engines blindly on every URL (too slow/wasteful at SaaS scale without domain/HEAD pre-filtering).
* **Verdict:** **DO NOT FORK; BORROW ITS CLEAN ADAPTER INTERFACE & CONFIDENCE-SCORED `ProbeResult` MODEL.**

---

### 2.8 Browser-Based Extraction

#### 13. `microsoft/playwright` — https://github.com/microsoft/playwright
* **What it does:** Cross-browser automation framework (Chromium, Firefox, WebKit) with first-class TypeScript, Python, Go, and .NET bindings (100.7k ★, `v1.65.1`, Apache-2.0).
* **Why it beats Puppeteer for our fallback tier:**
  * Lightweight, hermetically isolated **`BrowserContext`** per job (separate cookies, storage, cache, proxy settings—tens of milliseconds vs launching a new browser process).
  * Superior network interception (`context.route("**/*", handler)`) with request/response body inspection (`response.text()` for `.m3u8`/`.mpd`), resource blocking (skip fonts/CSS/analytics to save 70%+ bandwidth), and auto-waiting.
  * Official Linux Docker images (`mcr.microsoft.com/playwright`) pre-configured with OS dependencies.
* **Critical Gotcha Verified:** Bundled open-source Chromium lacks proprietary **H.264/HEVC/AAC** decoders (causing MSE `addSourceBuffer` errors if trying to play encrypted/H.264 video). However, **manifest/URL network sniffing does NOT require decoding**, and if full playback triggering is needed, Playwright supports `channel="chrome"` or Firefox/codec-enabled builds.
* **Verdict:** **USE AS TIER-3 BROWSER FALLBACK ENGINE.**

#### 14. `puppeteer/puppeteer` — https://github.com/puppeteer/puppeteer
* **What it does:** Google-maintained Node.js/TypeScript automation library over Chrome DevTools Protocol (CDP) and WebDriver BiDi (95.9k ★, `v25.18.1`, Apache-2.0).
* **Limitations vs Playwright:** Node.js-only official SDK; less ergonomic multi-context isolation and request routing compared to Playwright; no WebKit engine.
* **Verdict:** **DO NOT USE.** Playwright offers strictly superior isolation, ergonomics, and cross-language support under the identical Apache-2.0 license.

---

### 2.9 Additional Maintained Projects Discovered

| Project | Repo | License | Why It Matters | Verdict |
|---|---|---|---|---|
| **N_m3u8DL-RE** | https://github.com/nilaoda/N_m3u8DL-RE | **MIT** | Native C#/.NET AOT binary (`v0.6.0-beta`, 9.1k ★) specialized for ultra-fast multi-threaded HLS (`.m3u8`), DASH (`.mpd`), and Smooth Streaming (`.ism`) downloading, live recording, and automatic FFmpeg muxing. Dramatically faster and more resilient on complex manifests than raw FFmpeg or yt-dlp's fragment downloader. | **Integrate in Phase 2** as dedicated HLS/DASH downloader adapter |
| **Trafilatura** | https://github.com/adbar/trafilatura | **Apache-2.0** | Best-in-class Python webpage text, article, Open Graph, JSON-LD, and metadata extractor (`v2.3.1`, 7.2k ★). Fast, low-RAM, handles static public webpages without launching a browser. | **Integrate in Phase 1/2** for public webpage & metadata extraction |
| **ArchiveBox** | https://github.com/ArchiveBox/ArchiveBox | **MIT** | Mature (29.1k ★, `v0.9.77`) plugin-based URL archiving engine (`archivebox/plugins/`). Great architectural reference for hook-based extractor discovery, snapshotting, and dependency health checks (`archivebox status`). | **Study plugin discovery & health-check architecture** |
| **Media Downloader (`mhogomchungu`)** | https://github.com/mhogomchungu/media-downloader | **GPL-2.0** | Mature (5.1k ★, `5.6.8`) C++/Qt desktop orchestrator wrapping 10+ tools (`yt-dlp`, `gallery-dl`, `aria2c`, `wget`, `deno`, `quickjs_ng`, `ffmpeg`, `svtplay-dl`, `you-get`). Validates the adapter-per-binary pattern. | **Study engine capability configuration; do not fork (desktop GPL-2.0)** |
| **bgutil-ytdlp-pot-provider** | https://github.com/Brainicism/bgutil-ytdlp-pot-provider | **GPL-3.0** | Standard HTTP sidecar (`2.0.2`, 911 ★) generating Proof-of-Origin tokens for `yt-dlp` on YouTube. | **Deploy as separate isolated HTTP sidecar** if YouTube bot-gating impacts server IPs |
| **YouTube.js** | https://github.com/LuanRT/YouTube.js | **MIT** | TypeScript InnerTube API client (`v18.3.0`, 5.4k ★) powering Cobalt's YouTube service. Useful as a fast, lightweight fallback if building a Node-based YouTube metadata cache. | **Optional future fallback** (yt-dlp handles wider edge cases) |
| **Lux** | https://github.com/iawia002/lux | **MIT** | Fast Go single-binary downloader (31.9k ★, 49 extractors) strong on Chinese video sites (Bilibili, iQIYI, Youku, QQ). Slower maintenance cadence in 2026. | **Optional niche adapter** only if Asian video coverage is prioritized |
| **spotDL** | https://github.com/spotDL/spotify-downloader | **MIT** | Resolves Spotify metadata and matches audio on YouTube Music (`v4.6.0`, 26.9k ★). Note: does not extract directly from Spotify (Spotify streams are DRM-protected). | **Avoid in core engine** (cross-source matching has accuracy & ToS nuances) |

---

## 3. Summary: Projects Worth Borrowing Ideas From vs. Projects to Avoid

### Worth Borrowing Architectural Ideas From
1. **`star1133/media-downloader` (MaxDL):** `safe_proxy.py` connect-time DNS-validated egress proxy + hard abuse quotas (max items, max bytes, min free disk floor, TTL cleanup).
2. **`S-k404/grab`:** Headless browser EME DRM detection (`navigator.requestMediaKeySystemAccess` hook), Shadow DOM `<video>`/`<audio>` traversal, global token-bucket speed proxy, and stale-extractor error signature detection.
3. **`alexta69/metube`:** Declarative single-source-of-truth `format_catalog`, immutable secret-redacting `DownloadOptions`, atomic state persistence, and candid SSRF threat modeling (`url_guard.py`).
4. **`TheRealGudPerson/OmniFetch`:** Clean `Engine` adapter contract with confidence-scored `ProbeResult` objects and strict `argv`-array subprocess invocation.
5. **`imputnet/cobalt`:** URL canonicalization (`youtu.be`, `x.com`, `pin.it`, `b23.tv`), stateless streaming tunnel delivery for low-storage proxying, and rotating cookie-pool management.
6. **`ArchiveBox/ArchiveBox`:** Modular plugin discovery, extractor health diagnostics, and multi-artifact output grouping.

### Projects We Should NOT Use (and Why)
1. **`ytdl-org/youtube-dl`:** Abandoned release cadence (no release since Dec 2021); broken throttling and missing modern JS/TLS solvers; 100% superseded by `yt-dlp`.
2. **`imputnet/cobalt` (as a dependency/fork):** **AGPL-3.0** copyleft trap for proprietary SaaS; narrow coverage (21 sites, no galleries/playlists/generic); public API prohibits third-party app integration.
3. **`alexta69/metube` (as a fork):** **AGPL-3.0** license; coupled to `yt-dlp` only; single-node self-hosted architecture without multi-tenant worker queues.
4. **`puppeteer/puppeteer`:** Redundant next to Playwright; weaker multi-context isolation and network routing; Node-only official SDK.
5. **`mirror/wget`:** Single-connection, GPL-3.0, recursive crawling hazards, inferior programmatic control vs native async HTTP + `aria2`.
6. **`MaxDL` / `Grab` / `OmniFetch` (as direct codebases to fork):** Early-stage (0–1 ★), single-node prototypes lacking distributed queues, multi-tenant auth, object storage, and production observability.
