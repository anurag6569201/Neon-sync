# 06 — Licensing & Commercial Compliance Matrix

> **Disclaimer:** This analysis is an engineering compliance audit verified directly against upstream `LICENSE`/`COPYING` files and dependency manifests (`pyproject.toml`, `package.json`, `LICENSE.md`) in **November 2026**. It is not formal legal counsel; consult IP counsel before commercial distribution of client binaries or on-premise appliances.

---

## 1. Critical Licensing Discoveries (Verified from Source)

Our source inspection uncovered **four high-impact licensing traps** that are frequently misreported in online summaries:

1. **`mikf/gallery-dl` is GPL-2.0 — NOT Public Domain / Unlicense.** Directly importing `gallery_dl` inside a proprietary Python server codebase creates strong copyleft risk. **Mitigation:** Invoke strictly as a standalone CLI subprocess (`gallery-dl ...`).
2. **`yt-dlp` source is Unlicense, BUT its default dependency `mutagen` is GPL-2.0 AND official standalone binaries are GPLv3+.** Installing `yt-dlp[default]` pulls `mutagen` (`quodlibet/mutagen`, verified `GPL-2.0`). **Mitigation:** Run `yt-dlp` in its own isolated CLI subprocess/worker environment—do not import `yt_dlp` in-process with proprietary business logic if you ever distribute the backend, or omit `mutagen` if embedding pure `yt-dlp` source.
3. **BOTH `imputnet/cobalt` AND `alexta69/metube` are AGPL-3.0.** Under AGPL-3.0 Section 13, modifying and running an AGPL service over a network requires disclosing the complete corresponding source code to every remote user. **Mitigation:** Do **not** fork, embed, or wrap either project in a proprietary commercial SaaS.
4. **FFmpeg's license depends entirely on compile-time flags.** Base FFmpeg is **LGPL-2.1+**, but enabling `libx264`, `libx265`, `libxvid`, or GPL filters upgrades the binary to **GPL-2.0+** (or **GPL-3.0** with `--enable-version3`). Most distro packages and `yt-dlp/FFmpeg-Builds` default builds are **GPL**. **Mitigation:** Always invoke `ffmpeg`/`ffprobe` as standalone CLI subprocesses (never dynamically/statically link `libav*` into proprietary code). If distributing a desktop app with bundled FFmpeg, either ship an **LGPL build** (using OpenH264 / SVT-AV1 / libvpx / OS hardware encoders) or comply with GPL binary distribution rules for the separate `ffmpeg` executable.

---

## 2. Complete Component Licensing Matrix

