/* Neon Sync — Consumer Product Frontend Controller */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const state = {
    mode: "auto",
    videoQuality: "best",
    h264Mp4: true,
    audioFormat: "mp3",
    audioBitrate: "320k",
    loudnorm: false,
    imageFormat: "original",
    imageMaxWidth: "original",
    clipEnabled: false,
    subsEnabled: false,
    embedSubs: true,
    generateTranscript: true,
    finetuneOpen: false,
    batchMode: false,
    autoSave: localStorage.getItem("neon_autosave") !== "false",
    lockedFormatId: null,
    probeAbort: null,
    probeTimer: null,
    lastProbedUrl: "",
    probeData: null,
    streamsOpen: false,
    jobs: new Map(),
    sse: new Map(),
    autoSavedJobs: new Set(),
  };

  /* ---------- Platform Recognition ---------- */
  const PLATFORM_RULES = [
    { re: /youtu\.?be|youtube\.com/i, label: "YouTube", hint: "video" },
    { re: /instagram\.com/i, label: "Instagram", hint: "auto" },
    { re: /(?:twitter|x|vxtwitter|fxtwitter)\.com/i, label: "X / Twitter", hint: "auto" },
    { re: /tiktok\.com/i, label: "TikTok", hint: "video" },
    { re: /reddit\.com|redd\.it/i, label: "Reddit", hint: "auto" },
    { re: /twitch\.tv/i, label: "Twitch", hint: "video" },
    { re: /kick\.com/i, label: "Kick Live", hint: "video" },
    { re: /soundcloud\.com|bandcamp\.com/i, label: "Audio", hint: "audio" },
    { re: /imgur\.com|pixiv\.net|deviantart\.com|flickr\.com|artstation\.com|pinterest\.com|pin\.it/i, label: "Gallery", hint: "image" },
    { re: /vimeo\.com|dailymotion\.com|bilibili\.com|loom\.com|streamable\.com/i, label: "Video", hint: "video" },
    { re: /\.m3u8(\?|$)/i, label: "HLS Stream", hint: "video" },
    { re: /\.mpd(\?|$)/i, label: "DASH Stream", hint: "video" },
    { re: /\.pdf(\?|$)/i, label: "PDF Document", hint: "auto" },
    { re: /\.(?:zip|tar|gz|7z|iso)(\?|$)/i, label: "Archive", hint: "auto" },
    { re: /\.(?:mp3|m4a|flac|wav|ogg|opus)(\?|$)/i, label: "Direct Audio", hint: "audio" },
    { re: /\.(?:jpg|jpeg|png|webp|gif|avif)(\?|$)/i, label: "Direct Image", hint: "image" },
    { re: /\.(?:mp4|webm|mov|mkv)(\?|$)/i, label: "Direct Video", hint: "video" },
  ];

  function detectPlatform(url) {
    for (const r of PLATFORM_RULES) {
      if (r.re.test(url)) return r;
    }
    try {
      const u = new URL(url);
      return { label: u.hostname.replace(/^www\./, ""), hint: "auto" };
    } catch (_) {
      return null;
    }
  }

  /* ---------- Formatting Helpers ---------- */
  function toast(msg, kind = "") {
    const stack = $("toast-stack");
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    stack.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transition = "opacity .18s";
      setTimeout(() => el.remove(), 200);
    }, 3000);
  }

  function esc(str) {
    return String(str ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function fmtBytes(n) {
    if (n === null || n === undefined) return "";
    const units = ["B", "KB", "MB", "GB"];
    let v = Number(n), i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
  }

  function fmtDuration(sec) {
    if (sec === null || sec === undefined) return "";
    const s = Math.max(0, Math.round(Number(sec)));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
  }

  function parseTime(raw) {
    const s = (raw || "").trim();
    if (!s) return null;
    if (/^\d+(\.\d+)?$/.test(s)) return parseFloat(s);
    const parts = s.split(":").map(Number);
    if (parts.some(Number.isNaN)) return null;
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    return null;
  }

  /* ---------- Mode & Contextual UI ---------- */
  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll("#mode-group .seg").forEach((b) => {
      b.classList.toggle("active", b.dataset.mode === mode);
    });
    $("context-strip-video").classList.toggle("hidden", !(mode === "auto" || mode === "video"));
    $("context-strip-audio").classList.toggle("hidden", mode !== "audio");
    $("context-strip-image").classList.toggle("hidden", mode !== "image");
  }

  function buildPayload(url) {
    const fps = $("fps-limit").value;
    const rangeEnd = $("gallery-range-end").value.trim();
    const langs = $("subtitle-languages").value.split(",").map((x) => x.trim()).filter(Boolean);

    const payload = {
      url,
      mode: state.mode,
      video_quality: state.videoQuality,
      video_container: $("video-container").value,
      video_codec: $("video-codec").value,
      fps_limit: fps && fps !== "any" ? parseInt(fps, 10) : null,
      remux_faststart: true,
      embed_metadata: true,
      embed_thumbnail: false,
      audio_format: state.mode === "audio" ? state.audioFormat : null,
      audio_bitrate: state.mode === "audio" ? state.audioBitrate : "auto",
      normalize_audio: state.loudnorm,
      image_format: state.imageFormat,
      image_quality: parseInt($("image-quality").value, 10),
      image_max_width: state.imageMaxWidth !== "original" ? parseInt(state.imageMaxWidth, 10) : null,
      gallery_archive_format: "zip",
      gallery_range_start: Math.max(1, parseInt($("gallery-range-start").value || "1", 10)),
      gallery_range_end: rangeEnd ? parseInt(rangeEnd, 10) : null,
      extract_subtitles: state.subsEnabled,
      embed_subtitles: state.subsEnabled && state.embedSubs,
      generate_transcript: state.subsEnabled && state.generateTranscript,
      subtitle_mode: "prefer_manual",
      subtitle_format: "vtt",
      subtitle_languages: langs.length ? langs : ["en"],
      clip_start_seconds: state.clipEnabled ? parseTime($("clip-start").value) : null,
      clip_end_seconds: state.clipEnabled ? parseTime($("clip-end").value) : null,
      preferred_engine: $("preferred-engine").value || null,
      custom_filename_prefix: $("filename-prefix").value.trim(),
      playlist_items: $("playlist-items").value.trim() || null,
      max_items: 50,
    };
    if (state.lockedFormatId) payload.format_id = state.lockedFormatId;
    return payload;
  }

  /* ---------- Automatic Background Probe on Paste/Input ---------- */
  function onUrlChanged(triggerProbe = true) {
    const url = $("url-input").value.trim();
    $("clear-url-btn").classList.toggle("hidden", !url);

    if (!url) {
      $("platform-tag").classList.add("hidden");
      $("preview-stage").classList.add("hidden");
      state.lastProbedUrl = "";
      state.probeData = null;
      state.lockedFormatId = null;
      return;
    }

    const plat = detectPlatform(url);
    if (plat) {
      $("platform-tag").textContent = plat.label;
      $("platform-tag").classList.remove("hidden");
    } else {
      $("platform-tag").classList.add("hidden");
    }

    if (!triggerProbe || !/^https?:\/\/[^/\s]+\.[^/\s]+/i.test(url)) return;
    if (url === state.lastProbedUrl) return;

    clearTimeout(state.probeTimer);
    state.probeTimer = setTimeout(() => autoProbeUrl(url), 220);
  }

  async function autoProbeUrl(url) {
    if (state.probeAbort) state.probeAbort.abort();
    const ctrl = new AbortController();
    state.probeAbort = ctrl;
    state.lastProbedUrl = url;
    state.lockedFormatId = null;

    $("preview-stage").classList.remove("hidden");
    $("preview-loading").classList.remove("hidden");
    $("preview-content").classList.add("hidden");

    try {
      const res = await fetch("/v1/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url,
          preferred_engine: $("preferred-engine").value || null,
        }),
        signal: ctrl.signal,
      });
      const data = await res.json();
      if (!res.ok) {
        $("preview-stage").classList.add("hidden");
        return;
      }
      renderLivePreview(data);
    } catch (_) {
      if (!ctrl.signal.aborted) $("preview-stage").classList.add("hidden");
    }
  }

  function renderLivePreview(data) {
    state.probeData = data;
    $("preview-loading").classList.add("hidden");
    $("preview-content").classList.remove("hidden");

    if (data.thumbnail?.url) {
      $("preview-thumb-wrap").classList.remove("hidden");
      $("preview-thumb-img").src = data.thumbnail.url;
      $("preview-dur").textContent = data.is_live ? "LIVE" : fmtDuration(data.duration_seconds);
      $("preview-dur").classList.toggle("hidden", !data.is_live && !data.duration_seconds);
    } else {
      $("preview-thumb-wrap").classList.add("hidden");
    }

    $("preview-title").textContent = data.title || data.url;
    const meta = [
      data.source?.domain,
      data.author?.name,
      data.content_type ? data.content_type.toUpperCase() : null,
      data.size_bytes ? fmtBytes(data.size_bytes) : null,
      (data.subtitles || []).length ? `${data.subtitles.length} caption track(s)` : null,
      (data.items || []).length > 1 ? `${data.items.length} items` : null,
      `via ${data.engine.engine_used}`,
    ].filter(Boolean);
    $("preview-meta").textContent = meta.join(" · ");

    // Wire clip slider if duration is known
    if (data.duration_seconds && data.duration_seconds > 1) {
      const dur = Math.round(data.duration_seconds);
      const slider = $("clip-slider-end");
      slider.max = String(dur);
      slider.value = String(dur);
      slider.classList.remove("hidden");
      $("clip-duration-hint").textContent = `/ ${fmtDuration(dur)}`;
      slider.oninput = () => {
        $("clip-end").value = fmtDuration(slider.value);
        if (!$("clip-start").value) $("clip-start").value = "0:00";
      };
    } else {
      $("clip-slider-end").classList.add("hidden");
      $("clip-duration-hint").textContent = "";
    }

    const fmts = data.available_formats || [];
    const btn = $("toggle-streams-btn");
    if (fmts.length > 1) {
      btn.classList.remove("hidden");
      btn.textContent = `Choose exact stream (${fmts.length} available)`;
      renderStreamsTable(fmts);
    } else {
      btn.classList.add("hidden");
      $("streams-drawer").classList.add("hidden");
    }
    updateLockedBadge();
  }

  function updateLockedBadge() {
    const b = $("locked-stream-badge");
    if (state.lockedFormatId) {
      b.textContent = `Locked stream: ${state.lockedFormatId} (click to clear)`;
      b.classList.remove("hidden");
      b.style.cursor = "pointer";
      b.onclick = () => {
        state.lockedFormatId = null;
        updateLockedBadge();
        if (state.probeData) renderStreamsTable(state.probeData.available_formats || []);
      };
    } else {
      b.classList.add("hidden");
    }
  }

  function renderStreamsTable(fmts) {
    $("streams-tbody").innerHTML = fmts.slice(0, 45).map((f) => `
      <tr class="stream-row ${state.lockedFormatId === f.format_id ? "selected" : ""}" data-id="${esc(f.format_id)}">
        <td class="mono">${esc(f.format_id)}</td>
        <td class="num">${esc(f.resolution || f.label || "—")}</td>
        <td class="mono num">${f.fps ? Math.round(f.fps) : "—"}</td>
        <td class="mono">${esc(f.video_codec || "—")}</td>
        <td class="mono">${esc(f.audio_codec || "—")}</td>
        <td class="mono">${esc(f.container)}</td>
        <td class="mono num">${fmtBytes(f.size_bytes) || "—"}</td>
      </tr>
    `).join("");

    document.querySelectorAll("#streams-tbody tr.stream-row").forEach((tr) => {
      tr.onclick = () => {
        const id = tr.dataset.id;
        state.lockedFormatId = state.lockedFormatId === id ? null : id;
        renderStreamsTable(fmts);
        updateLockedBadge();
      };
    });
  }

  /* ---------- Download & Auto-Save to Device ---------- */
  function triggerBrowserSave(artifactUrl, filename) {
    const a = document.createElement("a");
    a.href = artifactUrl;
    a.download = filename || "download";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 500);
  }

  async function startDownload() {
    const url = $("url-input").value.trim();
    if (!url) return toast("Paste a link first", "err");

    const btn = $("download-btn");
    btn.disabled = true;
    try {
      const res = await fetch("/v1/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildPayload(url)),
      });
      const job = await res.json();
      if (!res.ok) return toast(job.detail?.message || "Could not start download", "err");

      state.jobs.set(job.job_id, job);
      renderDownloads();
      watchJob(job.job_id);
    } catch (err) {
      toast(`Network error: ${err.message}`, "err");
    } finally {
      btn.disabled = false;
    }
  }

  async function startBatchDownload() {
    const urls = $("batch-input").value.split(/\r?\n/).map((u) => u.trim()).filter(Boolean);
    if (!urls.length) return toast("Paste at least one link", "err");

    const base = buildPayload(urls[0]);
    delete base.url;
    delete base.format_id;

    const res = await fetch("/v1/jobs/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls, ...base }),
    });
    const list = await res.json();
    if (!res.ok) return toast(list.detail?.message || "Batch rejected", "err");

    for (const j of list) {
      state.jobs.set(j.job_id, j);
      watchJob(j.job_id);
    }
    renderDownloads();
    toast(`Downloading ${list.length} item(s)…`, "ok");
  }

  function watchJob(jobId) {
    if (state.sse.has(jobId)) return;
    const es = new EventSource(`/v1/jobs/${jobId}/events`);
    state.sse.set(jobId, es);

    es.addEventListener("progress", async (ev) => {
      const p = JSON.parse(ev.data);
      const cur = state.jobs.get(jobId) || { job_id: jobId };
      cur.status = p.status;
      cur.progress = p;
      state.jobs.set(jobId, cur);
      renderDownloads();

      if (["completed", "failed", "cancelled"].includes(p.status)) {
        es.close();
        state.sse.delete(jobId);
        const full = await (await fetch(`/v1/jobs/${jobId}`)).json();
        state.jobs.set(jobId, full);
        renderDownloads();

        const dl = full.result?.download;
        if (p.status === "completed" && dl?.artifact_url) {
          if (state.autoSave && !state.autoSavedJobs.has(jobId)) {
            state.autoSavedJobs.add(jobId);
            triggerBrowserSave(dl.artifact_url, dl.filename);
            toast(`Saved ${dl.filename} to your Downloads`, "ok");
          } else {
            toast(`Ready: ${dl.filename}`, "ok");
          }
        } else if (p.status === "failed") {
          toast(full.error || "Download failed", "err");
        }
      }
    });
    es.onerror = () => { es.close(); state.sse.delete(jobId); };
  }

  function buildInlinePreviewHtml(dl) {
    if (!dl?.artifact_url) return "";
    const mime = (dl.mime_type || "").toLowerCase();
    const inlineUrl = `${dl.artifact_url}${dl.artifact_url.includes("?") ? "&" : "?"}inline=true`;
    if (mime.startsWith("video/")) {
      return `<div class="inline-player"><video controls preload="metadata" src="${esc(inlineUrl)}"></video></div>`;
    }
    if (mime.startsWith("audio/")) {
      return `<div class="inline-player"><audio controls preload="metadata" src="${esc(inlineUrl)}"></audio></div>`;
    }
    if (mime.startsWith("image/") && !mime.includes("svg")) {
      return `<div class="inline-player"><img src="${esc(inlineUrl)}" alt="${esc(dl.filename)}" /></div>`;
    }
    return "";
  }

  function renderDownloads() {
    const arr = Array.from(state.jobs.values()).sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
    $("jobs-summary").textContent = arr.length ? `${arr.length} item${arr.length === 1 ? "" : "s"}` : "";

    const container = $("downloads-list");
    if (!arr.length) {
      container.innerHTML = "";
      return;
    }

    container.innerHTML = arr.slice(0, 15).map((j) => {
      const pct = j.progress?.percent ?? (j.status === "completed" ? 100 : 8);
      const isDone = j.status === "completed";
      const isErr = j.status === "failed" || j.status === "cancelled";
      const isActive = !isDone && !isErr;
      const dl = j.result?.download;
      const tr = j.result?.transcript;
      const ff = j.result?.metadata?.extra?.ffprobe;

      const title = dl?.filename || j.result?.title || j.url;
      const statusBits = [];
      if (isErr) statusBits.push(`<span style="color:var(--err);">${esc(j.error || j.status)}</span>`);
      else if (isDone) statusBits.push(fmtBytes(dl?.size_bytes));
      else statusBits.push(esc(j.progress?.message || j.status));

      if (isActive && j.progress?.speed_bps) statusBits.push(`<span class="mono num">${fmtBytes(j.progress.speed_bps)}/s</span>`);
      if (isActive && j.progress?.eta_seconds) statusBits.push(`<span class="mono num">ETA ${Math.round(j.progress.eta_seconds)}s</span>`);
      if (ff) {
        const codecInfo = [ff.video_codec, ff.audio_codec, ff.width && ff.height ? `${ff.width}×${ff.height}` : null].filter(Boolean).join(" · ");
        if (codecInfo) statusBits.push(`<span class="mono">${esc(codecInfo)}</span>`);
      }

      const actions = [];
      if (dl?.artifact_url) {
        actions.push(`<a class="btn btn-primary btn-sm" href="${esc(dl.artifact_url)}" download="${esc(dl.filename || "download")}">Save File</a>`);
      }
      if (tr?.artifact_url) {
        actions.push(`<a class="btn btn-subtle btn-sm" href="${esc(tr.artifact_url)}">Transcript .txt</a>`);
      }
      if (isActive) {
        actions.push(`<button class="btn btn-danger btn-sm" data-cancel="${esc(j.job_id)}">Cancel</button>`);
      }

      return `
        <div class="dl-card">
          <div class="dl-top">
            <div class="dl-name" title="${esc(j.url)}">${esc(title)}</div>
            <span class="badge ${isDone ? "ok" : isErr ? "err" : "warn"}">${esc(j.status)}</span>
          </div>
          <div class="progress-rail">
            <div class="progress-bar ${isDone ? "done" : isErr ? "err" : ""}" style="width:${Math.min(100, Math.max(4, pct))}%"></div>
          </div>
          <div class="dl-bottom">
            <div class="dl-status-line">${statusBits.filter(Boolean).join(" &middot; ")}</div>
            <div style="display:flex;gap:6px;align-items:center;">${actions.join("")}</div>
          </div>
          ${isDone ? buildInlinePreviewHtml(dl) : ""}
        </div>
      `;
    }).join("");

    container.querySelectorAll("[data-cancel]").forEach((btn) => {
      btn.onclick = async () => {
        const id = btn.dataset.cancel;
        const res = await fetch(`/v1/jobs/${id}/cancel`, { method: "POST" });
        if (res.ok) {
          state.jobs.set(id, await res.json());
          renderDownloads();
        }
      };
    });
  }

  /* ---------- Engine Registry & API Modals ---------- */
  async function loadEngines(refresh = false) {
    const res = await fetch(refresh ? "/v1/engines/refresh" : "/v1/engines", {
      method: refresh ? "POST" : "GET",
    });
    const list = await res.json();
    const installed = list.filter((e) => e.installation_status === "installed").length;
    $("engines-pill-text").textContent = `${installed}/${list.length} engines`;

    $("preferred-engine").innerHTML =
      `<option value="">Auto (Smart Router)</option>` +
      list.filter((e) => e.id !== "ffmpeg").map((e) => `<option value="${esc(e.id)}">${esc(e.id)} (${esc(e.installation_status)})</option>`).join("");

    $("engines-modal-tbody").innerHTML = list.map((e) => {
      const ok = e.installation_status === "installed";
      return `
        <tr>
          <td><strong>${esc(e.id)}</strong></td>
          <td><span class="badge ${ok ? "ok" : "warn"}">${esc(ok ? "ready" : e.installation_status)}</span></td>
          <td class="mono num">${esc(e.version)}</td>
          <td class="mono num">Tier ${e.tier}</td>
          <td style="color:var(--text-secondary);">${esc(e.license)}</td>
        </tr>`;
    }).join("");
    if (refresh) toast(`Engines refreshed (${installed}/${list.length} ready)`, "ok");
  }

  /* ---------- Event Wiring ---------- */
  function bindEvents() {
    const input = $("url-input");
    input.addEventListener("input", () => onUrlChanged(true));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        startDownload();
      }
    });

    // Global Paste Anywhere: pressing Cmd+V / Ctrl+V anywhere on the page pastes into URL bar!
    document.addEventListener("paste", (e) => {
      const active = document.activeElement;
      if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) return;
      const text = (e.clipboardData || window.clipboardData)?.getData("text");
      if (text && /^https?:\/\//i.test(text.trim())) {
        e.preventDefault();
        input.value = text.trim();
        input.focus();
        onUrlChanged(true);
      }
    });

    // Drag & Drop any link onto the window
    window.addEventListener("dragover", (e) => {
      e.preventDefault();
      $("drop-overlay").classList.remove("hidden");
    });
    window.addEventListener("dragleave", (e) => {
      if (e.relatedTarget === null) $("drop-overlay").classList.add("hidden");
    });
    window.addEventListener("drop", (e) => {
      e.preventDefault();
      $("drop-overlay").classList.add("hidden");
      const text = e.dataTransfer?.getData("text/uri-list") || e.dataTransfer?.getData("text/plain");
      if (text && /^https?:\/\//i.test(text.trim())) {
        input.value = text.trim().split(/\r?\n/)[0];
        onUrlChanged(true);
      }
    });

    $("paste-btn").onclick = async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (text) {
          input.value = text.trim();
          onUrlChanged(true);
        }
      } catch (_) {
        input.focus();
        toast("Press ⌘V / Ctrl+V to paste", "");
      }
    };

    $("clear-url-btn").onclick = () => {
      input.value = "";
      onUrlChanged(false);
      input.focus();
    };

    $("download-btn").onclick = startDownload;
    $("download-batch-btn").onclick = startBatchDownload;

    // Mode tabs
    document.querySelectorAll("#mode-group .seg").forEach((b) => {
      b.onclick = () => setMode(b.dataset.mode);
    });

    // Video quality pills
    document.querySelectorAll("#quality-pills [data-vq]").forEach((b) => {
      b.onclick = () => {
        state.videoQuality = b.dataset.vq;
        state.lockedFormatId = null;
        updateLockedBadge();
        document.querySelectorAll("#quality-pills [data-vq]").forEach((x) => x.classList.toggle("active", x === b));
      };
    });

    $("h264-pill").onclick = () => {
      state.h264Mp4 = !state.h264Mp4;
      $("h264-pill").classList.toggle("toggle-on", state.h264Mp4);
      $("video-codec").value = state.h264Mp4 ? "h264" : "auto";
      $("video-container").value = state.h264Mp4 ? "mp4" : "auto";
    };

    // Audio pills
    document.querySelectorAll("#audio-fmt-pills [data-af]").forEach((b) => {
      b.onclick = () => {
        state.audioFormat = b.dataset.af;
        document.querySelectorAll("#audio-fmt-pills [data-af]").forEach((x) => x.classList.toggle("active", x === b));
      };
    });
    document.querySelectorAll("#audio-br-pills [data-ab]").forEach((b) => {
      b.onclick = () => {
        state.audioBitrate = b.dataset.ab;
        document.querySelectorAll("#audio-br-pills [data-ab]").forEach((x) => x.classList.toggle("active", x === b));
      };
    });
    $("loudnorm-pill").onclick = () => {
      state.loudnorm = !state.loudnorm;
      $("loudnorm-pill").classList.toggle("toggle-on", state.loudnorm);
    };

    // Image pills
    document.querySelectorAll("#img-fmt-pills [data-if]").forEach((b) => {
      b.onclick = () => {
        state.imageFormat = b.dataset.if;
        document.querySelectorAll("#img-fmt-pills [data-if]").forEach((x) => x.classList.toggle("active", x === b));
      };
    });
    document.querySelectorAll("#img-width-pills [data-iw]").forEach((b) => {
      b.onclick = () => {
        state.imageMaxWidth = b.dataset.iw;
        document.querySelectorAll("#img-width-pills [data-iw]").forEach((x) => x.classList.toggle("active", x === b));
      };
    });

    // Quick toggles: Trim Clip, Subtitles, Batch, Fine-tune
    $("clip-toggle-btn").onclick = () => {
      state.clipEnabled = !state.clipEnabled;
      $("clip-toggle-btn").classList.toggle("toggle-on", state.clipEnabled);
      $("clip-box").classList.toggle("hidden", !state.clipEnabled);
    };
    $("subs-toggle-btn").onclick = () => {
      state.subsEnabled = !state.subsEnabled;
      $("subs-toggle-btn").classList.toggle("toggle-on", state.subsEnabled);
      $("subs-box").classList.toggle("hidden", !state.subsEnabled);
    };
    $("embed-subs-pill").onclick = () => {
      state.embedSubs = !state.embedSubs;
      $("embed-subs-pill").classList.toggle("toggle-on", state.embedSubs);
    };
    $("transcript-pill").onclick = () => {
      state.generateTranscript = !state.generateTranscript;
      $("transcript-pill").classList.toggle("toggle-on", state.generateTranscript);
    };
    $("batch-toggle-btn").onclick = () => {
      state.batchMode = !state.batchMode;
      $("batch-toggle-btn").classList.toggle("toggle-on", state.batchMode);
      $("single-bar").classList.toggle("hidden", state.batchMode);
      $("batch-bar").classList.toggle("hidden", !state.batchMode);
    };
    $("exit-batch-btn").onclick = () => $("batch-toggle-btn").click();
    $("batch-input").oninput = () => {
      const n = $("batch-input").value.split(/\r?\n/).filter((x) => x.trim()).length;
      $("batch-count").textContent = `${n} link${n === 1 ? "" : "s"}`;
    };
    $("finetune-toggle-btn").onclick = () => {
      state.finetuneOpen = !state.finetuneOpen;
      $("finetune-toggle-btn").classList.toggle("toggle-on", state.finetuneOpen);
      $("finetune-drawer").classList.toggle("hidden", !state.finetuneOpen);
    };
    $("image-quality").oninput = (e) => { $("img-q-label").textContent = `${e.target.value}%`; };

    $("toggle-streams-btn").onclick = () => {
      state.streamsOpen = !state.streamsOpen;
      $("streams-drawer").classList.toggle("hidden", !state.streamsOpen);
    };

    // Header Pills
    function syncAutosaveUi() {
      $("autosave-pill").classList.toggle("active", state.autoSave);
      $("autosave-dot").style.background = state.autoSave ? "var(--ok)" : "var(--text-muted)";
    }
    syncAutosaveUi();
    $("autosave-pill").onclick = () => {
      state.autoSave = !state.autoSave;
      localStorage.setItem("neon_autosave", String(state.autoSave));
      syncAutosaveUi();
      toast(state.autoSave ? "Auto-save to device enabled" : "Auto-save disabled");
    };

    $("engines-modal-btn").onclick = () => $("engines-modal").classList.remove("hidden");
    $("close-engines-modal").onclick = () => $("engines-modal").classList.add("hidden");
    $("refresh-engines-btn").onclick = () => loadEngines(true);

    $("api-modal-btn").onclick = () => {
      const sample = input.value.trim() || "https://www.youtube.com/watch?v=jNQXAC9IVRw";
      const jsonStr = JSON.stringify(buildPayload(sample), null, 2);
      $("api-curl-pre").textContent =
        `curl -X POST "${window.location.origin}/v1/jobs" \\\n` +
        `  -H "Content-Type: application/json" \\\n` +
        `  -d '${jsonStr}'`;
      $("api-modal").classList.remove("hidden");
    };
    $("close-api-modal").onclick = () => $("api-modal").classList.add("hidden");
    $("copy-curl-btn").onclick = async () => {
      await navigator.clipboard.writeText($("api-curl-pre").textContent);
      toast("cURL copied", "ok");
    };
    $("copy-json-btn").onclick = async () => {
      const sample = input.value.trim() || "https://www.youtube.com/watch?v=jNQXAC9IVRw";
      await navigator.clipboard.writeText(JSON.stringify(buildPayload(sample), null, 2));
      toast("JSON copied", "ok");
    };

    // Theme
    function applyTheme(t) {
      document.documentElement.setAttribute("data-theme", t);
      localStorage.setItem("neon_sync_theme", t);
      $("theme-text").textContent = t === "dark" ? "Light" : "Dark";
    }
    applyTheme(localStorage.getItem("neon_sync_theme") || "dark");
    $("theme-btn").onclick = () => {
      const cur = document.documentElement.getAttribute("data-theme") || "dark";
      applyTheme(cur === "dark" ? "light" : "dark");
    };
  }

  /* ---------- Init ---------- */
  (async () => {
    bindEvents();
    await loadEngines(false);
    try {
      const existing = await (await fetch("/v1/jobs")).json();
      for (const j of existing) {
        state.jobs.set(j.job_id, j);
        state.autoSavedJobs.add(j.job_id); // Don't auto-download old historical jobs on page load
      }
      renderDownloads();
    } catch (_) {}
  })();
})();
