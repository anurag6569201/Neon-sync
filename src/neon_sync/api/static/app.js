/* Neon Sync Studio — Frontend Controller (Zero external build dependencies) */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const state = {
    view: "extract",
    batchMode: false,
    drawerOpen: false,
    catalog: null,
    engines: [],
    jobs: new Map(),
    sse: new Map(),
    probeData: null,
    fmtFilter: "all",
    queueFilter: "all",
    selectedFormatId: null,
    selectedFormatLabel: null,
    activePresetId: null,
    expandedJsonJobs: new Set(),
  };

  /* ---------- Utilities ---------- */
  function toast(message, kind = "info") {
    const stack = $("toast-stack");
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = message;
    stack.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transition = "opacity .18s ease";
      setTimeout(() => el.remove(), 200);
    }, 3200);
  }

  function esc(str) {
    return String(str ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function fmtBytes(n) {
    if (n === null || n === undefined) return "—";
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

  function fillSelect(id, options, defaultVal) {
    const el = $(id);
    el.innerHTML = options.map((o) => `<option value="${esc(o.id)}">${esc(o.label)}</option>`).join("");
    if (defaultVal !== undefined) el.value = defaultVal;
  }

  /* ---------- Navigation & Theme ---------- */
  const VIEW_TITLES = {
    extract: "Extract",
    queue: "Queue & History",
    engines: "Engine Registry",
    api: "API & Live Payload",
  };

  function setView(viewName) {
    state.view = viewName;
    $("topbar-title").textContent = VIEW_TITLES[viewName] || "Neon Sync";
    document.querySelectorAll(".nav-item[data-view]").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === viewName);
    });
    document.querySelectorAll(".view").forEach((v) => {
      v.classList.toggle("active", v.id === `view-${viewName}`);
    });
    if (viewName === "api") renderApiPreview();
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("neon_sync_theme", theme);
    $("theme-label").textContent = theme === "dark" ? "Light" : "Dark";
  }

  /* ---------- Settings & Presets ---------- */
  function collectOptions() {
    const fps = $("fps-limit").value;
    const imgW = $("image-max-width").value;
    const rangeEnd = $("gallery-range-end").value.trim();
    const liveMax = $("live-max-dur").value.trim();
    const langs = $("subtitle-languages").value.split(",").map((x) => x.trim()).filter(Boolean);

    const opts = {
      mode: $("mode-select").value,
      video_quality: $("video-quality").value,
      video_container: $("video-container").value,
      video_codec: $("video-codec").value,
      fps_limit: fps && fps !== "any" ? parseInt(fps, 10) : null,
      remux_faststart: $("remux-faststart").checked,
      embed_metadata: $("embed-metadata").checked,
      embed_thumbnail: $("embed-thumbnail").checked,
      audio_format: $("audio-format").value !== "best" ? $("audio-format").value : null,
      audio_bitrate: $("audio-bitrate").value,
      normalize_audio: $("normalize-audio").checked,
      image_format: $("image-format").value,
      image_quality: parseInt($("image-quality").value, 10),
      image_max_width: imgW && imgW !== "original" ? parseInt(imgW, 10) : null,
      gallery_archive_format: $("gallery-archive-format").value,
      gallery_range_start: Math.max(1, parseInt($("gallery-range-start").value || "1", 10)),
      gallery_range_end: rangeEnd ? parseInt(rangeEnd, 10) : null,
      extract_subtitles: $("extract-subtitles").checked,
      embed_subtitles: $("embed-subtitles").checked,
      generate_transcript: $("generate-transcript").checked,
      subtitle_mode: $("subtitle-mode").value,
      subtitle_format: $("subtitle-format").value,
      subtitle_languages: langs.length ? langs : ["en"],
      clip_start_seconds: parseTime($("clip-start").value),
      clip_end_seconds: parseTime($("clip-end").value),
      preferred_engine: $("preferred-engine").value || null,
      custom_filename_prefix: $("filename-prefix").value.trim(),
      playlist_items: $("playlist-items").value.trim() || null,
      max_items: Math.max(1, parseInt($("max-items").value || "50", 10)),
      livestream_max_duration: liveMax ? parseInt(liveMax, 10) : null,
      allow_browser_fallback: $("browser-fallback").checked,
    };
    if (state.selectedFormatId) opts.format_id = state.selectedFormatId;
    return opts;
  }

  function renderActiveChips() {
    const o = collectOptions();
    const chips = [];
    if (state.selectedFormatId) {
      chips.push(`<span class="chip accent mono">format: ${esc(state.selectedFormatId)}</span>`);
    }
    chips.push(`<span class="chip">Mode: ${esc(o.mode)}</span>`);
    if (o.mode !== "audio" && o.mode !== "image" && o.mode !== "direct") {
      if (o.video_quality !== "best") chips.push(`<span class="chip">${esc(o.video_quality)}p</span>`);
      if (o.video_codec !== "auto") chips.push(`<span class="chip">${esc(o.video_codec.toUpperCase())}</span>`);
      if (o.video_container !== "auto") chips.push(`<span class="chip">${esc(o.video_container.toUpperCase())}</span>`);
      if (o.fps_limit) chips.push(`<span class="chip">${o.fps_limit}fps</span>`);
    }
    if (o.mode === "audio" || o.audio_format) {
      if (o.audio_format) chips.push(`<span class="chip">${esc(o.audio_format.toUpperCase())}</span>`);
      if (o.audio_bitrate !== "auto") chips.push(`<span class="chip">${esc(o.audio_bitrate)}</span>`);
      if (o.normalize_audio) chips.push(`<span class="chip">Loudnorm</span>`);
    }
    if (o.image_format !== "original") chips.push(`<span class="chip">Img: ${esc(o.image_format.toUpperCase())} Q${o.image_quality}</span>`);
    if (o.image_max_width) chips.push(`<span class="chip">Max ${o.image_max_width}px</span>`);
    if (o.extract_subtitles) chips.push(`<span class="chip">Subs (${esc(o.subtitle_languages.join(","))})</span>`);
    if (o.generate_transcript) chips.push(`<span class="chip">Transcript .txt</span>`);
    if (o.clip_start_seconds !== null) {
      chips.push(`<span class="chip">Clip ${fmtDuration(o.clip_start_seconds)}–${o.clip_end_seconds !== null ? fmtDuration(o.clip_end_seconds) : "end"}</span>`);
    }
    if (o.preferred_engine) chips.push(`<span class="chip accent">Engine: ${esc(o.preferred_engine)}</span>`);
    if (o.allow_browser_fallback) chips.push(`<span class="chip">Tier-3 Browser</span>`);
    if (o.custom_filename_prefix) chips.push(`<span class="chip mono">prefix:${esc(o.custom_filename_prefix)}</span>`);

    $("active-chips").innerHTML = chips.join("");
    if (state.view === "api") renderApiPreview();
  }

  function resetDefaults(silent = false) {
    $("mode-select").value = "auto";
    $("video-quality").value = "best";
    $("video-container").value = "auto";
    $("video-codec").value = "auto";
    $("fps-limit").value = "any";
    $("remux-faststart").checked = true;
    $("embed-metadata").checked = true;
    $("embed-thumbnail").checked = false;
    $("audio-format").value = "best";
    $("audio-bitrate").value = "auto";
    $("normalize-audio").checked = false;
    $("image-format").value = "original";
    $("image-quality").value = "90";
    $("image-quality-val").textContent = "90%";
    $("image-max-width").value = "original";
    $("gallery-archive-format").value = "zip";
    $("gallery-range-start").value = "1";
    $("gallery-range-end").value = "";
    $("extract-subtitles").checked = false;
    $("embed-subtitles").checked = false;
    $("generate-transcript").checked = false;
    $("subtitle-mode").value = "prefer_manual";
    $("subtitle-format").value = "vtt";
    $("subtitle-languages").value = "en";
    $("clip-start").value = "";
    $("clip-end").value = "";
    $("preferred-engine").value = "";
    $("filename-prefix").value = "";
    $("playlist-items").value = "";
    $("max-items").value = "50";
    $("live-max-dur").value = "";
    $("browser-fallback").checked = false;
    state.selectedFormatId = null;
    state.selectedFormatLabel = null;
    state.activePresetId = null;
    updatePresetHighlight();
    renderProbeFormats();
    renderActiveChips();
    if (!silent) toast("Options reset to defaults");
  }

  function updatePresetHighlight() {
    document.querySelectorAll(".preset-chip").forEach((b) => {
      b.classList.toggle("active", b.dataset.presetId === state.activePresetId);
    });
  }

  function applyPreset(preset) {
    resetDefaults(true);
    const s = preset.settings || {};
    if (s.mode) $("mode-select").value = s.mode;
    if (s.video_quality) $("video-quality").value = s.video_quality;
    if (s.video_container) $("video-container").value = s.video_container;
    if (s.video_codec) $("video-codec").value = s.video_codec;
    if (s.fps_limit) $("fps-limit").value = String(s.fps_limit);
    if (s.remux_faststart !== undefined) $("remux-faststart").checked = s.remux_faststart;
    if (s.embed_metadata !== undefined) $("embed-metadata").checked = s.embed_metadata;
    if (s.embed_thumbnail !== undefined) $("embed-thumbnail").checked = s.embed_thumbnail;
    if (s.audio_format) $("audio-format").value = s.audio_format;
    if (s.audio_bitrate) $("audio-bitrate").value = s.audio_bitrate;
    if (s.normalize_audio !== undefined) $("normalize-audio").checked = s.normalize_audio;
    if (s.image_format) $("image-format").value = s.image_format;
    if (s.image_quality !== undefined) {
      $("image-quality").value = String(s.image_quality);
      $("image-quality-val").textContent = `${s.image_quality}%`;
    }
    if (s.image_max_width) $("image-max-width").value = String(s.image_max_width);
    if (s.gallery_archive_format) $("gallery-archive-format").value = s.gallery_archive_format;
    if (s.extract_subtitles !== undefined) $("extract-subtitles").checked = s.extract_subtitles;
    if (s.embed_subtitles !== undefined) $("embed-subtitles").checked = s.embed_subtitles;

    state.activePresetId = preset.id;
    updatePresetHighlight();
    renderActiveChips();
    toast(`Preset applied: ${preset.name}`, "ok");
  }

  /* ---------- API Preview ---------- */
  function buildLivePayload() {
    const sampleUrl = $("url-input").value.trim() || "https://www.youtube.com/watch?v=jNQXAC9IVRw";
    return { url: sampleUrl, ...collectOptions() };
  }

  function renderApiPreview() {
    const payload = buildLivePayload();
    const origin = window.location.origin;
    const jsonStr = JSON.stringify(payload, null, 2);
    $("live-curl-preview").textContent =
      `curl -X POST "${origin}/v1/jobs" \\\n` +
      `  -H "Content-Type: application/json" \\\n` +
      `  -d '${jsonStr}'`;
  }

  /* ---------- Data Fetching ---------- */
  async function loadCatalog() {
    const res = await fetch("/v1/formats");
    state.catalog = await res.json();
    fillSelect("mode-select", state.catalog.modes, "auto");
    fillSelect("video-quality", state.catalog.video_qualities, "best");
    fillSelect("video-container", state.catalog.video_containers, "auto");
    fillSelect("video-codec", state.catalog.video_codecs, "auto");
    fillSelect("fps-limit", state.catalog.fps_limits, "any");
    fillSelect("audio-format", state.catalog.audio_formats, "best");
    fillSelect("audio-bitrate", state.catalog.audio_bitrates, "auto");
    fillSelect("image-format", state.catalog.image_formats, "original");
    fillSelect("image-max-width", state.catalog.image_max_widths, "original");
    fillSelect("gallery-archive-format", state.catalog.gallery_archive_formats, "zip");
    fillSelect("subtitle-mode", state.catalog.subtitle_modes, "prefer_manual");
    fillSelect("subtitle-format", state.catalog.subtitle_formats, "vtt");

    $("presets-row").innerHTML = state.catalog.presets.map(
      (p) => `<button class="preset-chip" data-preset-id="${esc(p.id)}" title="${esc(p.description)}">${esc(p.name)}</button>`
    ).join("");

    document.querySelectorAll(".preset-chip").forEach((btn) => {
      btn.onclick = () => {
        const preset = state.catalog.presets.find((p) => p.id === btn.dataset.presetId);
        if (preset) applyPreset(preset);
      };
    });
    renderActiveChips();
  }

  async function loadEngines(refresh = false) {
    try {
      const res = await fetch(refresh ? "/v1/engines/refresh" : "/v1/engines", {
        method: refresh ? "POST" : "GET",
      });
      state.engines = await res.json();
      const installed = state.engines.filter((e) => e.installation_status === "installed").length;
      const total = state.engines.length;

      $("nav-engine-count").textContent = `${installed}/${total}`;
      $("engines-ready-badge").textContent = `${installed} / ${total} installed`;
      $("sidebar-status-text").textContent = `${installed}/${total} engines ready`;
      $("sidebar-status-dot").className = `status-dot ${installed > 0 ? "" : "err"}`;

      const cur = $("preferred-engine").value;
      $("preferred-engine").innerHTML =
        `<option value="">Auto (Smart Router)</option>` +
        state.engines
          .filter((e) => e.id !== "ffmpeg")
          .map((e) => `<option value="${esc(e.id)}">${esc(e.id)} (${esc(e.installation_status)})</option>`)
          .join("");
      $("preferred-engine").value = cur;

      $("engines-tbody").innerHTML = state.engines.map((e) => {
        const ok = e.installation_status === "installed";
        const caps = (e.capabilities || []).slice(0, 4).map((c) => c.replace(/_/g, " ")).join(", ");
        return `
          <tr>
            <td><strong>${esc(e.id)}</strong><div style="font-size:11px;color:var(--text-muted);">${esc(e.name)}</div></td>
            <td class="mono num">T${e.tier}</td>
            <td><span class="badge ${ok ? "ok" : "warn"}">${esc(ok ? "ready" : e.installation_status)}</span></td>
            <td class="mono num">${esc(e.version)}</td>
            <td class="mono num">${e.priority}</td>
            <td class="mono">${esc(e.invocation_mode)}</td>
            <td style="font-size:11.5px;color:var(--text-secondary);">${esc(e.license)}</td>
            <td style="font-size:11.5px;color:var(--text-muted);">${esc(caps)}${(e.capabilities || []).length > 4 ? "…" : ""}</td>
          </tr>`;
      }).join("");

      if (refresh) toast(`Engine health refreshed (${installed}/${total} ready)`, "ok");
    } catch (err) {
      $("sidebar-status-dot").className = "status-dot err";
      $("sidebar-status-text").textContent = "API offline";
      toast(`Failed to reach API: ${err.message}`, "err");
    }
  }

  /* ---------- Probe Inspector ---------- */
  function renderProbeFormats() {
    const data = state.probeData;
    if (!data || !(data.available_formats || []).length) {
      $("probe-formats-section").classList.add("hidden");
      return;
    }
    $("probe-formats-section").classList.remove("hidden");
    $("clear-fmt-lock-btn").classList.toggle("hidden", !state.selectedFormatId);

    const all = data.available_formats;
    const filtered = all.filter((f) => {
      if (state.fmtFilter === "muxed") return f.has_video && f.has_audio;
      if (state.fmtFilter === "video") return f.has_video && !f.has_audio;
      if (state.fmtFilter === "audio") return !f.has_video && f.has_audio;
      return true;
    });

    const list = filtered.length ? filtered : all;
    $("probe-formats-tbody").innerHTML = list.slice(0, 50).map((f) => `
      <tr class="clickable ${state.selectedFormatId === f.format_id ? "selected" : ""}" data-fmt-id="${esc(f.format_id)}" data-fmt-label="${esc(f.resolution || f.label || f.container)}">
        <td class="mono">${esc(f.format_id)}</td>
        <td class="num">${esc(f.resolution || f.label || "—")}</td>
        <td class="mono num">${f.fps ? Math.round(f.fps) : "—"}</td>
        <td class="mono">${esc(f.video_codec || "—")}</td>
        <td class="mono">${esc(f.audio_codec || "—")}</td>
        <td class="mono">${esc(f.container)}</td>
        <td class="mono num">${fmtBytes(f.size_bytes)}</td>
      </tr>
    `).join("");

    document.querySelectorAll("#probe-formats-tbody tr.clickable").forEach((row) => {
      row.onclick = () => {
        const id = row.dataset.fmtId;
        if (state.selectedFormatId === id) {
          state.selectedFormatId = null;
          state.selectedFormatLabel = null;
        } else {
          state.selectedFormatId = id;
          state.selectedFormatLabel = row.dataset.fmtLabel;
          toast(`Locked format ${id}`, "ok");
        }
        renderProbeFormats();
        renderActiveChips();
      };
    });
  }

  function renderProbe(data) {
    state.probeData = data;
    state.selectedFormatId = null;
    $("probe-panel").classList.remove("hidden");
    $("probe-header-badges").innerHTML = `
      <span class="badge ok">${esc(data.content_type)}</span>
      ${data.is_live ? '<span class="badge err">LIVE</span>' : ""}
      <span class="badge accent mono">${esc(data.engine.engine_used)} · ${data.engine.probe_duration_ms ?? 0}ms</span>
    `;

    const thumbHtml = data.thumbnail?.url
      ? `<div class="thumb-wrap"><img src="${esc(data.thumbnail.url)}" alt="Thumbnail" />${data.duration_seconds ? `<span class="duration-pill">${fmtDuration(data.duration_seconds)}</span>` : ""}</div>`
      : "";

    const subs = (data.subtitles || []).slice(0, 10).map((s) => `${s.language}${s.is_auto_generated ? "*" : ""}`);
    const metaBits = [
      data.source?.domain,
      data.author?.name,
      data.size_bytes ? fmtBytes(data.size_bytes) : null,
      (data.items || []).length ? `${data.items.length} items` : null,
    ].filter(Boolean);

    $("probe-summary").innerHTML = `
      ${thumbHtml}
      <div class="probe-meta">
        <div class="probe-title">${esc(data.title || data.url)}</div>
        <div class="probe-sub num">${esc(metaBits.join(" · "))}</div>
        ${subs.length ? `<div class="mono" style="color:var(--text-muted);margin-bottom:8px;">Subtitles: ${esc(subs.join(", "))}</div>` : ""}
        ${(data.engine.fallback_chain || []).length > 1 ? `<div class="mono" style="color:var(--text-muted);">Fallback chain: ${esc(data.engine.fallback_chain.join(" -> "))}</div>` : ""}
      </div>
    `;
    renderProbeFormats();
    renderActiveChips();
  }

  /* ---------- Jobs & SSE ---------- */
  function renderJobCard(j) {
    const pct = j.progress?.percent ?? (j.status === "completed" ? 100 : 0);
    const isActive = ["queued", "probing", "downloading", "post_processing", "uploading"].includes(j.status);
    const badgeCls = j.status === "completed" ? "ok" : (j.status === "failed" || j.status === "cancelled") ? "err" : "warn";
    const fillCls = j.status === "completed" ? "done" : (j.status === "failed" || j.status === "cancelled") ? "failed" : "";
    const dl = j.result?.download;
    const tr = j.result?.transcript;
    const ff = j.result?.metadata?.extra?.ffprobe;

    const metaParts = [];
    if (j.error) metaParts.push(`<span style="color:var(--err);">${esc(j.error)}</span>`);
    else if (j.progress?.message) metaParts.push(esc(j.progress.message));
    if (isActive && j.progress?.speed_bps) metaParts.push(`<span class="mono num">${fmtBytes(j.progress.speed_bps)}/s</span>`);
    if (isActive && j.progress?.eta_seconds) metaParts.push(`<span class="mono num">ETA ${Math.round(j.progress.eta_seconds)}s</span>`);
    if (ff) {
      const specs = [ff.video_codec, ff.audio_codec, ff.width && ff.height ? `${ff.width}x${ff.height}` : null].filter(Boolean);
      if (specs.length) metaParts.push(`<span class="badge mono">${esc(specs.join(" · "))}</span>`);
    }
    if (dl?.sha256) {
      metaParts.push(`<button class="icon-btn mono" data-copy-sha="${esc(dl.sha256)}" title="Copy SHA-256">sha256:${esc(dl.sha256.slice(0, 10))}</button>`);
    }

    const actions = [];
    if (dl?.artifact_url) {
      actions.push(`<a class="btn btn-primary btn-sm" href="${esc(dl.artifact_url)}">Download (${fmtBytes(dl.size_bytes)})</a>`);
    }
    if (tr?.artifact_url) {
      actions.push(`<a class="btn btn-secondary btn-sm" href="${esc(tr.artifact_url)}">Transcript</a>`);
    }
    if (isActive) {
      actions.push(`<button class="btn btn-danger-ghost btn-sm" data-cancel-job="${esc(j.job_id)}">Cancel</button>`);
    }
    if (j.result) {
      const open = state.expandedJsonJobs.has(j.job_id);
      actions.push(`<button class="btn btn-ghost btn-sm" data-toggle-json="${esc(j.job_id)}">${open ? "Hide JSON" : "JSON"}</button>`);
    }

    const jsonViewer = (j.result && state.expandedJsonJobs.has(j.job_id))
      ? `<pre class="code-block" style="margin-top:10px;max-height:260px;">${esc(JSON.stringify(j.result, null, 2))}</pre>`
      : "";

    const title = j.result?.title || dl?.filename || j.url;
    return `
      <div class="job-row">
        <div class="job-top">
          <div class="job-title" title="${esc(j.url)}">${esc(title)}</div>
          <div style="display:flex;gap:6px;align-items:center;flex-shrink:0;">
            ${j.result?.engine?.engine_used ? `<span class="badge mono">${esc(j.result.engine.engine_used)}</span>` : ""}
            <span class="badge ${badgeCls}">${esc(j.status)}</span>
          </div>
        </div>
        <div class="job-progress-track">
          <div class="job-progress-fill ${fillCls}" style="width:${Math.min(100, Math.max(0, pct))}%"></div>
        </div>
        <div class="job-bottom">
          <div class="job-meta-line">${metaParts.join(" <span>&middot;</span> ")}</div>
          <div style="display:flex;gap:6px;align-items:center;">${actions.join("")}</div>
        </div>
        ${jsonViewer}
      </div>
    `;
  }

  function bindJobCardActions(container) {
    container.querySelectorAll("[data-cancel-job]").forEach((btn) => {
      btn.onclick = async () => {
        const id = btn.dataset.cancelJob;
        const res = await fetch(`/v1/jobs/${id}/cancel`, { method: "POST" });
        if (res.ok) {
          state.jobs.set(id, await res.json());
          renderJobs();
          toast("Job cancelled");
        }
      };
    });
    container.querySelectorAll("[data-copy-sha]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await navigator.clipboard.writeText(btn.dataset.copySha);
          toast("SHA-256 copied to clipboard", "ok");
        } catch (_) {}
      };
    });
    container.querySelectorAll("[data-toggle-json]").forEach((btn) => {
      btn.onclick = () => {
        const id = btn.dataset.toggleJson;
        if (state.expandedJsonJobs.has(id)) state.expandedJsonJobs.delete(id);
        else state.expandedJsonJobs.add(id);
        renderJobs();
      };
    });
  }

  function renderJobs() {
    const all = Array.from(state.jobs.values()).sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
    $("nav-job-count").textContent = String(all.length);
    $("extract-job-count").textContent = String(all.length);

    const recent = all.slice(0, 6);
    const extractEl = $("extract-jobs-list");
    extractEl.innerHTML = recent.length
      ? recent.map(renderJobCard).join("")
      : `<div class="empty-state">No jobs yet. Paste a URL above and press <span class="kbd">⌘↵</span> to extract.</div>`;
    bindJobCardActions(extractEl);

    const filtered = all.filter((j) => {
      if (state.queueFilter === "active") return !["completed", "failed", "cancelled"].includes(j.status);
      if (state.queueFilter === "completed") return j.status === "completed";
      if (state.queueFilter === "failed") return j.status === "failed" || j.status === "cancelled";
      return true;
    });
    const fullEl = $("full-jobs-list");
    fullEl.innerHTML = filtered.length
      ? filtered.map(renderJobCard).join("")
      : `<div class="empty-state">No matching jobs in this filter.</div>`;
    bindJobCardActions(fullEl);
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
      renderJobs();
      if (["completed", "failed", "cancelled"].includes(p.status)) {
        es.close();
        state.sse.delete(jobId);
        const full = await (await fetch(`/v1/jobs/${jobId}`)).json();
        state.jobs.set(jobId, full);
        renderJobs();
        if (p.status === "completed") toast(`Ready: ${full.result?.download?.filename || "Artifact"}`, "ok");
        else if (p.status === "failed") toast(`Failed: ${full.error || "Extraction error"}`, "err");
      }
    });
    es.onerror = () => { es.close(); state.sse.delete(jobId); };
  }

  /* ---------- Actions ---------- */
  async function runProbe() {
    const url = $("url-input").value.trim();
    if (!url) return toast("Paste a URL first", "err");
    const btn = $("probe-btn");
    btn.disabled = true;
    btn.textContent = "Probing…";
    try {
      const opts = collectOptions();
      const res = await fetch("/v1/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url,
          preferred_engine: opts.preferred_engine,
          max_items: opts.max_items,
          allow_browser_fallback: opts.allow_browser_fallback,
        }),
      });
      const data = await res.json();
      if (!res.ok) return toast(data.detail?.message || "Probe failed", "err");
      renderProbe(data);
      toast(`Probed via ${data.engine.engine_used} (${data.available_formats.length} formats)`, "ok");
    } catch (err) {
      toast(`Probe failed: ${err.message}`, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "Probe";
    }
  }

  async function runExtract() {
    const url = $("url-input").value.trim();
    if (!url) return toast("Paste a URL first", "err");
    const btn = $("extract-btn");
    btn.disabled = true;
    try {
      const payload = { url, ...collectOptions() };
      const res = await fetch("/v1/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) return toast(data.detail?.message || "Failed to queue job", "err");
      state.jobs.set(data.job_id, data);
      renderJobs();
      watchJob(data.job_id);
      toast("Extraction queued", "ok");
    } catch (err) {
      toast(`Request failed: ${err.message}`, "err");
    } finally {
      btn.disabled = false;
    }
  }

  async function runBatch() {
    const urls = $("batch-input").value.split(/\r?\n/).map((u) => u.trim()).filter(Boolean);
    if (!urls.length) return toast("Enter at least one URL", "err");
    const opts = collectOptions();
    delete opts.format_id;
    const res = await fetch("/v1/jobs/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls, ...opts }),
    });
    const data = await res.json();
    if (!res.ok) return toast(data.detail?.message || "Batch rejected", "err");
    for (const j of data) {
      state.jobs.set(j.job_id, j);
      watchJob(j.job_id);
    }
    renderJobs();
    toast(`Queued ${data.length} extraction job(s)`, "ok");
  }

  /* ---------- Event Wiring ---------- */
  function bindEvents() {
    document.querySelectorAll(".nav-item[data-view]").forEach((btn) => {
      btn.onclick = () => setView(btn.dataset.view);
    });
    document.querySelectorAll("[data-jump]").forEach((btn) => {
      btn.onclick = () => setView(btn.dataset.jump);
    });

    $("theme-toggle").onclick = () => {
      const cur = document.documentElement.getAttribute("data-theme") || "dark";
      applyTheme(cur === "dark" ? "light" : "dark");
    };

    $("mode-single-btn").onclick = () => {
      state.batchMode = false;
      $("mode-single-btn").classList.add("active");
      $("mode-batch-btn").classList.remove("active");
      $("single-input-wrap").classList.remove("hidden");
      $("batch-input-wrap").classList.add("hidden");
    };
    $("mode-batch-btn").onclick = () => {
      state.batchMode = true;
      $("mode-batch-btn").classList.add("active");
      $("mode-single-btn").classList.remove("active");
      $("single-input-wrap").classList.add("hidden");
      $("batch-input-wrap").classList.remove("hidden");
    };

    $("batch-input").oninput = () => {
      const n = $("batch-input").value.split(/\r?\n/).filter((l) => l.trim()).length;
      $("batch-line-count").textContent = `${n} URL${n === 1 ? "" : "s"}`;
    };

    $("toggle-settings-btn").onclick = () => {
      state.drawerOpen = !state.drawerOpen;
      $("settings-drawer").classList.toggle("hidden", !state.drawerOpen);
      $("toggle-settings-label").textContent = state.drawerOpen ? "Hide Options" : "Configure Options";
    };

    document.querySelectorAll("#settings-tabs .tab-btn").forEach((btn) => {
      btn.onclick = () => {
        document.querySelectorAll("#settings-tabs .tab-btn").forEach((b) => b.classList.remove("active"));
        document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        $(`tab-${btn.dataset.tab}`).classList.add("active");
      };
    });

    $("settings-drawer").addEventListener("change", () => {
      state.activePresetId = null;
      updatePresetHighlight();
      renderActiveChips();
    });
    $("image-quality").oninput = (e) => {
      $("image-quality-val").textContent = `${e.target.value}%`;
      renderActiveChips();
    };
    $("url-input").oninput = () => { if (state.view === "api") renderApiPreview(); };

    $("paste-btn").onclick = async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (text) { $("url-input").value = text.trim(); $("url-input").focus(); }
      } catch (_) {
        toast("Clipboard access denied by browser", "err");
      }
    };

    $("probe-btn").onclick = runProbe;
    $("extract-btn").onclick = runExtract;
    $("batch-submit-btn").onclick = runBatch;
    $("reset-btn").onclick = () => resetDefaults(false);
    $("close-probe-btn").onclick = () => $("probe-panel").classList.add("hidden");
    $("clear-fmt-lock-btn").onclick = () => {
      state.selectedFormatId = null;
      state.selectedFormatLabel = null;
      renderProbeFormats();
      renderActiveChips();
    };

    document.querySelectorAll("#fmt-filter-group .seg-btn").forEach((b) => {
      b.onclick = () => {
        state.fmtFilter = b.dataset.fmtFilter;
        document.querySelectorAll("#fmt-filter-group .seg-btn").forEach((x) => x.classList.toggle("active", x === b));
        renderProbeFormats();
      };
    });

    document.querySelectorAll("#queue-filter-group .seg-btn").forEach((b) => {
      b.onclick = () => {
        state.queueFilter = b.dataset.qfilter;
        document.querySelectorAll("#queue-filter-group .seg-btn").forEach((x) => x.classList.toggle("active", x === b));
        renderJobs();
      };
    });

    $("sidebar-refresh").onclick = () => loadEngines(true);
    $("engines-refresh-btn").onclick = () => loadEngines(true);

    $("copy-curl-btn").onclick = async () => {
      await navigator.clipboard.writeText($("live-curl-preview").textContent);
      toast("cURL command copied", "ok");
    };
    $("copy-json-btn").onclick = async () => {
      await navigator.clipboard.writeText(JSON.stringify(buildLivePayload(), null, 2));
      toast("JSON payload copied", "ok");
    };

    // Keyboard shortcuts: Cmd/Ctrl+K focuses URL, Cmd/Ctrl+Enter starts extraction
    document.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setView("extract");
        $("url-input").focus();
        $("url-input").select();
      } else if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && state.view === "extract") {
        e.preventDefault();
        if (state.batchMode) runBatch();
        else runExtract();
      }
    });
  }

  /* ---------- Init ---------- */
  (async () => {
    applyTheme(localStorage.getItem("neon_sync_theme") || "dark");
    bindEvents();
    try {
      const health = await (await fetch("/v1/health")).json();
      if (health.version) $("app-version").textContent = `v${health.version}`;
    } catch (_) {}
    await Promise.all([loadCatalog(), loadEngines(false)]);
    try {
      const existing = await (await fetch("/v1/jobs")).json();
      for (const j of existing) state.jobs.set(j.job_id, j);
      renderJobs();
    } catch (_) {}
  })();
})();
