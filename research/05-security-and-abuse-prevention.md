# 05 — Security & Abuse Prevention Architecture

> **Threat Model:** Arbitrary, potentially hostile Internet users submit arbitrary URLs to a multi-tenant service. Any extractor (`yt-dlp`, `gallery-dl`, `aria2c`, `FFmpeg`, `Playwright`) that fetches user-controlled URLs or parses user-supplied media is an **untrusted code/network boundary**.

---

## 1. Core Security Principles

1. **Never rely on string-level URL validation alone.** Attackers trivially defeat URL allow/deny checks using HTTP `301`/`302`/`307` redirects, DNS rebinding (0-second TTL flipping from a public IP to `169.254.169.254`), IPv4-mapped IPv6 addresses (`[::ffff:127.0.0.1]`), octal/decimal IP encodings (`http://2130706433`), or crafted `.m3u8` manifests pointing to internal services.
2. **Never rely on Python socket monkey-patching alone.** As verified in `alexta69/metube`'s `app/url_guard.py`, native C libraries (`curl_cffi`/`libcurl`, `aria2c`, `FFmpeg`, `Chromium`) bypass Python's `socket.getaddrinfo` completely.
3. **Enforce network security at the egress proxy AND OS/container firewall layer.**
4. **Separate Network Extraction from Binary Media Processing.** The container running `FFmpeg` to transcode untrusted media must have **zero network egress**.

---

## 2. Defense-in-Depth Architecture

```mermaid
flowchart LR
    U[Untrusted User URL] --> L1["Layer 1: Ingress Validator\n• http/https only\n• Max 4096 chars, no whitespace/creds\n• Strip fragments, reject IP literals in blocked ranges\n• Rate limit + Auth + Captcha on burst"]
    L1 --> L2["Layer 2: Isolated Extraction Container\n• Non-root UID, Read-only rootfs\n• cap_drop: ALL, seccomp\n• NO direct Internet route (internal net only)\n• HTTP_PROXY / HTTPS_PROXY pinned"]
    L2 --> L3["Layer 3: Safe Egress Proxy (Go/Rust)\n• Ports restricted to 80, 443 ONLY\n• Resolves DNS atomically\n• Rejects ALL non-global IPs (v4 & v6)\n• Dials verified IP directly (Zero DNS Rebinding)\n• Enforces redirect cap & byte/rate limits"]
    L3 --> L4["Layer 4: Host/VPC Egress Firewall\n• nftables / K8s NetworkPolicy\n• Hard DROP to 10/8, 172.16/12, 192.168/16,\n  127/8, 169.254/16, 100.64/10, fc00::/7, fe80::/10"]
    L4 --> INT((Public Internet))
```

---

## 3. Threat-by-Threat Mitigation Blueprint

### 3.1 SSRF, Private IP Blocking & DNS Rebinding
* **Ingress Validation (Fast Reject):**
  * Allow strictly `http://` and `https://` schemes (block `file://`, `ftp://`, `gopher://`, `dict://`, `data://`, `javascript://`, `chrome://`, `sftp://`, `rtmp://` unless explicitly allowlisted).
  * Reject embedded userinfo (`http://user:pass@host`) and non-standard ports (allow only `80`, `443`).
  * Reject known internal hostnames (`localhost`, `*.localhost`, `*.local`, `*.internal`, `metadata.google.internal`, `metadata.azure.com`).
