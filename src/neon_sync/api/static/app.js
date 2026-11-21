/* Neon Sync Agent — Kinetic Media Reactor Frontend Controller */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const URL_RE = /https?:\/\/[^\s<>"')]+/i;

  const state = {
    mode: "auto",
    forceAgent: false,
    videoQuality: "best",
    h264Mp4: true,
    audioFormat: "mp3",
    audioBitrate: "320k",
    loudnorm: false,
    imageFormat: "original",
    imageMaxWidth: "original",
    browserFallback: false,
    clipEnabled: false,
    subsEnabled: false,
    embedSubs: true,
    generateTranscript: true,
    finetuneOpen: false,
    batchMode: false,
    autoSave: localStorage.getItem("neon_autosave") !== "false",
    sfxEnabled: localStorage.getItem("neon_sfx") !== "false",
    lockedFormatId: null,
    probeAbort: null,
    probeTimer: null,
    lastProbedUrl: "",
    probeData: null,
    streamsOpen: false,
    tier: null,
    proKey: sessionStorage.getItem("neon_pro_key") || "",
    byokKey: sessionStorage.getItem("neon_byok_key") || "",
    jobs: new Map(),
    sse: new Map(),
    autoSavedJobs: new Set(),
    oscActive: false,
  };

  /* ---------- Web Audio Tactile Micro-Synthesizer (Zero Assets) ---------- */
  const sfx = (() => {
    let ctx = null;
    function getCtx() {
      if (!state.sfxEnabled) return null;
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      if (!ctx) ctx = new AC();
      if (ctx.state === "suspended") ctx.resume().catch(() => {});
      return ctx;
    }
    function tone(freq, dur, type = "sine", gainVal = 0.04, slideTo = null) {
      const c = getCtx();
      if (!c) return;
      const now = c.currentTime;
      const osc = c.createOscillator();
      const g = c.createGain();
      osc.type = type;
      osc.frequency.setValueAtTime(freq, now);
      if (slideTo) osc.frequency.exponentialRampToValueAtTime(slideTo, now + dur);
      g.gain.setValueAtTime(gainVal, now);
      g.gain.exponentialRampToValueAtTime(0.0001, now + dur);
      osc.connect(g).connect(c.destination);
      osc.start(now);
      osc.stop(now + dur + 0.01);
    }
    return {
      click: () => tone(520, 0.028, "triangle", 0.032, 210),
      capture: () => {
        tone(340, 0.09, "sine", 0.045, 760);
        setTimeout(() => tone(880, 0.08, "sine", 0.03), 55);
      },
      step: () => tone(640, 0.05, "sine", 0.024, 720),
      complete: () => {
        tone(523.25, 0.14, "sine", 0.045);
        setTimeout(() => tone(783.99, 0.22, "sine", 0.045), 95);
      },
      locked: () => {
        tone(190, 0.07, "sawtooth", 0.03, 130);
        setTimeout(() => tone(150, 0.09, "sawtooth", 0.025, 110), 75);
      },
    };
  })();

  /* ---------- 60fps Gravitational Dot-Matrix Field + Implosion Shockwave ---------- */
  const gravity = (() => {
    const canvas = $("gravity-field");
    if (!canvas) return { implosion: () => {}, pulse: () => {} };
    const ctx = canvas.getContext("2d");
    let w = 0, h = 0, dpr = 1;
    const pointer = { x: -9999, y: -9999, active: false };
    const waves = [];

    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    resize();
    window.addEventListener("resize", resize);

    window.addEventListener("pointermove", (e) => {
      pointer.x = e.clientX;
      pointer.y = e.clientY;
      pointer.active = true;
      const cr = $("coord-readout");
      if (cr) {
        const px = String(Math.round(e.clientX)).padStart(4, "0");
        const py = String(Math.round(e.clientY)).padStart(4, "0");
        cr.textContent = `X:${px} Y:${py} · FIELD:${state.oscActive ? "ENERGIZED" : "LOCKED"}`;
      }
    });
    window.addEventListener("pointerleave", () => { pointer.active = false; });

    function getReactorCenter() {
      const el = $("reactor-shell");
      if (!el) return { x: w / 2, y: h * 0.4 };
      const r = el.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    }

    function addWave(kind) {
      const c = getReactorCenter();
      waves.push({ x: c.x, y: c.y, t: 0, kind });
    }

    function frame() {
      ctx.clearRect(0, 0, w, h);
      const isDark = (document.documentElement.getAttribute("data-theme") || "dark") === "dark";
      const baseAlpha = isDark ? 0.16 : 0.14;
      const dotRgb = isDark ? "255,255,255" : "18,18,21";
      const accentRgb = isDark ? "255,77,0" : "230,68,0";
      const okRgb = "22,196,127";

      for (let i = waves.length - 1; i >= 0; i--) {
        waves[i].t += 0.028;
        if (waves[i].t >= 1) waves.splice(i, 1);
      }

      const spacing = 34;
      for (let gx = spacing * 0.5; gx < w; gx += spacing) {
        for (let gy = spacing * 0.5; gy < h; gy += spacing) {
          let dx = 0, dy = 0, energy = 0, useOk = false;

          if (pointer.active) {
            const vx = pointer.x - gx;
            const vy = pointer.y - gy;
            const dist = Math.hypot(vx, vy);
            if (dist < 170 && dist > 1) {
              const pull = (1 - dist / 170) * 11;
              dx += (vx / dist) * pull;
              dy += (vy / dist) * pull;
              energy = Math.max(energy, 1 - dist / 170);
            }
          }

          for (const wave of waves) {
            const vx = gx - wave.x;
            const vy = gy - wave.y;
            const dist = Math.hypot(vx, vy) || 1;
            const radius = wave.t * Math.max(w, h) * 0.72;
            const band = Math.abs(dist - radius);
            if (band < 95) {
              const force = (1 - band / 95) * (1 - wave.t);
              const dir = wave.kind === "implode" && wave.t < 0.25 ? -18 : 22;
              dx += (vx / dist) * force * dir;
              dy += (vy / dist) * force * dir;
              energy = Math.max(energy, force);
              if (wave.kind === "complete") useOk = true;
            }
          }

          const r = 1.15 + energy * 1.45;
          if (energy > 0.14) {
            ctx.fillStyle = `rgba(${useOk ? okRgb : accentRgb},${(baseAlpha + energy * 0.65).toFixed(2)})`;
          } else {
            ctx.fillStyle = `rgba(${dotRgb},${baseAlpha})`;
          }
          ctx.beginPath();
          ctx.arc(gx + dx, gy + dy, r, 0, Math.PI * 2);
          ctx.fill();
        }
      }
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);

    return {
      implosion: () => {
        addWave("implode");
        const shell = $("reactor-shell");
        if (shell) {
          shell.classList.remove("shock");
          void shell.offsetWidth;
          shell.classList.add("shock");
        }
      },
      pulse: () => addWave("complete"),
    };
  })();

  /* ---------- Oscilloscope Visualizer ---------- */
  const osc = (() => {
    const host = $("oscilloscope");
    const bars = [];
    if (host) {
      for (let i = 0; i < 24; i++) {
        const b = document.createElement("span");
        b.className = "osc-bar";
        host.appendChild(b);
        bars.push(b);
      }
    }
    let phase = 0;
    setInterval(() => {
      phase += 0.35;
      if (!host) return;
      host.classList.toggle("active", state.oscActive);
      bars.forEach((b, idx) => {
        const h = state.oscActive
          ? 4 + Math.round((Math.sin(phase + idx * 0.55) * 0.5 + 0.5) * 12)
          : 3 + Math.round((Math.sin(phase * 0.25 + idx * 0.4) * 0.5 + 0.5) * 3);
        b.style.height = `${h}px`;
      });
    }, 85);

    return {
      set: (active, label) => {
        state.oscActive = active;
        if (label && $("reactor-status-text")) $("reactor-status-text").textContent = label;
      },
    };
  })();

  /* ---------- Magnetic Primary Buttons & 3D Tilt Cartridges ---------- */
  function attachMagnetic(el) {
    if (!el) return;
    el.addEventListener("pointermove", (e) => {
      const r = el.getBoundingClientRect();
      const x = (e.clientX - r.left - r.width / 2) * 0.18;
      const y = (e.clientY - r.top - r.height / 2) * 0.22;
      el.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)`;
    });
    el.addEventListener("pointerleave", () => { el.style.transform = ""; });
  }

  function attachTilt(card) {
    if (!card || card.dataset.tiltBound) return;
    card.dataset.tiltBound = "1";
    card.addEventListener("pointermove", (e) => {
      const r = card.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width;
      const py = (e.clientY - r.top) / r.height;
      const rx = (0.5 - py) * 5;
      const ry = (px - 0.5) * 5;
      card.style.transform = `perspective(900px) rotateX(${rx.toFixed(2)}deg) rotateY(${ry.toFixed(2)}deg)`;
      card.style.setProperty("--mx", `${(px * 100).toFixed(1)}%`);
      card.style.setProperty("--my", `${(py * 100).toFixed(1)}%`);
    });
    card.addEventListener("pointerleave", () => { card.style.transform = ""; });
  }

  /* ---------- Auth & API Headers (Guest / Pro / BYOK) ---------- */
  function apiHeaders(extra = {}) {
    const h = { ...extra };
    if (state.proKey) h["X-Neon-Pro-Key"] = state.proKey;
    if (state.byokKey) h["X-Gemini-Api-Key"] = state.byokKey;
    return h;
  }

  function isPro() {
    return state.tier?.tier === "pro";
  }

  async function refreshTier() {
    try {
      const res = await fetch("/v1/tier", { headers: apiHeaders() });
      if (!res.ok) return;
      state.tier = await res.json();
      renderTierGauge();
    } catch (_) {}
  }

  function renderLedStrip(el, remaining, total, isOk) {
    if (!el) return;
    const count = Math.min(total, 5);
    const lit = total > 0 ? Math.round((remaining / total) * count) : 0;
    let html = "";
    for (let i = 0; i < count; i++) {
      html += `<span class="led-seg ${i < lit ? (isOk ? "lit-ok" : "lit") : ""}"></span>`;
    }
    el.innerHTML = html;
  }

  function renderTierGauge() {
    const t = state.tier;
    if (!t) return;
    const pro = t.tier === "pro";
    document.body.classList.toggle("is-pro", pro);
    $("tier-pill").classList.toggle("is-pro", pro);
    $("tier-badge").textContent = pro ? (state.byokKey ? "PRO · BYOK" : "PRO") : "GUEST";
    $("clear-keys-btn").classList.toggle("hidden", !pro);

    if (pro) {
      $("fuel-ai-count").textContent = "∞";
      $("fuel-dl-count").textContent = "∞";
      renderLedStrip($("fuel-ai-leds"), 5, 5, true);
      renderLedStrip($("fuel-dl-leds"), 5, 5, true);
      $("tier-cta").textContent = t.has_llm_provider ? "Gemini Ready ✓" : "Pro Active ✓";
    } else {
      $("fuel-ai-count").textContent = `${t.agent_runs_remaining}/${t.agent_runs_limit_daily}`;
      $("fuel-dl-count").textContent = `${t.downloads_remaining}/${t.downloads_limit_hourly}`;
      renderLedStrip($("fuel-ai-leds"), t.agent_runs_remaining, t.agent_runs_limit_daily, false);
      renderLedStrip($("fuel-dl-leds"), t.downloads_remaining, t.downloads_limit_hourly, false);
      $("tier-cta").textContent = "Unlock Pro ↗";
    }
  }

  function openUpgradeModal(reasonText = "") {
    sfx.locked();
    const box = $("upgrade-reason-box");
    if (reasonText) {
      box.textContent = reasonText;
      box.classList.remove("hidden");
    } else {
      box.classList.add("hidden");
    }
    $("pro-key-input").value = state.proKey;
    $("byok-key-input").value = state.byokKey;
    $("upgrade-modal").classList.remove("hidden");
  }

  /* ---------- Intent Detection (Direct URL vs Natural-Language Agent Prompt) ---------- */
  const PLATFORM_RULES = [
    { re: /youtu\.?be|youtube\.com/i, label: "YouTube" },
    { re: /instagram\.com/i, label: "Instagram" },
    { re: /(?:twitter|x|vxtwitter|fxtwitter)\.com/i, label: "X / Twitter" },
    { re: /tiktok\.com/i, label: "TikTok" },
    { re: /reddit\.com|redd\.it/i, label: "Reddit" },
    { re: /twitch\.tv/i, label: "Twitch" },
    { re: /soundcloud\.com|bandcamp\.com/i, label: "Audio" },
    { re: /imgur\.com|pixiv\.net|deviantart\.com|flickr\.com|artstation\.com|pinterest\.com/i, label: "Gallery" },
    { re: /\.m3u8(\?|$)/i, label: "HLS" },
    { re: /\.mpd(\?|$)/i, label: "DASH" },
    { re: /\.pdf(\?|$)/i, label: "PDF" },
    { re: /\.(?:mp3|m4a|flac|wav|ogg|opus)(\?|$)/i, label: "Direct Audio" },
    { re: /\.(?:jpg|jpeg|png|webp|gif|avif)(\?|$)/i, label: "Direct Image" },
    { re: /\.(?:mp4|webm|mov|mkv)(\?|$)/i, label: "Direct Video" },
  ];

  function extractUrl(text) {
    const m = (text || "").match(URL_RE);
    return m ? m[0].replace(/[.,;:]+$/, "") : null;
  }

  function isNaturalLanguageCommand(raw) {
    const trimmed = (raw || "").trim();
    if (!trimmed) return false;
    if (state.forceAgent) return true;
    const url = extractUrl(trimmed);
    if (!url) return trimmed.includes(" ");
    const rest = trimmed.replace(url, "").trim();
    return rest.length >= 3;
  }

  function detectPlatform(url) {
    for (const r of PLATFORM_RULES) {
      if (r.re.test(url)) return r.label;
    }
    try {
      return new URL(url).hostname.replace(/^www\./, "");
    } catch (_) {
      return null;
    }
  }

  function syncReactorModeVisuals() {
    const raw = $("url-input").value.trim();
    const agent = isNaturalLanguageCommand(raw);
    $("reactor-shell").classList.toggle("agent-mode", agent);
    $("intent-label").textContent = agent ? "NEON AI AGENT" : "DIRECT EXTRACT";
    $("fire-label").textContent = agent ? "Run Agent" : "Extract";
  }

  /* ---------- Utilities ---------- */
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
    }, 3200);
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

  /* ---------- Mode & Payload Builder ---------- */
  function setMode(mode) {
    state.mode = mode;
    sfx.click();
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
      allow_browser_fallback: state.browserFallback,
      custom_filename_prefix: $("filename-prefix").value.trim(),
      playlist_items: $("playlist-items").value.trim() || null,
      max_items: 50,
    };
    if (state.lockedFormatId) payload.format_id = state.lockedFormatId;
    return payload;
  }

  /* ---------- Automatic Background Probe ---------- */
  function onUrlChanged(triggerProbe = true, fireImplosion = false) {
    const raw = $("url-input").value.trim();
    $("clear-url-btn").classList.toggle("hidden", !raw);
    syncReactorModeVisuals();

    const url = extractUrl(raw);
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
      $("platform-tag").textContent = plat.toUpperCase();
      $("platform-tag").classList.remove("hidden");
    }

    if (fireImplosion) {
      gravity.implosion();
      sfx.capture();
    }

    if (!triggerProbe || url === state.lastProbedUrl) return;
    clearTimeout(state.probeTimer);
    state.probeTimer = setTimeout(() => autoProbeUrl(url), 240);
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
    osc.set(true, "PROBING SIGNAL…");

    try {
      const res = await fetch("/v1/probe", {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          url,
          preferred_engine: $("preferred-engine").value || null,
          allow_browser_fallback: state.browserFallback,
        }),
        signal: ctrl.signal,
      });
      const data = await res.json();
      if (res.status === 402) {
        $("preview-stage").classList.add("hidden");
        openUpgradeModal(data.detail?.message);
        return;
      }
      if (!res.ok) {
        $("preview-stage").classList.add("hidden");
        return;
      }
      renderLivePreview(data);
    } catch (_) {
      if (!ctrl.signal.aborted) $("preview-stage").classList.add("hidden");
    } finally {
      osc.set(false, "SIGNAL LOCKED");
    }
  }

  function renderLivePreview(data) {
    state.probeData = data;
    $("preview-loading").classList.add("hidden");
    $("preview-content").classList.remove("hidden");
    attachTilt($("preview-stage"));

    $("preview-engine-badge").textContent = `ENGINE: ${data.engine.engine_used.toUpperCase()}`;

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
      (data.subtitles || []).length ? `${data.subtitles.length} caption(s)` : null,
      (data.items || []).length > 1 ? `${data.items.length} items` : null,
    ].filter(Boolean);
    $("preview-meta").textContent = meta.join(" · ");

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
      btn.textContent = `Streams (${fmts.length})`;
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
      b.textContent = `LOCKED: ${state.lockedFormatId} ×`;
      b.classList.remove("hidden");
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
        sfx.click();
        const id = tr.dataset.id;
        state.lockedFormatId = state.lockedFormatId === id ? null : id;
        renderStreamsTable(fmts);
        updateLockedBadge();
      };
    });
  }

  /* ---------- Live Streaming Neon Sync Agent (/v1/agent/run) ---------- */
  function appendTraceStep(tag, cls, message) {
    const feed = $("agent-trace-feed");
    const row = document.createElement("div");
    row.className = "trace-step";
    row.innerHTML = `<span class="step-tag mono ${cls}">${esc(tag)}</span><span>${esc(message)}</span>`;
    feed.appendChild(row);
  }

  async function runAgentPrompt(customPrompt = null) {
    const raw = (customPrompt || $("url-input").value).trim();
    const url = extractUrl(raw) || state.lastProbedUrl;
    if (!url) {
      toast("Include a valid http(s) URL in your command", "err");
      return;
    }

    $("agent-stage").classList.remove("hidden");
    $("agent-trace-feed").innerHTML = "";
    $("agent-answer-box").classList.add("hidden");
    $("agent-answer-box").textContent = "";
    $("agent-provider-tag").textContent = "";
    $("download-btn").disabled = true;
    osc.set(true, "AGENT REASONING…");
    gravity.implosion();
    sfx.capture();

    try {
      const res = await fetch("/v1/agent/run", {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ prompt: raw, url }),
      });

      if (res.status === 402) {
        const err = await res.json();
        $("agent-stage").classList.add("hidden");
        openUpgradeModal(`${err.detail?.message} ${err.detail?.upgrade_hint || ""}`.trim());
        return;
      }
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        appendTraceStep("ERROR", "err", err.detail?.message || "Agent failed to start");
        return;
      }

      refreshTier();
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() || "";

        for (const frame of frames) {
          let evtType = "message";
          let dataStr = "";
          for (const line of frame.split("\n")) {
            if (line.startsWith("event:")) evtType = line.slice(6).trim();
            else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
          }
          if (!dataStr) continue;
          const payload = JSON.parse(dataStr);
          handleAgentEvent(evtType, payload);
        }
      }
    } catch (err) {
      appendTraceStep("ERROR", "err", `Stream error: ${err.message}`);
    } finally {
      $("download-btn").disabled = false;
      osc.set(false, "AGENT READY");
      refreshTier();
    }
  }

  function handleAgentEvent(type, ev) {
    if (type === "thought") {
      sfx.step();
      appendTraceStep("THINK", "", ev.message);
    } else if (type === "plan") {
      sfx.step();
      if (ev.data?.provider) $("agent-provider-tag").textContent = `via ${ev.data.provider}`;
      appendTraceStep("PLAN", "plan", ev.message);
    } else if (type === "tool") {
      sfx.step();
      appendTraceStep(`TOOL · ${ev.data?.tool || "exec"}`, "tool", ev.message);
    } else if (type === "job" && ev.data?.job_id) {
      state.jobs.set(ev.data.job_id, ev.data);
      renderDownloads();
      if (ev.data.status === "completed" && ev.data.result?.download?.artifact_url) {
        const dl = ev.data.result.download;
        if (state.autoSave && !state.autoSavedJobs.has(ev.data.job_id)) {
          state.autoSavedJobs.add(ev.data.job_id);
          triggerBrowserSave(dl.artifact_url, dl.filename);
          toast(`Agent saved ${dl.filename}`, "ok");
        }
      }
    } else if (type === "progress" && ev.data?.job_id) {
      const cur = state.jobs.get(ev.data.job_id) || { job_id: ev.data.job_id };
      cur.status = ev.data.status;
      cur.progress = ev.data;
      state.jobs.set(ev.data.job_id, cur);
      renderDownloads();
    } else if (type === "answer") {
      sfx.complete();
      gravity.pulse();
      const box = $("agent-answer-box");
      box.textContent = ev.message;
      box.classList.remove("hidden");
    } else if (type === "error") {
      appendTraceStep("HALT", "err", ev.message);
      if (ev.data?.code === "ERR_PRO_FEATURE_REQUIRED" || ev.data?.code === "ERR_GUEST_QUOTA_EXCEEDED") {
        openUpgradeModal(`${ev.message} ${ev.data?.upgrade_hint || ""}`.trim());
      } else {
        toast(ev.message, "err");
      }
    }
  }

  /* ---------- Direct Download & Auto-Save ---------- */
  function triggerBrowserSave(artifactUrl, filename) {
    const a = document.createElement("a");
    a.href = artifactUrl;
    a.download = filename || "download";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 500);
  }

  async function firePrimaryAction() {
    const raw = $("url-input").value.trim();
    if (!raw) return toast("Paste a link or type an Agent command first", "err");

    if (isNaturalLanguageCommand(raw)) {
      return runAgentPrompt(raw);
    }

    const url = extractUrl(raw);
    if (!url) return toast("Enter a valid http(s) URL", "err");

    const btn = $("download-btn");
    btn.disabled = true;
    osc.set(true, "EXTRACTING…");
    gravity.implosion();
    sfx.capture();

    try {
      const res = await fetch("/v1/jobs", {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(buildPayload(url)),
      });
      const job = await res.json();
      if (res.status === 402) {
        openUpgradeModal(`${job.detail?.message} ${job.detail?.upgrade_hint || ""}`.trim());
        osc.set(false, "UPGRADE REQUIRED");
        return;
      }
      if (!res.ok) {
        toast(job.detail?.message || "Could not start download", "err");
        osc.set(false, "STANDBY");
        return;
      }

      refreshTier();
      state.jobs.set(job.job_id, job);
      renderDownloads();
      watchJob(job.job_id);
    } catch (err) {
      toast(`Network error: ${err.message}`, "err");
      osc.set(false, "STANDBY");
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
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ urls, ...base }),
    });
    const list = await res.json();
    if (res.status === 402) {
      openUpgradeModal(`${list.detail?.message} ${list.detail?.upgrade_hint || ""}`.trim());
      return;
    }
    if (!res.ok) return toast(list.detail?.message || "Batch rejected", "err");

    refreshTier();
    gravity.implosion();
    sfx.capture();
    for (const j of list) {
      state.jobs.set(j.job_id, j);
      watchJob(j.job_id);
    }
    renderDownloads();
    toast(`Extracting ${list.length} item(s)…`, "ok");
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
        osc.set(false, "EXTRACTION COMPLETE");
        const full = await (await fetch(`/v1/jobs/${jobId}`)).json();
        state.jobs.set(jobId, full);
        renderDownloads();

        const dl = full.result?.download;
        if (p.status === "completed" && dl?.artifact_url) {
          sfx.complete();
          gravity.pulse();
          if (state.autoSave && !state.autoSavedJobs.has(jobId)) {
            state.autoSavedJobs.add(jobId);
            triggerBrowserSave(dl.artifact_url, dl.filename);
            toast(`Saved ${dl.filename} to Downloads`, "ok");
          } else {
            toast(`Ready: ${dl.filename}`, "ok");
          }
        } else if (p.status === "failed") {
          toast(full.error || "Extraction failed", "err");
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
    $("jobs-summary").textContent = arr.length ? `${arr.length} CARTRIDGE${arr.length === 1 ? "" : "S"}` : "";

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
        actions.push(`<a class="btn-link" href="${esc(dl.artifact_url)}" download="${esc(dl.filename || "download")}">Save File</a>`);
      }
      if (tr?.artifact_url) {
        actions.push(`<a class="ghost-btn btn-xs" href="${esc(tr.artifact_url)}">Transcript .txt</a>`);
      }
      if (dl?.sha256) {
        actions.push(`<button class="ghost-btn btn-xs mono" data-sha="${esc(dl.sha256)}">SHA-256</button>`);
      }
      if (isActive) {
        actions.push(`<button class="ghost-btn btn-xs" data-cancel="${esc(j.job_id)}">Cancel</button>`);
      }

      return `
        <div class="dl-card">
          <div class="dl-top">
            <div class="dl-name" title="${esc(j.url)}">${esc(title)}</div>
            <span class="status-pill mono ${isDone ? "ok" : isErr ? "err" : "warn"}">${esc(j.status)}</span>
          </div>
          <div class="progress-rail">
            <div class="progress-bar ${isDone ? "done" : isErr ? "err" : ""}" style="width:${Math.min(100, Math.max(4, pct))}%"></div>
          </div>
          <div class="dl-bottom">
            <div class="dl-status-line">${statusBits.filter(Boolean).join(" &middot; ")}</div>
            <div class="dl-actions">${actions.join("")}</div>
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
    container.querySelectorAll("[data-sha]").forEach((btn) => {
      btn.onclick = async () => {
        await navigator.clipboard.writeText(btn.dataset.sha);
        sfx.click();
        toast("SHA-256 checksum copied", "ok");
      };
    });
  }

  /* ---------- Engine Registry ---------- */
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
          <td><span class="status-pill mono ${ok ? "ok" : "warn"}">${esc(ok ? "ready" : e.installation_status)}</span></td>
          <td class="mono num">${esc(e.version)}</td>
          <td class="mono num">Tier ${e.tier}</td>
          <td class="muted">${esc(e.license)}</td>
        </tr>`;
    }).join("");
    if (refresh) toast(`Engines refreshed (${installed}/${list.length} ready)`, "ok");
  }

  /* ---------- Event Wiring ---------- */
  function bindEvents() {
    const input = $("url-input");
    attachMagnetic($("download-btn"));
    attachMagnetic($("download-batch-btn"));

    input.addEventListener("input", () => onUrlChanged(true, false));
    input.addEventListener("paste", () => setTimeout(() => onUrlChanged(true, true), 15));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        firePrimaryAction();
      }
    });

    // Global Keyboard Shortcuts
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        $("upgrade-modal").classList.add("hidden");
        $("engines-modal").classList.add("hidden");
        $("api-modal").classList.add("hidden");
      }
      if ((e.key === "/" || ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k")) &&
          document.activeElement?.tagName !== "INPUT" && document.activeElement?.tagName !== "TEXTAREA") {
        e.preventDefault();
        sfx.click();
        input.focus();
      }
    });

    // Global Paste Anywhere -> Gravitational Capture!
    document.addEventListener("paste", (e) => {
      const active = document.activeElement;
      if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) return;
      const text = (e.clipboardData || window.clipboardData)?.getData("text");
      if (text && URL_RE.test(text.trim())) {
        e.preventDefault();
        input.value = text.trim();
        input.focus();
        onUrlChanged(true, true);
      }
    });

    // Drag & Drop targeting reticle
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
      if (text && URL_RE.test(text.trim())) {
        input.value = text.trim().split(/\r?\n/)[0];
        onUrlChanged(true, true);
      }
    });

    $("intent-mode-toggle").onclick = () => {
      state.forceAgent = !state.forceAgent;
      sfx.click();
      syncReactorModeVisuals();
      toast(state.forceAgent ? "Neon AI Agent mode locked ON" : "Smart Auto mode (URL = Extract, Prompt = Agent)");
    };

    $("paste-btn").onclick = async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (text) {
          input.value = text.trim();
          onUrlChanged(true, true);
        }
      } catch (_) {
        input.focus();
        toast("Press ⌘V / Ctrl+V to paste");
      }
    };

    $("clear-url-btn").onclick = () => {
      sfx.click();
      input.value = "";
      onUrlChanged(false, false);
      input.focus();
    };

    $("download-btn").onclick = firePrimaryAction;
    $("download-batch-btn").onclick = startBatchDownload;
    $("close-agent-stage").onclick = () => $("agent-stage").classList.add("hidden");

    // Agent Recipe Chips
    document.querySelectorAll(".recipe-chip").forEach((chip) => {
      chip.onclick = () => {
        sfx.click();
        const existingUrl = extractUrl(input.value) || state.lastProbedUrl;
        const cmd = chip.dataset.recipe;
        if (existingUrl) {
          input.value = `${cmd} ${existingUrl}`;
          syncReactorModeVisuals();
          runAgentPrompt(input.value);
        } else {
          input.value = `${cmd} `;
          state.forceAgent = true;
          syncReactorModeVisuals();
          input.focus();
          toast("Now paste a link at the end and hit Enter!");
        }
      };
    });

    // Quick Agent Chips right on the Media Cartridge
    $("quick-agent-clip").onclick = () => runAgentPrompt(`clip 0:00 to 0:15 in 720p mp4 ${state.lastProbedUrl}`);
    $("quick-agent-mp3").onclick = () => runAgentPrompt(`extract audio as 320k mp3 and normalize volume ${state.lastProbedUrl}`);
    $("quick-agent-sum").onclick = () => runAgentPrompt(`summarize the key points from ${state.lastProbedUrl}`);

    // Mode tabs
    document.querySelectorAll("#mode-group .seg").forEach((b) => {
      b.onclick = () => setMode(b.dataset.mode);
    });

    // Video quality pills (Pro-gated 4K/2K check)
    document.querySelectorAll("#quality-pills [data-vq]").forEach((b) => {
      b.onclick = () => {
        const vq = b.dataset.vq;
        if ((vq === "2160" || vq === "1440") && !isPro()) {
          openUpgradeModal(`${vq === "2160" ? "4K" : "2K"} Ultra-HD extraction requires Neon Pro (Guest includes up to 1080p).`);
          return;
        }
        sfx.click();
        state.videoQuality = vq;
        state.lockedFormatId = null;
        updateLockedBadge();
        document.querySelectorAll("#quality-pills [data-vq]").forEach((x) => x.classList.toggle("active", x === b));
      };
    });

    $("h264-pill").onclick = () => {
      sfx.click();
      state.h264Mp4 = !state.h264Mp4;
      $("h264-pill").classList.toggle("toggle-on", state.h264Mp4);
      $("video-codec").value = state.h264Mp4 ? "h264" : "auto";
      $("video-container").value = state.h264Mp4 ? "mp4" : "auto";
    };

    // Audio pills (Pro-gated FLAC/WAV check)
    document.querySelectorAll("#audio-fmt-pills [data-af]").forEach((b) => {
      b.onclick = () => {
        const af = b.dataset.af;
        if ((af === "flac" || af === "wav") && !isPro()) {
          openUpgradeModal(`Lossless ${af.toUpperCase()} mastering requires Neon Pro (Guest includes MP3 320k, M4A & Opus).`);
          return;
        }
        sfx.click();
        state.audioFormat = af;
        document.querySelectorAll("#audio-fmt-pills [data-af]").forEach((x) => x.classList.toggle("active", x === b));
      };
    });
    document.querySelectorAll("#audio-br-pills [data-ab]").forEach((b) => {
      b.onclick = () => {
        sfx.click();
        state.audioBitrate = b.dataset.ab;
        document.querySelectorAll("#audio-br-pills [data-ab]").forEach((x) => x.classList.toggle("active", x === b));
      };
    });
    $("loudnorm-pill").onclick = () => {
      sfx.click();
      state.loudnorm = !state.loudnorm;
      $("loudnorm-pill").classList.toggle("toggle-on", state.loudnorm);
    };

    // Image pills
    document.querySelectorAll("#img-fmt-pills [data-if]").forEach((b) => {
      b.onclick = () => {
        sfx.click();
        state.imageFormat = b.dataset.if;
        document.querySelectorAll("#img-fmt-pills [data-if]").forEach((x) => x.classList.toggle("active", x === b));
      };
    });
    document.querySelectorAll("#img-width-pills [data-iw]").forEach((b) => {
      b.onclick = () => {
        sfx.click();
        state.imageMaxWidth = b.dataset.iw;
        document.querySelectorAll("#img-width-pills [data-iw]").forEach((x) => x.classList.toggle("active", x === b));
      };
    });

    $("browser-fallback-pill").onclick = () => {
      if (!isPro()) {
        openUpgradeModal("Tier-3 Playwright headless browser extraction is a Pro feature.");
        return;
      }
      sfx.click();
      state.browserFallback = !state.browserFallback;
      $("browser-fallback-pill").classList.toggle("toggle-on", state.browserFallback);
    };

    // Quick toggles
    $("clip-toggle-btn").onclick = () => {
      sfx.click();
      state.clipEnabled = !state.clipEnabled;
      $("clip-toggle-btn").classList.toggle("toggle-on", state.clipEnabled);
      $("clip-box").classList.toggle("hidden", !state.clipEnabled);
    };
    $("subs-toggle-btn").onclick = () => {
      sfx.click();
      state.subsEnabled = !state.subsEnabled;
      $("subs-toggle-btn").classList.toggle("toggle-on", state.subsEnabled);
      $("subs-box").classList.toggle("hidden", !state.subsEnabled);
    };
    $("embed-subs-pill").onclick = () => {
      sfx.click();
      state.embedSubs = !state.embedSubs;
      $("embed-subs-pill").classList.toggle("toggle-on", state.embedSubs);
    };
    $("transcript-pill").onclick = () => {
      sfx.click();
      state.generateTranscript = !state.generateTranscript;
      $("transcript-pill").classList.toggle("toggle-on", state.generateTranscript);
    };
    $("batch-toggle-btn").onclick = () => {
      sfx.click();
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
      sfx.click();
      state.finetuneOpen = !state.finetuneOpen;
      $("finetune-toggle-btn").classList.toggle("toggle-on", state.finetuneOpen);
      $("finetune-drawer").classList.toggle("hidden", !state.finetuneOpen);
    };
    $("image-quality").oninput = (e) => { $("img-q-label").textContent = `${e.target.value}%`; };
    $("toggle-streams-btn").onclick = () => {
      sfx.click();
      state.streamsOpen = !state.streamsOpen;
      $("streams-drawer").classList.toggle("hidden", !state.streamsOpen);
    };

    // Upgrade / Unlock Pro & BYOK Sheet
    $("tier-pill").onclick = () => openUpgradeModal("");
    $("close-upgrade-modal").onclick = () => $("upgrade-modal").classList.add("hidden");
    $("tab-pro-key").onclick = () => {
      sfx.click();
      $("tab-pro-key").classList.add("active");
      $("tab-byok-key").classList.remove("active");
      $("panel-pro-key").classList.remove("hidden");
      $("panel-byok-key").classList.add("hidden");
    };
    $("tab-byok-key").onclick = () => {
      sfx.click();
      $("tab-byok-key").classList.add("active");
      $("tab-pro-key").classList.remove("active");
      $("panel-byok-key").classList.remove("hidden");
      $("panel-pro-key").classList.add("hidden");
    };
    $("fill-dev-pro-btn").onclick = () => {
      $("pro-key-input").value = "neon-pro-local";
      $("save-pro-key-btn").click();
    };
    $("save-pro-key-btn").onclick = async () => {
      state.proKey = $("pro-key-input").value.trim();
      sessionStorage.setItem("neon_pro_key", state.proKey);
      await refreshTier();
      if (isPro()) {
        sfx.complete();
        gravity.pulse();
        $("upgrade-modal").classList.add("hidden");
        toast("Neon Pro unlocked! 4K, Lossless FLAC & 500 Agent runs ready.", "ok");
      } else {
        toast("Key not recognized (try neon-pro-local in dev)", "err");
      }
    };
    $("save-byok-key-btn").onclick = async () => {
      state.byokKey = $("byok-key-input").value.trim();
      sessionStorage.setItem("neon_byok_key", state.byokKey);
      await refreshTier();
      if (isPro()) {
        sfx.complete();
        gravity.pulse();
        $("upgrade-modal").classList.add("hidden");
        toast("Gemini BYOK connected — Pro Agent unlocked!", "ok");
      }
    };
    $("clear-keys-btn").onclick = async () => {
      state.proKey = "";
      state.byokKey = "";
      sessionStorage.removeItem("neon_pro_key");
      sessionStorage.removeItem("neon_byok_key");
      await refreshTier();
      $("upgrade-modal").classList.add("hidden");
      toast("Switched back to Guest Trial mode");
    };

    // Bezel Controls: Auto-save, SFX, Engines, API, Theme
    function syncAutosaveUi() {
      $("autosave-pill").classList.toggle("active", state.autoSave);
    }
    syncAutosaveUi();
    $("autosave-pill").onclick = () => {
      state.autoSave = !state.autoSave;
      localStorage.setItem("neon_autosave", String(state.autoSave));
      syncAutosaveUi();
      sfx.click();
      toast(state.autoSave ? "Auto-save enabled" : "Auto-save disabled");
    };

    function syncSfxUi() {
      $("sfx-btn").classList.toggle("active", state.sfxEnabled);
      $("sfx-text").textContent = state.sfxEnabled ? "SFX" : "Muted";
    }
    syncSfxUi();
    $("sfx-btn").onclick = () => {
      state.sfxEnabled = !state.sfxEnabled;
      localStorage.setItem("neon_sfx", String(state.sfxEnabled));
      syncSfxUi();
      if (state.sfxEnabled) sfx.click();
    };

    $("engines-modal-btn").onclick = () => { sfx.click(); $("engines-modal").classList.remove("hidden"); };
    $("close-engines-modal").onclick = () => $("engines-modal").classList.add("hidden");
    $("refresh-engines-btn").onclick = () => loadEngines(true);

    $("api-modal-btn").onclick = () => {
      sfx.click();
      const raw = input.value.trim();
      const sample = extractUrl(raw) || "https://www.youtube.com/watch?v=jNQXAC9IVRw";
      const useAgent = isNaturalLanguageCommand(raw);
      const endpoint = useAgent ? "/v1/agent/run" : "/v1/jobs";
      const body = useAgent ? { prompt: raw, url: sample } : buildPayload(sample);
      const jsonStr = JSON.stringify(body, null, 2);
      const proHeader = state.proKey ? ` \\\n  -H "X-Neon-Pro-Key: ${state.proKey}"` : "";
      $("api-curl-pre").textContent =
        `curl -X POST "${window.location.origin}${endpoint}" \\\n` +
        `  -H "Content-Type: application/json"${proHeader} \\\n` +
        `  -d '${jsonStr}'`;
      $("api-modal").classList.remove("hidden");
    };
    $("close-api-modal").onclick = () => $("api-modal").classList.add("hidden");
    $("copy-curl-btn").onclick = async () => {
      await navigator.clipboard.writeText($("api-curl-pre").textContent);
      sfx.click();
      toast("cURL copied", "ok");
    };
    $("copy-json-btn").onclick = async () => {
      const sample = extractUrl(input.value.trim()) || "https://www.youtube.com/watch?v=jNQXAC9IVRw";
      await navigator.clipboard.writeText(JSON.stringify(buildPayload(sample), null, 2));
      sfx.click();
      toast("JSON copied", "ok");
    };

    function applyTheme(t) {
      document.documentElement.setAttribute("data-theme", t);
      localStorage.setItem("neon_sync_theme", t);
      $("theme-text").textContent = t === "dark" ? "Light" : "Dark";
    }
    applyTheme(localStorage.getItem("neon_sync_theme") || "dark");
    $("theme-btn").onclick = () => {
      sfx.click();
      const cur = document.documentElement.getAttribute("data-theme") || "dark";
      applyTheme(cur === "dark" ? "light" : "dark");
    };
  }

  /* ---------- Init ---------- */
  (async () => {
    bindEvents();
    await Promise.all([refreshTier(), loadEngines(false)]);
    try {
      const existing = await (await fetch("/v1/jobs")).json();
      for (const j of existing) {
        state.jobs.set(j.job_id, j);
        state.autoSavedJobs.add(j.job_id);
      }
      renderDownloads();
    } catch (_) {}
  })();
})();