| Component | Verified License | Category | Commercial Use Allowed? | Proprietary App Safe (SaaS Server-Side)? | Proprietary App Safe (Distributed Desktop/Appliance)? | Obligations When Modified | Obligations When Distributed | Integration Rule |
|---|---|---|---|---|---|---|---|---|
| **yt-dlp (source)** | **Unlicense** | Public Domain | Yes | Yes | Yes *(check transitive deps!)* | None | None for yt-dlp source itself | **Safe** (still isolate via subprocess for `mutagen` & security) |
| **yt-dlp (`[default]` / binaries)** | **GPL-2.0** (`mutagen`) / **GPL-3.0+** (PyInstaller binaries) | Strong Copyleft | Yes | Yes via subprocess | Yes as separate CLI binary w/ GPL notices & source offer | Disclose modifications to GPL components if distributed | Include `THIRD_PARTY_LICENSES.txt`, GPL text, and source offer for binary | **Subprocess only**; never link in-process |
| **yt-dlp/ejs** | **Unlicense** | Public Domain | Yes | Yes | Yes | None | None | **Safe** |
| **Deno (JS runtime)** | **MIT** | Permissive | Yes | Yes | Yes | Preserve copyright notice | Include MIT license notice | **Safe** |
| **curl_cffi** | **MIT** | Permissive | Yes | Yes | Yes | Preserve copyright notice | Include MIT license notice | **Safe** |
| **gallery-dl** | **GPL-2.0** | Strong Copyleft | Yes | **Yes ONLY via CLI subprocess** | Separate CLI binary w/ GPL-2.0 source offer | Must release changes to `gallery-dl` under GPL-2.0 if distributed | Provide GPL-2.0 text + source offer for `gallery-dl` | **Strict CLI subprocess boundary** (NEVER `import gallery_dl` in proprietary code) |
| **aria2** | **GPL-2.0** *(w/ OpenSSL note)* | Strong Copyleft | Yes | **Yes ONLY via CLI subprocess** | Separate CLI binary w/ GPL-2.0 source offer | Release changes to `aria2` under GPL-2.0 if distributed | Provide GPL-2.0 text + source offer for `aria2c` | **Strict CLI subprocess boundary** |
| **FFmpeg (LGPL build)** | **LGPL-2.1+** or **LGPL-3.0** | Weak Copyleft | Yes | Yes | Yes (subprocess or dynamic link) | Release FFmpeg mods under LGPL if distributed | Include LGPL text + source offer + IJG JPEG credit (`LICENSE.md`) | **Best for desktop bundling**; invoke via CLI |
| **FFmpeg (GPL build — x264/x265)** | **GPL-2.0+** or **GPL-3.0** | Strong Copyleft | Yes | **Yes via CLI subprocess** | Separate CLI executable only w/ GPL source offer | Release FFmpeg mods under GPL if distributed | Provide GPL text + source offer for FFmpeg & x264/x265 | **Safe server-side via CLI subprocess** (never link `libav*`) |
| **FFmpeg (`--enable-nonfree`)** | **Non-free / Unredistributable** | Proprietary Incompatible | Server-side internal only | Internal server only | **NO (Cannot distribute)** | N/A | **Prohibited to distribute** | **Avoid `--enable-nonfree` completely** |
| **Streamlink** | **BSD-2-Clause** | Permissive | Yes | Yes | Yes | Preserve copyright & disclaimer | Include BSD-2-Clause notice | **Safe** (subprocess preferred for isolation) |
| **Playwright** | **Apache-2.0** | Permissive | Yes | Yes | Yes | State significant changes; preserve NOTICE | Include Apache-2.0 + NOTICE; Chromium carries BSD/LGPL/MIT notices | **Safe** (note: `channel="chrome"` uses Google Chrome EULA — check terms for SaaS vs bundled Chromium) |
| **Puppeteer** | **Apache-2.0** | Permissive | Yes | Yes | Yes | State changes; preserve NOTICE | Include Apache-2.0 + NOTICE | Not selected (use Playwright) |
| **N_m3u8DL-RE** | **MIT** | Permissive | Yes | Yes | Yes | Preserve MIT notice | Include MIT notice | **Safe** (single binary subprocess) |
| **Trafilatura** | **Apache-2.0** | Permissive | Yes | Yes | Yes | State changes; preserve NOTICE | Include Apache-2.0 + NOTICE | **Safe** (direct import or worker) |
| **Mozilla Readability** | **Apache-2.0** | Permissive | Yes | Yes | Yes | State changes; preserve NOTICE | Include Apache-2.0 + NOTICE | **Safe** |
| **GNU Wget** | **GPL-3.0** | Strong Copyleft | Yes | Via subprocess | Separate CLI w/ GPL-3.0 source offer | Release mods under GPL-3.0 | GPL-3.0 text + source offer | Not selected |
| **Wget2 (`libwget` / CLI)** | **LGPL-3.0** (lib) / **GPL-3.0** (CLI) | Weak / Strong Copyleft | Yes | Via subprocess | LGPL-3.0 / GPL-3.0 compliance | Release mods under (L)GPL-3.0 | License text + source offer | Not selected |
| **bgutil-ytdlp-pot-provider** | **GPL-3.0** | Strong Copyleft | Yes | **Yes as separate HTTP sidecar** | Separate container/binary w/ GPL-3.0 | Release mods under GPL-3.0 if distributed | GPL-3.0 text + source offer | **Deploy only as separate HTTP sidecar container** |
| **Lux / ArchiveBox / Grab / MaxDL / OmniFetch / YouTube.js** | **MIT** | Permissive | Yes | Yes | Yes | Preserve MIT notice | Include MIT notice | Reference / optional niche use |
| **Cobalt (`imputnet/cobalt`)** | **AGPL-3.0** | **Network Copyleft** | Yes (open-source only) | **HIGH RISK — Must disclose modified network service source** | **HIGH RISK** | **Must publish full source to remote network users (Sec. 13)** | AGPL-3.0 full source disclosure | **DO NOT EMBED OR FORK** |
| **MeTube (`alexta69/metube`)** | **AGPL-3.0** | **Network Copyleft** | Yes (open-source only) | **HIGH RISK — Network copyleft** | **HIGH RISK** | **Must publish full source to remote network users (Sec. 13)** | AGPL-3.0 full source disclosure | **DO NOT EMBED OR FORK** |

---

## 3. Architectural Rules for Maintaining a Proprietary Commercial Product

Follow these **5 compliance invariants** so our Universal Media Extraction Engine core (Orchestrator, API, Registry, Router, Security Proxy, Frontends, SDKs) can legally remain **100% proprietary**:

1. **Zero AGPL Components Anywhere in the Stack:** Ban `imputnet/cobalt`, `alexta69/metube`, and any AGPL-3.0 dependency from backend containers and CI images (enforce via automated license scanning in CI such as `pip-licenses`, `license-checker`, or `Syft` + `Trivy`).
2. **Strict Process Boundary for All GPL/LGPL Tools:** Never `import gallery_dl`, never `import mutagen`, never link `libavcodec.so` into proprietary code. Always execute `yt-dlp`, `gallery-dl`, `aria2c`, `ffmpeg`, `ffprobe`, and `bgutil-pot-provider` as **separate OS executables or separate containers** communicating strictly via standard CLI arguments, JSON over stdout/stderr, files on scratch storage, or HTTP.
3. **Never Modify GPL Tools Without Compliance Tracking:** Use upstream unmodified releases of `yt-dlp`, `gallery-dl`, `aria2`, and `FFmpeg`. If a bug fix is needed, contribute it upstream or maintain a public fork so source-offer obligations (if ever distributing containers/desktop apps) are trivial.
4. **Separate Server SaaS vs. Desktop/On-Prem Distribution Plans:**
   * **Cloud SaaS (Server-Side Only):** GPLv2/GPLv3/LGPL do *not* trigger source disclosure merely by offering a network service (unlike AGPL). Subprocess isolation keeps your proprietary code completely clean.
   * **Future Desktop App / Self-Hosted Docker Distribution:** Distributing a desktop app or Docker image containing `gallery-dl`, `aria2c`, or GPL `ffmpeg` triggers GPL binary distribution requirements **for those bundled binaries** (not for your proprietary app if separated by process boundary). Prepare an automated **Third-Party Attribution & Source Tarball Bundle** (or let the desktop client download engines on first run / ship an LGPL FFmpeg build).
5. **Maintain an Automated SBOM:** Generate an SPDX/CycloneDX SBOM on every release build (`syft`) and fail CI if any `AGPL-*` or `nonfree` package enters the dependency tree.