* **Mandatory Safe Egress Proxy (Primary Control — Hardened from MaxDL `safe_proxy.py`):**
  * Built in **Go or Rust** (high concurrency, memory-safe, low overhead) running in its own egress DMZ container.
  * Every extraction engine (`yt-dlp --proxy`, `gallery-dl -o proxy=...`, `aria2c --all-proxy`, `streamlink --http-proxy`, `Playwright --proxy-server`, `httpx`) is forced through this proxy—and extraction containers have **no default gateway to the internet**, making proxy bypass impossible even if an engine has a bug.
  * For every `HTTP` request and `HTTPS CONNECT` tunnel:
    1. Reject any port outside `{80, 443}`.
    2. Resolve DNS once via a hardened upstream resolver (with DNS rebinding protection).
    3. Inspect **every** returned IPv4 and IPv6 address. Unwrap IPv4-mapped IPv6 (`::ffff:a.b.c.d`) before checking!
    4. **Block ALL non-public ranges:**
       * IPv4: `0.0.0.0/8`, `10.0.0.0/8`, `100.64.0.0/10` (CGNAT/Tailscale), `127.0.0.0/8`, `169.254.0.0/16` (AWS/GCP/Azure IMDS), `172.16.0.0/12`, `192.0.0.0/24`, `192.168.0.0/16`, `198.18.0.0/15`, `224.0.0.0/4` (Multicast), `240.0.0.0/4` (Reserved).
       * IPv6: `::/128`, `::1/128`, `fc00::/7` (ULA), `fe80::/10` (Link-local), `ff00::/8` (Multicast), `::ffff:0:0/96` (check embedded IPv4), `64:ff9b::/96` (NAT64).
    5. **Open the TCP connection directly to the validated IP address** (never re-resolve the hostname!). This completely eliminates TOCTOU **DNS rebinding**.
  * **Cloud IMDS Hardening:** Enforce IMDSv2 (hop limit = 1) on AWS / Metadata Concealment on GCP / host firewall blocking `169.254.169.254` as an independent safety net.

### 3.2 Redirect Validation
* Because every hop in an HTTP redirect chain (`301`/`302`/`303`/`307`/`308`) passes through the Safe Egress Proxy, a redirect from `https://safe.example/video` → `http://169.254.169.254/latest/meta-data/` is intercepted and receives `403 Forbidden` at the proxy.
* Enforce a **maximum redirect hop limit** (e.g., 5–8 hops) in `httpx`, `Playwright`, `yt-dlp`, and `aria2c` to block redirect loops.

### 3.3 Arbitrary File Access & Command Injection Prevention
* **Zero Shell Execution:** Always invoke CLI tools via `subprocess.exec` / `argv` arrays (`shell=False`). Pass `--` before user-controlled URLs (`["yt-dlp", ..., "--", url]`) so a URL starting with `-` (e.g., `--exec=...`) is **never** interpreted as a CLI flag!
* **Disable Dangerous Engine Features:**
  * `yt-dlp`: Never pass user input into `--exec`, `--output`, `--config-location`, or `--batch-file`. Explicitly disable config autoloading (`--ignore-config --no-exec`).
  * `gallery-dl`: Pass `--no-mtime --ignore-config` and supply our own generated locked-down JSON config with `postprocessors` disabled for untrusted jobs.
  * `aria2c`: Disable RPC (`--enable-rpc=false`), BitTorrent, and Metalink (`--follow-torrent=false --follow-metalink=false`); explicitly pass `-d /scratch/jobs/<uuid> -o <uuid>.bin`.
* **Filesystem Containment:** Run workers with `read_only: true` root filesystem; mount only a single ephemeral per-job scratch directory with `noexec,nosuid,nodev`.

### 3.4 Malicious Media Files & FFmpeg Hardening
Crafted `.m3u8` playlists, `.mpd` manifests, `.mkv`/`.avi` containers, or polyglot files can exploit FFmpeg for SSRF, Local File Read (e.g., CVE-2016-1897/1898, CVE-2026-107697), or memory corruption / decompression bombs.
* **Zero-Egress Media Workers:** Transcoding/remuxing (`ffmpeg`, `ffprobe`) runs in dedicated containers attached to `network_mode: none` (zero network interfaces besides `lo`). Even a 0-day FFmpeg SSRF cannot exfiltrate a single byte.
* **Strict Protocol Whitelist:** Always pass `-protocol_whitelist file,pipe` **before** `-i` in post-processing workers (explicitly blocking `http`, `https`, `concat`, `subfile`, `crypto`, `ftp`, `gopher`).
* **Manifest Pre-Validation:** When extracting raw `.m3u8`/`.mpd` files, reject playlists containing `file://`, `concat:`, `subfile:`, non-HTTP segment URIs, or excessive segment counts (e.g., >5,000 segments).
* **Decompression Bomb & Resource Limits:** Limit FFmpeg with `-max_muxing_queue_size`, `-fs <max_output_bytes>`, `-t <max_duration>`, Linux `cgroups` memory/CPU caps, and hard wall-clock timeouts.
* **Magic-Byte Verification:** Validate every downloaded file's header bytes before passing to `ffprobe` or serving to users; reject unexpected executables (`ELF`, `PE/MZ`, `Mach-O`, shell scripts) unless explicitly allowed by policy.

### 3.5 Headless Browser Sandboxing (Playwright)
* **Never disable Chromium's sandbox lightly:** Run Playwright workers in containers with a proper Chromium seccomp profile (or user namespaces / gVisor) so Chromium's internal renderer sandbox stays **enabled** (avoid `--no-sandbox` in production whenever possible; if container runtime restricts namespaces, isolate the entire browser pod inside **gVisor (`runsc`)** or **Firecracker / Kata Containers**).
* **Ephemeral Contexts + Process Recycling:** One fresh `BrowserContext` per URL (destroyed immediately after), zero shared cookies/IndexedDB/localStorage across tenants, and full browser process restart every 50 jobs.
* **Strict Route & Permission Lockdown:** Deny geolocation, notifications, microphone, camera, clipboard, downloads inside browser, WebRTC local-IP leaks (`--force-webrtc-ip-handling-policy=disable_non_proxied_udp`), and non-HTTP schemes.

### 3.6 Worker Isolation, Resource Quotas & Abuse Prevention

| Control Dimension | Recommended Production Limit (Default / Anonymous Tier) | Authenticated / Paid Tier | Enforcement Mechanism |
|---|---|---|---|
| **Worker Runtime Isolation** | Non-root container (`UID 10001`), `cap_drop: ALL`, `read_only` rootfs, `no-new-privileges`, `seccomp` (gVisor/Firecracker for browser & FFmpeg pods) | Same | Kubernetes `SecurityContext` / RuntimeClass |
| **CPU & Memory per Job** | 1 vCPU, 1 GiB RAM (Extractor) / 2 GiB (Browser/FFmpeg) | 2 vCPU, 2–4 GiB RAM | Container cgroups + `RLIMIT_AS` / `RLIMIT_CPU` |
| **Job Wall-Clock Timeout** | Probe: 20s · Download: 10m · Live Record: 15m · Browser: 15s | Probe: 30s · Download: 30m · Live: 60m | Async watchdog + `os.killpg(SIGKILL)` |
| **Max Download Size** | 500 MiB – 2 GiB per job | Configurable (e.g., 5–10 GiB) | Pre-flight `Content-Length` + proxy byte counter + `--max-filesize` |
| **Max Playlist / Gallery Items** | 25–50 items per job | 100–300 items | `--playlist-end` / `--range 1-N` |
| **Disk Quota & Floor** | Per-job scratch cap + refuse jobs if free disk < 15% (min 10 GiB) | Same | `statvfs` guard + ephemeral volume limits |
| **Request Rate Limits** | 10 probes/min, 5 downloads/hr, 2 browser fallbacks/hr per IP | Token-bucket per API key / tenant | Redis sliding-window rate limiter + Turnstile on web abuse |
| **Concurrency per User** | 1–2 concurrent downloads | 3–10 concurrent downloads | Redis semaphore per tenant |

### 3.7 Safe Download Delivery (Client Protection)
Serving user-fetched files directly from your main app domain (`app.example.com/files/foo.html` or `.svg`) causes **Stored XSS and session cookie theft**.
1. **Isolated Download Domain:** Serve all downloaded artifacts strictly from a separate, cookieless origin (e.g., `dl-usercontent.example.net` backed by S3/R2 pre-signed URLs).
2. **Mandatory Security Headers on Delivery:**
   * `Content-Disposition: attachment; filename="<sanitized_ascii>"; filename*=UTF-8''<rfc5987>` (forces download, never inline browser execution).
   * `X-Content-Type-Options: nosniff` (prevents MIME sniffing of HTML/JS hidden inside images).
   * `Content-Security-Policy: default-src 'none'; sandbox` (defense-in-depth if opened directly).
   * `Cache-Control: private, max-age=300` (short-lived signed URLs, e.g., 15–60 min expiry).
3. **Optional Malware Scanning:** For direct downloadable archives/documents (`.zip`, `.pdf`, `.exe`) in public-facing products, optionally scan via ClamAV sidecar or block high-risk executable MIME types by default.
