/* Neon Sync — Pure Cascading Spatial Possibility Tree & Patchbay Engine */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const URL_RE = /https?:\/\/[^\s<>"')]+/i;
  const LAYOUT_STORAGE_KEY = "neon_sync_spatial_layout_v5";

  const SHOWCASE_VIDEO = {
    url: "https://youtu.be/YW7Uc5_ixtM",
    title: "Sajan Sajan remix | by Abhishek Mehra",
    uploader: "TUNES OF ABHI",
    duration: 263,
    views: "8.0M views",
    thumbnail: "https://i.ytimg.com/vi/YW7Uc5_ixtM/maxresdefault.jpg",
  };

  /* ---------- Application State ---------- */
  const state = {
    forceAgent: false,
    batchOpen: false,
    autoSave: localStorage.getItem("neon_autosave") !== "false",
    sfxEnabled: localStorage.getItem("neon_sfx") !== "false",
    tier: null,
    proKey: sessionStorage.getItem("neon_pro_key") || "",
    byokKey: sessionStorage.getItem("neon_byok_key") || "",
    sse: new Map(),
    autoSavedJobs: new Set(),
    oscActive: false,
    historyFilter: "all",
    historyJobs: [],
    selectedNodeId: null,
    nodes: new Map(), // id -> { id, type: 'source'|'choice'|'agent'|'result'|'note', x, y, ... }
    edges: [],        // { id, from, to, kind, label, active?: bool, demo?: bool }
    sourceByUrl: new Map(),
    resultByJobId: new Map(),
    savedPositions: {},
    pendingPatch: null,
    hasDemoGraph: false,
    lastApiPayload: null,
  };

  try {
    const saved = JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY) || "{}");
    state.savedPositions = saved.positions || {};
  } catch (_) {}

  function persistWorkspace() {
    const positions = {};
    const notes = [];
    const customEdges = [];
    for (const n of state.nodes.values()) {
      if (n.demo || n.type === "choice") continue;
      positions[n.id] = { x: Math.round(n.x), y: Math.round(n.y), collapsed: !!n.collapsed };
      if (n.type === "note") notes.push({ id: n.id, x: Math.round(n.x), y: Math.round(n.y), text: n.text || "" });
    }
    for (const e of state.edges) {
      if (e.demo) continue;
      if (e.kind === "custom" || e.kind === "note" || e.customLabel) {
        customEdges.push({ id: e.id, from: e.from, to: e.to, kind: e.kind, label: e.label, customLabel: !!e.customLabel });
      }
    }
    state.savedPositions = positions;
    try { localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify({ positions, notes, customEdges })); } catch (_) {}
  }

  /* ---------- Web Audio Tactile Haptics Synthesizer ---------- */
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
    function tone(freq, dur, type = "sine", gainVal = 0.034, slideTo = null) {
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
      click: () => tone(520, 0.025, "triangle", 0.028, 220),
      snap: () => tone(780, 0.022, "sine", 0.026, 980),
      bloom: (count = 3) => {
        const notes = [440, 554.37, 659.25, 783.99, 880];
        for (let i = 0; i < Math.min(count, 4); i++) {
          setTimeout(() => tone(notes[i], 0.075, "sine", 0.022), i * 32);
        }
      },
      capture: () => {
        tone(340, 0.085, "sine", 0.04, 760);
        setTimeout(() => tone(880, 0.075, "sine", 0.026), 52);
      },
      plug: () => {
        tone(420, 0.045, "triangle", 0.042, 680);
        setTimeout(() => tone(940, 0.06, "sine", 0.028), 38);
      },
      step: () => tone(640, 0.045, "sine", 0.02, 720),
      complete: () => {
        tone(523.25, 0.13, "sine", 0.04);
        setTimeout(() => tone(783.99, 0.2, "sine", 0.04), 90);
      },
      locked: () => {
        tone(190, 0.065, "sawtooth", 0.026, 130);
        setTimeout(() => tone(150, 0.085, "sawtooth", 0.022, 110), 70);
      },
    };
  })();

  /* ---------- Infinite Camera (Pan, Zoom, Spring Glide & Fit) ---------- */
  const camera = {
    x: window.innerWidth * 0.5,
    y: (window.innerHeight - 54) * 0.44,
    zoom: 1,
    anim: null,
  };

  function applyCamera() {
    const world = $("world");
    if (world) world.style.transform = `translate3d(${camera.x}px, ${camera.y}px, 0) scale(${camera.zoom})`;
    const zBtn = $("zoom-reset-btn");
    if (zBtn) zBtn.textContent = `${Math.round(camera.zoom * 100)}%`;
    drawMinimap();
  }

  function screenToWorld(sx, sy) {
    const vp = $("viewport").getBoundingClientRect();
    return { x: (sx - vp.left - camera.x) / camera.zoom, y: (sy - vp.top - camera.y) / camera.zoom };
  }

  function worldToScreen(wx, wy) {
    const vp = $("viewport").getBoundingClientRect();
    return { x: vp.left + camera.x + wx * camera.zoom, y: vp.top + camera.y + wy * camera.zoom };
  }

  function getViewportCenterWorld() {
    const vp = $("viewport").getBoundingClientRect();
    return screenToWorld(vp.left + vp.width / 2, vp.top + vp.height * 0.42);
  }

  function glideToWorld(wx, wy, targetZoom = 1) {
    if (camera.anim) cancelAnimationFrame(camera.anim);
    const vp = $("viewport").getBoundingClientRect();
    const tx = vp.width / 2 - wx * targetZoom;
    const ty = vp.height * 0.43 - wy * targetZoom;
    const startX = camera.x, startY = camera.y, startZ = camera.zoom;
    const t0 = performance.now();
    const dur = 340;
    function tick(now) {
      const p = Math.min(1, (now - t0) / dur);
      const ease = 1 - Math.pow(1 - p, 3);
      camera.x = startX + (tx - startX) * ease;
      camera.y = startY + (ty - startY) * ease;
      camera.zoom = startZ + (targetZoom - startZ) * ease;
      applyCamera();
      if (p < 1) camera.anim = requestAnimationFrame(tick);
      else camera.anim = null;
    }
    camera.anim = requestAnimationFrame(tick);
  }

  function getGraphBounds() {
    if (!state.nodes.size) return { minX: -300, minY: -200, maxX: 300, maxY: 200 };
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of state.nodes.values()) {
      const el = document.querySelector(`[data-node-id="${CSS.escape(n.id)}"]`);
      const nw = el ? el.offsetWidth : 260;
      const nh = el ? el.offsetHeight : 64;
      minX = Math.min(minX, n.x);
      minY = Math.min(minY, n.y);
      maxX = Math.max(maxX, n.x + nw);
      maxY = Math.max(maxY, n.y + nh);
    }
    return { minX, minY, maxX, maxY };
  }

  function fitAllNodes() {
    if (!state.nodes.size) return glideToWorld(0, 0, 1);
    const b = getGraphBounds();
    const vp = $("viewport").getBoundingClientRect();
    const spanX = Math.max(520, b.maxX - b.minX);
    const spanY = Math.max(300, b.maxY - b.minY);
    const z = Math.max(0.45, Math.min(0.96, Math.min((vp.width - 130) / spanX, (vp.height - 210) / spanY)));
    glideToWorld((b.minX + b.maxX) / 2, (b.minY + b.maxY) / 2 + 16, z);
  }

  /* ---------- Live Interactive Spatial Minimap ---------- */
  let minimapMeta = null;
  function drawMinimap() {
    const cvs = $("minimap-canvas");
    if (!cvs) return;
    const ctx = cvs.getContext("2d");
    const mw = cvs.width, mh = cvs.height;
    ctx.clearRect(0, 0, mw, mh);

    const b = getGraphBounds();
    const pad = 220;
    const bx = b.minX - pad, by = b.minY - pad;
    const bw = Math.max(980, (b.maxX - b.minX) + pad * 2);
    const bh = Math.max(580, (b.maxY - b.minY) + pad * 2);
    const scale = Math.min(mw / bw, mh / bh);
    const ox = (mw - bw * scale) / 2 - bx * scale;
    const oy = (mh - bh * scale) / 2 - by * scale;
    minimapMeta = { scale, ox, oy };

    ctx.lineWidth = 1;
    for (const e of state.edges) {
      const n1 = state.nodes.get(e.from), n2 = state.nodes.get(e.to);
      if (!n1 || !n2) continue;
      ctx.strokeStyle = e.kind === "agent" ? "rgba(22,196,127,0.55)" : e.kind === "audio" ? "rgba(168,85,247,0.55)" : e.kind === "subtitles" ? "rgba(59,130,246,0.55)" : "rgba(255,77,0,0.55)";
      ctx.beginPath();
      ctx.moveTo(ox + (n1.x + 120) * scale, oy + (n1.y + 30) * scale);
      ctx.lineTo(ox + (n2.x + 120) * scale, oy + (n2.y + 30) * scale);
      ctx.stroke();
    }

    for (const n of state.nodes.values()) {
      const el = document.querySelector(`[data-node-id="${CSS.escape(n.id)}"]`);
      const nw = Math.max(6, (el ? el.offsetWidth : 248) * scale);
      const nh = Math.max(3.5, (el ? el.offsetHeight : 58) * scale);
      ctx.fillStyle = n.type === "source" ? "#ff4d00"
        : n.type === "agent" || n.theme === "agent" ? "#16c47f"
        : n.theme === "audio" ? "#a855f7"
        : n.theme === "subtitles" ? "#3b82f6"
        : n.type === "note" || n.theme === "image" ? "#f5b700"
        : "#a1a1aa";
      ctx.fillRect(ox + n.x * scale, oy + n.y * scale, nw, nh);
    }

    const vp = $("viewport").getBoundingClientRect();
    const tl = screenToWorld(vp.left, vp.top);
    const br = screenToWorld(vp.right, vp.bottom);
    ctx.strokeStyle = "rgba(255,77,0,0.8)";
    ctx.fillStyle = "rgba(255,77,0,0.04)";
    ctx.lineWidth = 1.2;
    ctx.fillRect(ox + tl.x * scale, oy + tl.y * scale, (br.x - tl.x) * scale, (br.y - tl.y) * scale);
    ctx.strokeRect(ox + tl.x * scale, oy + tl.y * scale, (br.x - tl.x) * scale, (br.y - tl.y) * scale);
  }

  /* ---------- 60fps Infinite Camera-Synced Gravitational Dot Field ---------- */
  const gravity = (() => {
    const canvas = $("gravity-field");
    if (!canvas) return { shockAt: () => {}, pulseAt: () => {} };
    const ctx = canvas.getContext("2d");
    let w = 0, h = 0, dpr = 1;
    const pointer = { x: -9999, y: -9999, active: false };
    const waves = [];

    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = window.innerWidth; h = window.innerHeight;
      canvas.width = w * dpr; canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    resize();
    window.addEventListener("resize", resize);

    window.addEventListener("pointermove", (e) => {
      pointer.x = e.clientX; pointer.y = e.clientY; pointer.active = true;
      const pt = screenToWorld(e.clientX, e.clientY);
      const cr = $("coord-readout");
      if (cr) {
        const sx = (pt.x >= 0 ? "+" : "-") + String(Math.min(9999, Math.abs(Math.round(pt.x)))).padStart(4, "0");
        const sy = (pt.y >= 0 ? "+" : "-") + String(Math.min(9999, Math.abs(Math.round(pt.y)))).padStart(4, "0");
        cr.textContent = `X:${sx} Y:${sy} · ${Math.round(camera.zoom * 100)}%`;
      }
    });
    window.addEventListener("pointerleave", () => { pointer.active = false; });
    function pushWave(sx, sy, kind) { waves.push({ x: sx, y: sy, t: 0, kind }); }

    function frame() {
      ctx.clearRect(0, 0, w, h);
      const isDark = (document.documentElement.getAttribute("data-theme") || "dark") === "dark";
      const baseAlpha = isDark ? 0.16 : 0.14;
      const dotRgb = isDark ? "255,255,255" : "18,18,21";
      const accentRgb = isDark ? "255,77,0" : "230,68,0";
      const okRgb = "22,196,127";

      for (let i = waves.length - 1; i >= 0; i--) {
        waves[i].t += 0.03;
        if (waves[i].t >= 1) waves.splice(i, 1);
      }
      const step = Math.max(18, 36 * camera.zoom);
      const ox = ((camera.x % step) + step) % step;
      const oy = (((camera.y + 54) % step) + step) % step;

      for (let gx = ox; gx < w; gx += step) {
        for (let gy = oy; gy < h; gy += step) {
          let dx = 0, dy = 0, energy = 0, useOk = false;
          if (pointer.active) {
            const vx = pointer.x - gx, vy = pointer.y - gy, dist = Math.hypot(vx, vy);
            if (dist < 145 && dist > 1) {
              const pull = (1 - dist / 145) * 9;
              dx += (vx / dist) * pull; dy += (vy / dist) * pull;
              energy = Math.max(energy, 1 - dist / 145);
            }
          }
          for (const wave of waves) {
            const vx = gx - wave.x, vy = gy - wave.y, dist = Math.hypot(vx, vy) || 1;
            const radius = wave.t * 480, band = Math.abs(dist - radius);
            if (band < 80) {
              const force = (1 - band / 80) * (1 - wave.t);
              const dir = wave.kind === "implode" && wave.t < 0.25 ? -16 : 20;
              dx += (vx / dist) * force * dir; dy += (vy / dist) * force * dir;
              energy = Math.max(energy, force);
              if (wave.kind === "complete") useOk = true;
            }
          }
          const r = 1.1 + energy * 1.4;
          ctx.fillStyle = energy > 0.14
            ? `rgba(${useOk ? okRgb : accentRgb},${(baseAlpha + energy * 0.65).toFixed(2)})`
            : `rgba(${dotRgb},${baseAlpha})`;
          ctx.beginPath(); ctx.arc(gx + dx, gy + dy, r, 0, Math.PI * 2); ctx.fill();
        }
      }
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);

    return {
      shockAt: (wx, wy) => { const s = worldToScreen(wx, wy); pushWave(s.x, s.y, "implode"); },
      pulseAt: (wx, wy) => { const s = worldToScreen(wx, wy); pushWave(s.x, s.y, "complete"); },
    };
  })();

  /* ---------- Oscilloscope ---------- */
  const osc = (() => {
    const host = $("oscilloscope");
    const bars = [];
    if (host) {
      for (let i = 0; i < 20; i++) {
        const b = document.createElement("span"); b.className = "osc-bar"; host.appendChild(b); bars.push(b);
      }
    }
    let phase = 0;
    setInterval(() => {
      phase += 0.35;
      if (!host) return;
      host.classList.toggle("active", state.oscActive);
      bars.forEach((b, idx) => {
        const h = state.oscActive
          ? 4 + Math.round((Math.sin(phase + idx * 0.55) * 0.5 + 0.5) * 10)
          : 3 + Math.round((Math.sin(phase * 0.25 + idx * 0.4) * 0.5 + 0.5) * 2.5);
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

  /* ---------- Helpers ---------- */
  function toast(msg, kind = "") {
    const stack = $("toast-stack");
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    stack.appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .18s"; setTimeout(() => el.remove(), 200); }, 3200);
  }
  function esc(str) {
    return String(str ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
  }
  function fmtRelativeTime(iso) {
    if (!iso) return "";
    const diff = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
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
    return trimmed.replace(url, "").trim().length >= 3;
  }
  function shortHost(url) {
    try { return new URL(url).hostname.replace(/^www\./, ""); } catch (_) { return "link"; }
  }
  function hashUrl(str) {
    let h1 = 0x811c9dc5, h2 = 0x9e3779b9;
    for (let i = 0; i < str.length; i++) {
      const ch = str.charCodeAt(i);
      h1 = Math.imul(h1 ^ ch, 0x01000193);
      h2 = Math.imul(h2 ^ ch, 0x85ebca6b);
    }
    return ((h1 >>> 0).toString(36) + (h2 >>> 0).toString(36)).slice(0, 14);
  }
  function uid(prefix) { return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`; }

  function jobThreadLabel(job) {
    const req = job?.request;
    if (!req) return "OUTPUT";
    if (req.subtitles_only) return `SUBS · ${(req.subtitle_format || "VTT").toUpperCase()}`;
    if (req.mode === "audio" || req.audio_format) return `${(req.audio_format || "MP3").toUpperCase()} · ${req.audio_bitrate || "320k"}`;
    if (req.mode === "image" || (req.image_format && req.image_format !== "original")) return `IMG · ${(req.image_format || "ORIG").toUpperCase()}`;
    const muteTag = req.video_only ? " MUTED" : "";
    if (req.clip_start_seconds !== null && req.clip_start_seconds !== undefined) return `CLIP ${fmtDuration(req.clip_start_seconds)}–${fmtDuration(req.clip_end_seconds || 0)}${muteTag}`;
    if (req.video_quality && req.video_quality !== "best") return `${req.video_quality}p${muteTag}`;
    return `BEST${muteTag}`;
  }

  /* ---------- SaaS Tier & Headers ---------- */
  function apiHeaders(extra = {}) {
    const h = { ...extra };
    if (state.proKey) h["X-Neon-Pro-Key"] = state.proKey;
    if (state.byokKey) h["X-Gemini-Api-Key"] = state.byokKey;
    return h;
  }
  function isPro() { return state.tier?.tier === "pro"; }

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
    el.innerHTML = Array.from({ length: count }, (_, i) =>
      `<span class="led-seg ${i < lit ? (isOk ? "lit-ok" : "lit") : ""}"></span>`
    ).join("");
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
      $("tier-cta").textContent = t.has_llm_provider ? "Gemini Ready" : "Pro Active";
    } else {
      $("fuel-ai-count").textContent = `${t.agent_runs_remaining}/${t.agent_runs_limit_daily}`;
      $("fuel-dl-count").textContent = `${t.downloads_remaining}/${t.downloads_limit_hourly}`;
      renderLedStrip($("fuel-ai-leds"), t.agent_runs_remaining, t.agent_runs_limit_daily, false);
      renderLedStrip($("fuel-dl-leds"), t.downloads_remaining, t.downloads_limit_hourly, false);
      $("tier-cta").textContent = "Pro ↗";
    }
  }

  function openUpgradeModal(reasonText = "") {
    sfx.locked();
    const box = $("upgrade-reason-box");
    if (reasonText) { box.textContent = reasonText; box.classList.remove("hidden"); }
    else box.classList.add("hidden");
    $("pro-key-input").value = state.proKey;
    $("byok-key-input").value = state.byokKey;
    $("upgrade-modal").classList.remove("hidden");
  }

  /* =========================================================================
     LUCIDE ICON LIBRARY RENDERER (Zero Emojis · Instant SVG + Offline Safe)
     ========================================================================= */
  const LUCIDE_FALLBACK = {
    film: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M7 3v18"/><path d="M3 7.5h4"/><path d="M3 12h18"/><path d="M3 16.5h4"/><path d="M17 3v18"/><path d="M17 7.5h4"/><path d="M17 16.5h4"/>',
    headphones: '<path d="M3 14h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a9 9 0 0 1 18 0v7a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3"/>',
    subtitles: '<rect width="18" height="14" x="3" y="5" rx="2"/><path d="M7 15h4M15 15h2M7 11h2M13 11h4"/>',
    image: '<rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
    sparkles: '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/>',
    "file-down": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M12 18v-6"/><path d="m9 15 3 3 3-3"/>',
    "volume-2": '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>',
    "volume-x": '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="22" x2="16" y1="9" y2="15"/><line x1="16" x2="22" y1="9" y2="15"/>',
    scissors: '<circle cx="6" cy="6" r="3"/><path d="M8.12 8.12 12 12"/><path d="M20 4 8.12 15.88"/><circle cx="6" cy="18" r="3"/><path d="M14.8 14.8 20 20"/>',
    zap: '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
    monitor: '<rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/>',
    tv: '<rect width="20" height="15" x="2" y="7" rx="2" ry="2"/><polyline points="17 2 12 7 7 2"/>',
    smartphone: '<rect width="14" height="20" x="5" y="2" rx="2" ry="2"/><path d="M12 18h.01"/>',
    play: '<polygon points="6 3 20 12 6 21 6 3"/>',
    music: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    "disc-3": '<circle cx="12" cy="12" r="10"/><path d="M6 12c0-1.7.7-3.2 1.8-4.2"/><circle cx="12" cy="12" r="2"/><path d="M18 12c0 1.7-.7 3.2-1.8 4.2"/>',
    "sliders-horizontal": '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>',
    gem: '<path d="M6 3h12l4 6-10 13L2 9Z"/><path d="M11 3 8 9l4 13 4-13-3-6"/><path d="M2 9h20"/>',
    "audio-waveform": '<path d="M2 13a2 2 0 0 0 2-2V7a2 2 0 0 1 4 0v13a2 2 0 0 0 4 0V4a2 2 0 0 1 4 0v13a2 2 0 0 0 4 0v-4a2 2 0 0 1 2-2"/>',
    globe: '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    "file-text": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
    layers: '<path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
    camera: '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/>',
    terminal: '<polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/>',
  };

  function iconSvg(name, cls = "ico-sm") {
    const pascal = String(name || "sparkles").replace(/(^|-)([a-z0-9])/g, (_, __, c) => c.toUpperCase());
    if (window.lucide && window.lucide.icons && window.lucide.icons[pascal]) {
      const el = window.lucide.createElement(window.lucide.icons[pascal]);
      el.setAttribute("class", `lucide ${cls}`);
      return el.outerHTML;
    }
    const inner = LUCIDE_FALLBACK[name] || LUCIDE_FALLBACK.sparkles;
    return `<svg class="lucide ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
  }

  /* =========================================================================
     PURE CASCADING POSSIBILITY TREE SPEC (Every Click Blooms Next Paths!)
     ========================================================================= */

  function getLevel1Choices(sourceNode) {
    const p = sourceNode.probe;
    const ct = p?.content_type || "video";
    const hasThumb = !!p?.thumbnail?.url;
    const fmts = p?.available_formats || [];
    const maxH = Math.max(0, ...fmts.map((f) => f.height || 0));
    const subCount = (p?.subtitles || []).length;

    if (ct === "image" || ct === "gallery") {
      return [
        { key: "image", theme: "image", icon: "image", title: ct === "gallery" ? "Gallery Images" : "Image", sub: "Original · WebP · PNG · JPEG", edgeLabel: "IMAGE", nextStep: "image_format", delta: { mode: "image" } },
        { key: "agent", theme: "agent", icon: "sparkles", title: "AI Agent", sub: "Resize, slice or custom command", edgeLabel: "AGENT", nextStep: "agent_paths", delta: {} },
      ];
    }
    if (ct === "audio") {
      const list = [
        { key: "audio", theme: "audio", icon: "headphones", title: "Audio", sub: "MP3 · M4A · Opus · FLAC · WAV", edgeLabel: "AUDIO", nextStep: "audio_format", delta: { mode: "audio" } },
      ];
      if (hasThumb) list.push({ key: "image", theme: "image", icon: "image", title: "Cover Art", sub: "Extract & convert thumbnail", edgeLabel: "COVER", nextStep: "image_format", delta: { mode: "image", _useThumbnailUrl: true } });
      list.push({ key: "agent", theme: "agent", icon: "sparkles", title: "AI Agent", sub: "Trim, normalize or summarize", edgeLabel: "AGENT", nextStep: "agent_paths", delta: {} });
      return list;
    }
    if (ct === "document" || ct === "archive" || ct === "file") {
      return [
        { key: "direct", theme: "video", icon: "file-down", title: "Direct Download", sub: p?.size_bytes ? `${fmtBytes(p.size_bytes)} · Verified` : "Fast verified file", edgeLabel: "FILE", isLeaf: true, delta: { mode: "direct" } },
        { key: "agent", theme: "agent", icon: "sparkles", title: "AI Agent", sub: "Inspect or automate workflow", edgeLabel: "AGENT", nextStep: "agent_paths", delta: {} },
      ];
    }

    // Video / YouTube / Social / Stream
    const list = [
      { key: "video", theme: "video", icon: "film", title: "Video", sub: maxH ? `Up to ${maxH >= 2160 ? "4K" : maxH + "p"} · Audio or Muted` : "With Audio · Muted · Trim Clip", edgeLabel: "VIDEO", nextStep: "video_track", delta: { mode: "video" } },
      { key: "audio", theme: "audio", icon: "headphones", title: "Audio", sub: "MP3 · M4A · Opus · FLAC · WAV", edgeLabel: "AUDIO", nextStep: "audio_format", delta: { mode: "audio" } },
      { key: "subtitles", theme: "subtitles", icon: "subtitles", title: "Subtitles", sub: subCount ? `${subCount} track(s) · .txt · AI Brief` : "Auto captions · .txt · AI Brief", edgeLabel: "CAPTIONS", nextStep: "sub_lang", delta: { mode: "video", extract_subtitles: true, subtitles_only: true } },
    ];
    if (hasThumb) {
      list.push({ key: "image", theme: "image", icon: "image", title: "Thumbnail", sub: "HD Cover · WebP · PNG · JPEG", edgeLabel: "COVER", nextStep: "image_format", delta: { mode: "image", _useThumbnailUrl: true } });
    }
    list.push({ key: "agent", theme: "agent", icon: "sparkles", title: "AI Agent", sub: "Smart clips, audio & summaries", edgeLabel: "AGENT", nextStep: "agent_paths", delta: {} });
    return list;
  }

  function getNextChoicesFor(choiceNode) {
    const src = state.nodes.get(choiceNode.rootSourceId);
    const probe = src?.probe;
    const fmts = probe?.available_formats || [];
    const availH = new Set(fmts.map((f) => f.height).filter(Boolean));
    const step = choiceNode.nextStep;
    const acc = choiceNode.accumulated || {};

    if (step === "video_track") {
      return [
        { key: "with_audio", theme: "video", icon: "volume-2", title: "Video + Audio", sub: "Full video with stereo sound", edgeLabel: "WITH AUDIO", nextStep: "video_quality", delta: { video_only: false } },
        { key: "video_only", theme: "video", icon: "volume-x", title: "Video Only", sub: "Muted visual track for B-roll", edgeLabel: "MUTED", nextStep: "video_quality", delta: { video_only: true } },
        { key: "trim_video", theme: "video", icon: "scissors", title: "Trim Clip Range", sub: "Cut exact start → end seconds", edgeLabel: "CLIP", inputKind: "trim", isLeaf: true, delta: { video_only: false, video_quality: "1080", video_container: "mp4", video_codec: "h264" } },
      ];
    }

    if (step === "video_quality") {
      return [
        { key: "q_2160", theme: "video", icon: "zap", title: "4K · 2160p", sub: "Ultra-HD master stream", edgeLabel: "4K", pro: true, detected: availH.has(2160), nextStep: "video_container", delta: { video_quality: "2160" } },
        { key: "q_1080", theme: "video", icon: "monitor", title: "1080p · Full HD", sub: "Crisp standard high-def", edgeLabel: "1080p", detected: availH.has(1080), nextStep: "video_container", delta: { video_quality: "1080" } },
        { key: "q_720", theme: "video", icon: "tv", title: "720p · HD", sub: "Fast download · lighter file", edgeLabel: "720p", detected: availH.has(720), nextStep: "video_container", delta: { video_quality: "720" } },
        { key: "q_480", theme: "video", icon: "smartphone", title: "480p · Compact", sub: "Mobile friendly & quick", edgeLabel: "480p", detected: availH.has(480), nextStep: "video_container", delta: { video_quality: "480" } },
      ];
    }

    if (step === "video_container") {
      const qLbl = acc.video_quality === "2160" ? "4K" : `${acc.video_quality || "1080"}p`;
      const mLbl = acc.video_only ? "Muted" : "With Audio";
      return [
        { key: "c_mp4", theme: "video", icon: "download", title: "MP4 · H.264", sub: `${qLbl} ${mLbl} · Plays everywhere`, edgeLabel: "MP4", isLeaf: true, delta: { video_container: "mp4", video_codec: "h264", remux_faststart: true } },
        { key: "c_mkv", theme: "video", icon: "download", title: "MKV · Best Codec", sub: `${qLbl} ${mLbl} · Zero re-encode`, edgeLabel: "MKV", isLeaf: true, delta: { video_container: "mkv", video_codec: "auto" } },
        { key: "c_webm", theme: "video", icon: "download", title: "WebM · VP9/AV1", sub: `${qLbl} ${mLbl} · Smallest size`, edgeLabel: "WEBM", isLeaf: true, delta: { video_container: "webm", video_codec: "vp9" } },
        { key: "c_subs", theme: "video", icon: "subtitles", title: "MP4 + Embedded Subs", sub: `${qLbl} · Includes captions`, edgeLabel: "MP4+SUBS", isLeaf: true, delta: { video_container: "mp4", video_codec: "h264", extract_subtitles: true, embed_subtitles: true } },
      ];
    }

    if (step === "audio_format") {
      return [
        { key: "af_mp3", theme: "audio", icon: "music", title: "MP3", sub: "Universal stereo compatibility", edgeLabel: "MP3", nextStep: "audio_quality", delta: { audio_format: "mp3" } },
        { key: "af_m4a", theme: "audio", icon: "disc-3", title: "M4A · AAC", sub: "Native Apple & mobile audio", edgeLabel: "M4A", nextStep: "audio_quality", delta: { audio_format: "m4a" } },
        { key: "af_opus", theme: "audio", icon: "sliders-horizontal", title: "Opus", sub: "Ultra-clean web compression", edgeLabel: "OPUS", nextStep: "audio_quality", delta: { audio_format: "opus" } },
        { key: "af_flac", theme: "audio", icon: "gem", title: "FLAC · Lossless", sub: "Bit-perfect studio master", edgeLabel: "FLAC", pro: true, nextStep: "audio_lossless", delta: { audio_format: "flac", audio_bitrate: "auto" } },
        { key: "af_wav", theme: "audio", icon: "audio-waveform", title: "WAV · Raw PCM", sub: "Uncompressed DAW edit track", edgeLabel: "WAV", pro: true, nextStep: "audio_lossless", delta: { audio_format: "wav", audio_bitrate: "auto" } },
      ];
    }

    if (step === "audio_quality") {
      const fmt = (acc.audio_format || "mp3").toUpperCase();
      return [
        { key: "aq_320n", theme: "audio", icon: "audio-waveform", title: `320k · Loudnorm`, sub: `${fmt} · EBU R128 leveled volume`, edgeLabel: "320K+NORM", isLeaf: true, delta: { audio_bitrate: "320k", normalize_audio: true, embed_metadata: true } },
        { key: "aq_320", theme: "audio", icon: "download", title: `320k · Original`, sub: `${fmt} · Maximum bitrate stereo`, edgeLabel: "320K", isLeaf: true, delta: { audio_bitrate: "320k", normalize_audio: false, embed_metadata: true } },
        { key: "aq_192", theme: "audio", icon: "download", title: `192k · Balanced`, sub: `${fmt} · Fast & compact`, edgeLabel: "192K", isLeaf: true, delta: { audio_bitrate: "192k", normalize_audio: false, embed_metadata: true } },
        { key: "aq_trim", theme: "audio", icon: "scissors", title: "Trim Audio Range", sub: `Cut exact clip in ${fmt} 320k`, edgeLabel: "TRIM", inputKind: "trim", isLeaf: true, delta: { audio_bitrate: "320k", normalize_audio: true } },
      ];
    }

    if (step === "audio_lossless") {
      const fmt = (acc.audio_format || "flac").toUpperCase();
      return [
        { key: "al_norm", theme: "audio", icon: "audio-waveform", title: `${fmt} · Loudnorm`, sub: "Broadcast EBU R128 loudness", edgeLabel: "NORM", isLeaf: true, delta: { normalize_audio: true, embed_metadata: true } },
        { key: "al_raw", theme: "audio", icon: "download", title: `${fmt} · Untouched`, sub: "Original dynamic range", edgeLabel: "RAW", isLeaf: true, delta: { normalize_audio: false, embed_metadata: true } },
      ];
    }

    if (step === "sub_lang") {
      const detected = (probe?.subtitles || []).slice(0, 3);
      const langNodes = detected.length
        ? detected.map((s) => ({
            key: `lang_${s.language}`,
            theme: "subtitles",
            icon: "globe",
            title: `${s.language.toUpperCase()} Captions`,
            sub: s.is_auto_generated ? "Auto-generated track" : "Manual human track",
            edgeLabel: s.language.toUpperCase(),
            detected: true,
            nextStep: "sub_format",
            delta: { subtitle_languages: [s.language], subtitle_mode: "include_auto" },
          }))
        : [
            { key: "lang_en", theme: "subtitles", icon: "globe", title: "English (EN)", sub: "Manual or auto captions", edgeLabel: "EN", nextStep: "sub_format", delta: { subtitle_languages: ["en"], subtitle_mode: "include_auto" } },
            { key: "lang_hi", theme: "subtitles", icon: "globe", title: "Hindi (HI)", sub: "Manual or auto captions", edgeLabel: "HI", nextStep: "sub_format", delta: { subtitle_languages: ["hi"], subtitle_mode: "include_auto" } },
          ];
      langNodes.push({
        key: "sub_ai_brief",
        theme: "agent",
        icon: "sparkles",
        title: "AI Summary Brief",
        sub: "Instant key points via Neon Agent",
        edgeLabel: "AI BRIEF",
        isLeaf: true,
        delta: { _agentPrompt: "summarize the key points from" },
      });
      return langNodes;
    }

    if (step === "sub_format") {
      return [
        { key: "sf_txt", theme: "subtitles", icon: "file-text", title: "Clean .txt + VTT", sub: "Stripped readable transcript", edgeLabel: "TXT+VTT", isLeaf: true, delta: { subtitle_format: "vtt", generate_transcript: true } },
        { key: "sf_srt", theme: "subtitles", icon: "subtitles", title: "SubRip (.srt)", sub: "Ready for Premiere / DaVinci", edgeLabel: "SRT", isLeaf: true, delta: { subtitle_format: "srt", generate_transcript: true } },
        { key: "sf_vtt", theme: "subtitles", icon: "subtitles", title: "WebVTT (.vtt)", sub: "Standard HTML5 player track", edgeLabel: "VTT", isLeaf: true, delta: { subtitle_format: "vtt", generate_transcript: false } },
      ];
    }

    if (step === "image_format") {
      return [
        { key: "if_orig", theme: "image", icon: "download", title: "Original HD", sub: "Exact source resolution", edgeLabel: "ORIGINAL", isLeaf: true, delta: { image_format: "original" } },
        { key: "if_webp", theme: "image", icon: "zap", title: "WebP", sub: "Ultra-fast modern web image", edgeLabel: "WEBP", nextStep: "image_size", delta: { image_format: "webp", image_quality: 90 } },
        { key: "if_png", theme: "image", icon: "layers", title: "PNG · Lossless", sub: "Crisp lossless pixels", edgeLabel: "PNG", nextStep: "image_size", delta: { image_format: "png", image_quality: 95 } },
        { key: "if_jpg", theme: "image", icon: "camera", title: "JPEG", sub: "Universal photo format", edgeLabel: "JPEG", nextStep: "image_size", delta: { image_format: "jpg", image_quality: 90 } },
      ];
    }

    if (step === "image_size") {
      const fmt = (acc.image_format || "webp").toUpperCase();
      return [
        { key: "is_orig", theme: "image", icon: "download", title: `${fmt} · Full Size`, sub: "Keep original dimensions", edgeLabel: "FULL", isLeaf: true, delta: { image_max_width: null } },
        { key: "is_1920", theme: "image", icon: "monitor", title: `${fmt} · 1920px`, sub: "Full HD width resize", edgeLabel: "1920px", isLeaf: true, delta: { image_max_width: 1920 } },
        { key: "is_1280", theme: "image", icon: "tv", title: `${fmt} · 1280px`, sub: "Web / Thumbnail HD size", edgeLabel: "1280px", isLeaf: true, delta: { image_max_width: 1280 } },
        { key: "is_800", theme: "image", icon: "smartphone", title: `${fmt} · 800px`, sub: "Compact social preview", edgeLabel: "800px", isLeaf: true, delta: { image_max_width: 800 } },
      ];
    }

    if (step === "agent_paths") {
      return [
        { key: "ag_clip", theme: "agent", icon: "scissors", title: "15s Reel Clip", sub: "0:30–0:45 · 1080p H.264 MP4", edgeLabel: "AI CLIP", isLeaf: true, delta: { _agentPrompt: "clip 0:30 to 0:45 in 1080p h264 mp4" } },
        { key: "ag_mp3", theme: "agent", icon: "headphones", title: "Mastered 320k MP3", sub: "Extract audio + Loudnorm", edgeLabel: "AI AUDIO", isLeaf: true, delta: { _agentPrompt: "extract audio as 320k mp3 and normalize volume" } },
        { key: "ag_sum", theme: "agent", icon: "sparkles", title: "Summarize Media", sub: "Key takeaways & metadata", edgeLabel: "AI BRIEF", isLeaf: true, delta: { _agentPrompt: "summarize the key points from" } },
        { key: "ag_custom", theme: "agent", icon: "terminal", title: "Custom AI Command", sub: "Tell Neon Agent what to build", edgeLabel: "PROMPT", inputKind: "agent_prompt", isLeaf: true, delta: {} },
      ];
    }

    return [];
  }

  /* ---------- Tree Bloom, Pruning & Non-Overlapping Subtree Layout ---------- */
  function subtreeHasRealOutput(nodeId) {
    const n = state.nodes.get(nodeId);
    if (!n) return false;
    if (n.type === "result" || n.type === "agent" || n.type === "note") return true;
    const kids = state.edges.filter((e) => e.from === nodeId).map((e) => e.to);
    return kids.some((cid) => subtreeHasRealOutput(cid));
  }

  function pruneChoiceSubtree(nodeId) {
    if (subtreeHasRealOutput(nodeId)) return; // Never wipe branches with real downloads/agents!
    const kids = state.edges.filter((e) => e.from === nodeId).map((e) => e.to);
    for (const cid of kids) pruneChoiceSubtree(cid);
    const n = state.nodes.get(nodeId);
    if (n && n.type === "choice") {
      state.nodes.delete(nodeId);
      state.edges = state.edges.filter((e) => e.from !== nodeId && e.to !== nodeId);
      document.querySelector(`[data-node-id="${CSS.escape(nodeId)}"]`)?.remove();
    }
  }

  function collapseSiblingChoiceBranches(parentId, keepNodeId) {
    const siblingEdges = state.edges.filter((e) => e.from === parentId && e.to !== keepNodeId);
    for (const se of siblingEdges) {
      const sib = state.nodes.get(se.to);
      if (!sib || sib.type !== "choice") continue;
      if (sib.expanded) {
        const grandKids = state.edges.filter((e) => e.from === sib.id).map((e) => e.to);
        for (const gk of grandKids) pruneChoiceSubtree(gk);
        sib.expanded = false;
        se.active = false;
        renderNode(sib);
      }
    }
  }

  function spawnChoiceChildren(parentNode, specs, autoFocus = true, playSound = true) {
    const rootId = parentNode.type === "source" ? parentNode.id : parentNode.rootSourceId;
    const parentDepth = parentNode.type === "source" ? 0 : (parentNode.depth || 1);
    const parentAcc = parentNode.accumulated || {};
    const spawned = [];

    specs.forEach((spec, idx) => {
      const id = `ch_${parentNode.id}_${spec.key}`;
      if (state.nodes.has(id)) {
        spawned.push(state.nodes.get(id));
        return;
      }
      const node = {
        id,
        type: "choice",
        parentId: parentNode.id,
        rootSourceId: rootId,
        depth: parentDepth + 1,
        theme: spec.theme || parentNode.theme || "video",
        icon: spec.icon,
        title: spec.title,
        sub: spec.sub,
        pro: !!spec.pro,
        detected: !!spec.detected,
        isLeaf: !!spec.isLeaf,
        nextStep: spec.nextStep || null,
        inputKind: spec.inputKind || null,
        accumulated: { ...parentAcc, ...(spec.delta || {}) },
        expanded: false,
        demo: !!parentNode.demo,
        animDelayMs: idx * 32,
        clipStart: "0:30",
        clipEnd: "0:45",
        customPrompt: "clip 0:30 to 0:45 in 1080p mp4",
        x: parentNode.x + 360,
        y: parentNode.y,
      };
      state.nodes.set(id, node);
      renderNode(node);
      addEdge(parentNode.id, id, node.theme, false, spec.edgeLabel || spec.title.toUpperCase(), !!parentNode.demo);
      spawned.push(node);
    });

    if (parentNode.type === "choice") {
      parentNode.expanded = true;
      renderNode(parentNode);
    }
    refreshActiveLineageHighlight(parentNode.id);
    layoutTreeFromRoot(rootId);
    if (playSound && spawned.length) sfx.bloom(spawned.length);

    if (autoFocus && spawned.length) {
      const midChild = spawned[Math.floor(spawned.length / 2)];
      glideToWorld((parentNode.x + midChild.x) / 2 + 120, midChild.y + 35, Math.min(camera.zoom, 0.94));
    }
    return spawned;
  }

  function refreshActiveLineageHighlight(activeNodeId) {
    // Walk upward from activeNodeId to root Source to light up the chosen chain!
    const litNodes = new Set();
    const litEdges = new Set();
    let cur = activeNodeId;
    while (cur) {
      litNodes.add(cur);
      const inEdge = state.edges.find((e) => e.to === cur && state.nodes.get(e.from)?.type !== "note");
      if (!inEdge) break;
      litEdges.add(inEdge.id);
      cur = inEdge.from;
    }
    for (const n of state.nodes.values()) {
      if (n.type === "choice") {
        const el = document.querySelector(`[data-node-id="${CSS.escape(n.id)}"]`);
        if (el) el.classList.toggle("path-lit", litNodes.has(n.id));
      }
    }
    for (const e of state.edges) {
      const target = state.nodes.get(e.to);
      if (target?.type === "choice") e.active = litEdges.has(e.id);
    }
    renderThreads();
  }

  function layoutTreeFromRoot(rootId) {
    const root = state.nodes.get(rootId);
    if (!root) return;

    const colGap = 116;
    const rowGap = 14;
    const subtreeH = new Map();
    const v2 = new Set();
    function computeHeights(nodeId) {
      if (v2.has(nodeId)) return 0;
      v2.add(nodeId);
      const el = document.querySelector(`[data-node-id="${CSS.escape(nodeId)}"]`);
      const ownH = el ? el.offsetHeight : 64;
      const kids = state.edges
        .filter((e) => e.from === nodeId)
        .map((e) => e.to)
        .filter((cid) => state.nodes.has(cid) && !v2.has(cid));
      let total = ownH;
      if (kids.length) {
        const sum = kids.reduce((acc, cid) => acc + computeHeights(cid), 0) + rowGap * (kids.length - 1);
        total = Math.max(ownH, sum);
      }
      subtreeH.set(nodeId, { ownH, total, kids });
      return total;
    }

    computeHeights(rootId);
    const rootInfo = subtreeH.get(rootId);
    const rootCenterY = root.y + (rootInfo ? rootInfo.ownH / 2 : 120);

    function place(nodeId, xPos, topY) {
      const n = state.nodes.get(nodeId);
      const info = subtreeH.get(nodeId);
      if (!n || !info) return;
      const el = document.querySelector(`[data-node-id="${CSS.escape(nodeId)}"]`);
      const ownW = el ? el.offsetWidth : (n.type === "source" ? 344 : 248);

      if (nodeId !== rootId) {
        n.x = Math.round(xPos);
        n.y = Math.round(topY + (info.total - info.ownH) / 2);
        if (el) { el.style.left = `${n.x}px`; el.style.top = `${n.y}px`; }
      }

      if (!info.kids.length) return;
      const kidsTotal = info.kids.reduce((s, cid) => s + (subtreeH.get(cid)?.total || 64), 0) + rowGap * (info.kids.length - 1);
      const nodeCenterY = n.y + info.ownH / 2;
      let curY = nodeCenterY - kidsTotal / 2;
      for (const cid of info.kids) {
        const ch = subtreeH.get(cid)?.total || 64;
        place(cid, n.x + ownW + colGap, curY);
        curY += ch + rowGap;
      }
    }

    place(rootId, root.x, rootCenterY - (rootInfo?.total || 200) / 2);
    requestAnimationFrame(renderThreads);
  }

  /* ---------- Click Handler for Cascading Choice Nodes ---------- */
  function onChoiceClicked(choiceNode) {
    if (choiceNode.pro && !isPro()) {
      return openUpgradeModal(`${choiceNode.title} is a Pro feature (Guest includes up to 1080p & MP3 320k).`);
    }

    // If leaf node -> execute real download / AI Agent!
    if (choiceNode.isLeaf && !choiceNode.inputKind) {
      return executeChoiceLeaf(choiceNode);
    }

    // Otherwise -> bloom the NEXT column of possible paths!
    collapseSiblingChoiceBranches(choiceNode.parentId, choiceNode.id);

    // If already expanded, re-focus its children smoothly
    const existingKids = state.edges.filter((e) => e.from === choiceNode.id).map((e) => state.nodes.get(e.to)).filter(Boolean);
    if (existingKids.length) {
      refreshActiveLineageHighlight(choiceNode.id);
      const mid = existingKids[Math.floor(existingKids.length / 2)];
      glideToWorld((choiceNode.x + mid.x) / 2 + 100, mid.y + 30, Math.min(camera.zoom, 0.94));
      sfx.click();
      return;
    }

    const nextSpecs = getNextChoicesFor(choiceNode);
    if (nextSpecs.length) {
      spawnChoiceChildren(choiceNode, nextSpecs, true, true);
    }
  }

  async function executeChoiceLeaf(choiceNode) {
    promoteDemoTreeToReal();
    refreshActiveLineageHighlight(choiceNode.id);
    const src = state.nodes.get(choiceNode.rootSourceId);
    const acc = { ...(choiceNode.accumulated || {}) };

    // Handle inline AI Agent prompt leaf or preset AI prompt leaf
    if (acc._agentPrompt || choiceNode.inputKind === "agent_prompt") {
      const rawPrompt = choiceNode.inputKind === "agent_prompt" ? (choiceNode.customPrompt || "").trim() : acc._agentPrompt;
      if (!rawPrompt) return toast("Type an instruction for Neon Agent", "err");
      const fullPrompt = rawPrompt.includes("http") ? rawPrompt : `${rawPrompt} ${src.url}`;
      return launchAgentFromParent(choiceNode, fullPrompt, src.url);
    }

    // Handle inline Trim Clip leaf
    if (choiceNode.inputKind === "trim") {
      acc.clip_start_seconds = parseTime(choiceNode.clipStart);
      acc.clip_end_seconds = parseTime(choiceNode.clipEnd);
    }

    // Build clean DownloadRequest payload
    const targetUrl = acc._useThumbnailUrl && src?.probe?.thumbnail?.url ? src.probe.thumbnail.url : src.url;
    delete acc._useThumbnailUrl;
    delete acc._agentPrompt;

    const payload = { url: targetUrl, ...acc };
    if (targetUrl === src?.probe?.thumbnail?.url && src?.title) {
      payload.custom_filename_prefix = src.title.replace(/[^a-zA-Z0-9_-]+/g, "_").slice(0, 32);
    }
    state.lastApiPayload = payload;
    osc.set(true, `EXTRACTING · ${choiceNode.title.toUpperCase()}…`);
    sfx.capture();

    try {
      const res = await fetch("/v1/jobs", {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      const job = await res.json();
      if (res.status === 402) {
        openUpgradeModal(`${job.detail?.message} ${job.detail?.upgrade_hint || ""}`.trim());
        osc.set(false, "UPGRADE REQUIRED");
        return;
      }
      if (!res.ok) {
        toast(job.detail?.message || "Extraction rejected", "err");
        osc.set(false, "CANVAS READY");
        return;
      }
      refreshTier();
      const resNode = upsertResultNodeForJob(job, choiceNode, null, choiceNode.theme);
      layoutTreeFromRoot(choiceNode.rootSourceId);
      gravity.shockAt(resNode.x + 180, resNode.y + 80);
      glideToWorld((choiceNode.x + resNode.x) / 2 + 160, resNode.y + 90, Math.min(camera.zoom, 0.92));
      watchJob(job.job_id, resNode);
    } catch (err) {
      toast(`Network error: ${err.message}`, "err");
      osc.set(false, "CANVAS READY");
    }
  }

  /* ---------- Interactive Default Cascading Tree (`https://youtu.be/YW7Uc5_ixtM`) ---------- */
  function clearDemoGraph() {
    if (!state.hasDemoGraph) return;
    for (const [id, n] of Array.from(state.nodes.entries())) {
      if (n.demo) {
        if (n.type === "source" && n.url) state.sourceByUrl.delete(n.url);
        state.nodes.delete(id);
        document.querySelector(`[data-node-id="${CSS.escape(id)}"]`)?.remove();
      }
    }
    state.edges = state.edges.filter((e) => !e.demo);
    state.hasDemoGraph = false;
    $("sandbox-banner").classList.add("hidden");
    renderThreads();
    syncWorkspaceMeta();
  }

  function promoteDemoTreeToReal() {
    if (!state.hasDemoGraph) return;
    state.hasDemoGraph = false;
    $("sandbox-banner").classList.add("hidden");
    for (const n of state.nodes.values()) n.demo = false;
    for (const e of state.edges) e.demo = false;
    persistWorkspace();
    syncWorkspaceMeta();
  }

  function spawnDefaultShowcase() {
    clearDemoGraph();
    state.hasDemoGraph = true;
    $("sandbox-banner").classList.remove("hidden");

    const v = SHOWCASE_VIDEO;
    const src = {
      id: `src_${hashUrl(v.url)}`,
      type: "source",
      demo: true,
      url: v.url,
      title: v.title,
      x: -560,
      y: -135,
      probing: false,
      probe: {
        title: v.title,
        duration_seconds: v.duration,
        is_live: false,
        content_type: "video",
        source: { domain: "youtube.com" },
        author: { name: `${v.uploader} · ${v.views}` },
        thumbnail: { url: v.thumbnail },
        engine: { engine_used: "yt-dlp" },
        available_formats: [
          { format_id: "137", resolution: "1920x1080", height: 1080, fps: 30, container: "mp4", video_codec: "avc1" },
          { format_id: "136", resolution: "1280x720", height: 720, fps: 30, container: "mp4", video_codec: "avc1" },
          { format_id: "135", resolution: "854x480", height: 480, fps: 30, container: "mp4", video_codec: "avc1" },
          { format_id: "140", resolution: "audio only", container: "m4a", audio_codec: "mp4a" },
        ],
        subtitles: [{ language: "hi", is_auto_generated: true }, { language: "en", is_auto_generated: true }],
      },
    };

    state.nodes.set(src.id, src);
    state.sourceByUrl.set(v.url, src.id);
    renderNode(src);

    // Pre-bloom 2 intuitive hops (Source -> Video -> Video+Audio / Video Only / Trim) so the user instantly sees the tree mechanic!
    const col1 = spawnChoiceChildren(src, getLevel1Choices(src), false, false);
    const vidNode = col1.find((n) => n.id.endsWith("_video"));
    if (vidNode) {
      const col2 = spawnChoiceChildren(vidNode, getNextChoicesFor(vidNode), false, false);
      const withAud = col2.find((n) => n.id.endsWith("_with_audio"));
      if (withAud) {
        spawnChoiceChildren(withAud, getNextChoicesFor(withAud), false, false);
      }
    }
    setTimeout(() => {
      layoutTreeFromRoot(src.id);
      fitAllNodes();
    }, 65);
  }

  /* ---------- Spatial Graph Core: Threads & Layout ---------- */
  function syncWorkspaceMeta() {
    const realNodes = Array.from(state.nodes.values()).filter((n) => !n.demo && n.type !== "choice");
    const realEdges = state.edges.filter((e) => !e.demo);
    const totalHist = state.historyJobs.length + realNodes.filter((n) => n.type === "note" || n.type === "agent").length;
    $("history-count-chip").textContent = String(totalHist);
    $("history-stats").textContent = `${realNodes.length} on desk · ${state.historyJobs.length} saved · ${realEdges.length} threads`;
    drawMinimap();
  }

  function addEdge(fromId, toId, kind = "extract", active = false, label = "", isDemo = false, customLabel = false) {
    if (fromId === toId) return null;
    const exists = state.edges.find((e) => e.from === fromId && e.to === toId);
    if (exists) {
      exists.active = active;
      if (label) exists.label = label;
      renderThreads();
      return exists;
    }
    const edge = { id: uid("edge"), from: fromId, to: toId, kind, label: label || kind.toUpperCase(), active, demo: isDemo, customLabel };
    state.edges.push(edge);
    renderThreads();
    syncWorkspaceMeta();
    if (!isDemo) persistWorkspace();
    return edge;
  }

  function removeEdge(edgeId) {
    state.edges = state.edges.filter((e) => e.id !== edgeId);
    sfx.click();
    renderThreads();
    syncWorkspaceMeta();
    persistWorkspace();
  }

  function removeNode(nodeId) {
    const n = state.nodes.get(nodeId);
    if (!n) return;
    if (n.type === "source") {
      // Remove orphan choice children belonging to this source
      for (const [cid, cn] of Array.from(state.nodes.entries())) {
        if (cn.type === "choice" && cn.rootSourceId === nodeId && !subtreeHasRealOutput(cid)) {
          state.nodes.delete(cid);
          state.edges = state.edges.filter((e) => e.from !== cid && e.to !== cid);
          document.querySelector(`[data-node-id="${CSS.escape(cid)}"]`)?.remove();
        }
      }
      if (n.url) state.sourceByUrl.delete(n.url);
    }
    if (n.type === "result" && n.jobId) state.resultByJobId.delete(n.jobId);
    state.nodes.delete(nodeId);
    state.edges = state.edges.filter((e) => e.from !== nodeId && e.to !== nodeId);
    document.querySelector(`[data-node-id="${CSS.escape(nodeId)}"]`)?.remove();
    sfx.click();
    renderThreads();
    renderHistoryList();
    syncWorkspaceMeta();
    persistWorkspace();
  }

  function bezierPath(x1, y1, x2, y2) {
    const dist = Math.max(66, Math.abs(x2 - x1) * 0.46);
    return `M ${x1} ${y1} C ${x1 + dist} ${y1}, ${x2 - dist} ${y2}, ${x2} ${y2}`;
  }

  function bezierMidpoint(x1, y1, x2, y2) {
    const dist = Math.max(66, Math.abs(x2 - x1) * 0.46);
    const cx1 = x1 + dist, cy1 = y1, cx2 = x2 - dist, cy2 = y2;
    return {
      x: 0.125 * x1 + 0.375 * cx1 + 0.375 * cx2 + 0.125 * x2,
      y: 0.125 * y1 + 0.375 * cy1 + 0.375 * cy2 + 0.125 * y2,
    };
  }

  function getPortWorldCoords(nodeId, side) {
    const n = state.nodes.get(nodeId);
    if (!n) return null;
    const el = document.querySelector(`[data-node-id="${CSS.escape(nodeId)}"]`);
    const w = el ? el.offsetWidth : 248;
    const h = el ? el.offsetHeight : 64;
    return { x: side === "out" ? n.x + w : n.x, y: n.y + h / 2 };
  }

  function renderThreads() {
    const group = $("threads-group");
    if (!group) return;
    let html = "";
    for (const e of state.edges) {
      const p1 = getPortWorldCoords(e.from, "out");
      const p2 = getPortWorldCoords(e.to, "in");
      if (!p1 || !p2) continue;
      const d = bezierPath(p1.x, p1.y, p2.x, p2.y);
      const mid = bezierMidpoint(p1.x, p1.y, p2.x, p2.y);
      const text = (e.label || "THREAD").slice(0, 18);
      const pillW = Math.max(44, text.length * 5.7 + 14);

      html += `
        <g class="thread-group kind-${esc(e.kind)} ${e.active ? "is-active" : ""}" data-edge-id="${esc(e.id)}">
          <path class="thread-hit" d="${d}" />
          <path class="thread-halo kind-${esc(e.kind)}" d="${d}" />
          <path class="thread-base kind-${esc(e.kind)}" d="${d}" />
          <path class="thread-pulse kind-${esc(e.kind)} ${e.active ? "" : "idle"}" d="${d}" />
          <g class="thread-pill" data-rename-edge="${esc(e.id)}" transform="translate(${mid.x}, ${mid.y})">
            <rect class="thread-pill-bg" x="${(-pillW / 2).toFixed(1)}" y="-9" width="${pillW.toFixed(1)}" height="18" rx="9" />
            <text class="thread-pill-text">${esc(text)}</text>
          </g>
          <g class="thread-unplug" data-del-edge="${esc(e.id)}" transform="translate(${(mid.x + pillW / 2 + 10).toFixed(1)}, ${mid.y.toFixed(1)})">
            <circle r="8" />
            <text>×</text>
          </g>
        </g>
      `;
    }
    group.innerHTML = html;
    group.querySelectorAll("[data-del-edge]").forEach((g) => {
      g.onclick = (ev) => { ev.stopPropagation(); removeEdge(g.dataset.delEdge); };
    });
    group.querySelectorAll("[data-rename-edge]").forEach((g) => {
      g.ondblclick = (ev) => {
        ev.stopPropagation();
        const edge = state.edges.find((x) => x.id === g.dataset.renameEdge);
        if (!edge) return;
        const next = window.prompt("Rename thread label:", edge.label || "");
        if (next !== null && next.trim()) {
          edge.label = next.trim().toUpperCase().slice(0, 18);
          edge.customLabel = true;
          sfx.click();
          renderThreads();
          persistWorkspace();
        }
      };
    });
    drawMinimap();
  }

  function pickChildPosition(parentNode, offsetIndex = 0) {
    const el = document.querySelector(`[data-node-id="${CSS.escape(parentNode.id)}"]`);
    const pw = el ? el.offsetWidth : 260;
    return { x: parentNode.x + pw + 116, y: parentNode.y + offsetIndex * 180 };
  }

  function pickNewRootPosition() {
    const roots = Array.from(state.nodes.values()).filter((n) => n.type === "source" && !n.demo);
    if (!roots.length) {
      const c = getViewportCenterWorld();
      return { x: Math.round(c.x - 420), y: Math.round(c.y - 120) };
    }
    let maxY = -Infinity;
    for (const n of state.nodes.values()) if (!n.demo) maxY = Math.max(maxY, n.y);
    return { x: roots[0].x, y: maxY + 390 };
  }

  function tidyGraphLayout() {
    const roots = Array.from(state.nodes.values()).filter((n) => n.type === "source");
    let curY = -140;
    for (const r of roots) {
      r.x = -560;
      r.y = curY;
      const el = document.querySelector(`[data-node-id="${CSS.escape(r.id)}"]`);
      if (el) { el.style.left = `${r.x}px`; el.style.top = `${r.y}px`; }
      layoutTreeFromRoot(r.id);
      curY += 540;
    }
    sfx.click();
    renderThreads();
    persistWorkspace();
    fitAllNodes();
  }

  /* ---------- Node Renderer & Magnetic Port Wiring ---------- */
  function buildInlinePreviewHtml(dl) {
    if (!dl?.artifact_url) return "";
    const mime = (dl.mime_type || "").toLowerCase();
    const inlineUrl = `${dl.artifact_url}${dl.artifact_url.includes("?") ? "&" : "?"}inline=true`;
    if (mime.startsWith("video/")) return `<div class="inline-player"><video controls preload="metadata" src="${esc(inlineUrl)}"></video></div>`;
    if (mime.startsWith("audio/")) return `<div class="inline-player"><audio controls preload="metadata" src="${esc(inlineUrl)}"></audio></div>`;
    if (mime.startsWith("image/") && !mime.includes("svg")) return `<div class="inline-player"><img src="${esc(inlineUrl)}" alt="${esc(dl.filename)}" /></div>`;
    return "";
  }

  function renderNode(node) {
    const layer = $("nodes-layer");
    let el = document.querySelector(`[data-node-id="${CSS.escape(node.id)}"]`);
    if (!el) {
      el = document.createElement("article");
      el.dataset.nodeId = node.id;
      if (node.animDelayMs) el.style.animationDelay = `${node.animDelayMs}ms`;
      layer.appendChild(el);
    }

    const extraCls = node.type === "choice"
      ? `theme-${node.theme} ${node.isLeaf ? "is-leaf" : ""} ${node.inputKind ? "choice-interactive" : ""} ${node.expanded ? "path-lit" : ""}`
      : "";
    el.className = `node node-${node.type} ${extraCls} ${state.selectedNodeId === node.id ? "selected" : ""} ${node.collapsed ? "collapsed" : ""}`;
    el.style.left = `${node.x}px`;
    el.style.top = `${node.y}px`;

    let inner = "";

    if (node.type === "choice") {
      const ports = `
        <div class="port port-in" data-port="in" data-node="${esc(node.id)}"></div>
        <div class="port port-out" data-port="out" data-node="${esc(node.id)}"><span class="port-tip mono">Drag to link</span></div>
      `;
      const arrowHtml = node.inputKind
        ? ""
        : node.isLeaf
          ? `${iconSvg("play", "ico-xs")}<span>Run</span>`
          : (node.expanded ? `<span class="detected-dot" style="background:currentColor;"></span>` : iconSvg("chevron-right", "ico-xs"));
      let inlineExtra = "";

      if (node.inputKind === "trim") {
        inlineExtra = `
          <div class="choice-inline-body">
            <div class="cfg-row">
              <input type="text" class="cfg-input mono" data-trim-start value="${esc(node.clipStart)}" placeholder="0:00" />
              <span class="muted">→</span>
              <input type="text" class="cfg-input mono" data-trim-end value="${esc(node.clipEnd)}" placeholder="0:15" />
            </div>
            <button class="reactor-fire btn-sm" data-run-inline-leaf="${esc(node.id)}">${iconSvg("play", "ico-xs")}<span>Extract Clip</span></button>
          </div>
        `;
      } else if (node.inputKind === "agent_prompt") {
        inlineExtra = `
          <div class="choice-inline-body">
            <input type="text" class="cfg-input" data-agent-prompt value="${esc(node.customPrompt)}" placeholder="clip 0:30 to 0:45 in 1080p…" />
            <button class="reactor-fire btn-sm btn-agent" data-run-inline-leaf="${esc(node.id)}">${iconSvg("sparkles", "ico-xs")}<span>Run Agent</span></button>
          </div>
        `;
      }

      inner = `
        ${ports}
        <div class="choice-card" ${node.inputKind ? 'data-drag-handle="1"' : `data-choice-click="${esc(node.id)}"`}>
          <div class="choice-left">
            <div class="choice-icon">${iconSvg(node.icon)}</div>
            <div style="min-width:0;">
              <div class="choice-title">
                ${node.detected ? '<span class="detected-dot" title="Detected in stream"></span>' : ""}
                <span>${esc(node.title)}</span>
                ${node.pro ? '<span class="pro-tag mono">PRO</span>' : ""}
              </div>
              <div class="choice-sub mono">${esc(node.sub || "")}</div>
            </div>
          </div>
          ${arrowHtml ? `<span class="choice-arrow mono">${arrowHtml}</span>` : ""}
        </div>
        ${inlineExtra}
      `;
    } else if (node.type === "source") {
      const p = node.probe;
      const thumbHtml = p?.thumbnail?.url
        ? `<div class="node-thumb">
             <img src="${esc(p.thumbnail.url)}" alt="" />
             ${p.is_live || p.duration_seconds ? `<span class="dur-badge mono num">${p.is_live ? "LIVE" : fmtDuration(p.duration_seconds)}</span>` : ""}
           </div>` : "";
      const metaBits = p
        ? [p.source?.domain, p.author?.name, p.content_type?.toUpperCase(), p.size_bytes ? fmtBytes(p.size_bytes) : null, `via ${p.engine?.engine_used}`].filter(Boolean)
        : [shortHost(node.url), node.probing ? "Probing available paths…" : "Ready"];
      const caps = [];
      if (p) {
        if ((p.available_formats || []).length) caps.push(`${p.available_formats.length} streams`);
        if ((p.subtitles || []).length) caps.push(`${p.subtitles.length} captions`);
        if (p.thumbnail?.url) caps.push("HD Cover");
      }

      inner = `
        <div class="port port-in" data-port="in" data-node="${esc(node.id)}"></div>
        <div class="port port-out" data-port="out" data-node="${esc(node.id)}"><span class="port-tip mono">Drag to link or add note</span></div>
        <div class="node-head" data-drag-handle="1">
          <span class="node-type-pill mono"><span class="node-type-dot"></span>SOURCE · ${esc(shortHost(node.url).toUpperCase())}</span>
          <div class="node-head-right">
            <span class="status-pill mono ${node.probing ? "warn" : "ok"}">${node.probing ? "probing" : "ready"}</span>
            <button class="node-icon-btn" data-node-collapse="${esc(node.id)}">${node.collapsed ? "▸" : "▾"}</button>
            <button class="node-icon-btn" data-node-del="${esc(node.id)}">×</button>
          </div>
        </div>
        <div class="node-body">
          <h3 class="node-title">${esc(p?.title || node.title || node.url)}</h3>
          <div class="node-body-collapsible">
            ${thumbHtml}
            <div class="node-meta mono">${esc(metaBits.join(" · "))}</div>
            ${caps.length ? `<div class="node-caps">${caps.map((c) => `<span class="cap-tag mono">${esc(c)}</span>`).join("")}</div>` : ""}
          </div>
        </div>
      `;
    } else if (node.type === "agent") {
      const stepsHtml = (node.steps || []).map((s) =>
        `<div class="trace-item"><span class="step-tag mono ${esc(s.cls)}">${esc(s.tag)}</span><span>${esc(s.msg)}</span></div>`
      ).join("");
      inner = `
        <div class="port port-in" data-port="in" data-node="${esc(node.id)}"></div>
        <div class="port port-out" data-port="out" data-node="${esc(node.id)}"><span class="port-tip mono">Drag to link</span></div>
        <div class="node-head" data-drag-handle="1">
          <span class="node-type-pill mono"><span class="node-type-dot"></span>NEON AGENT ${node.provider ? `· ${esc(node.provider)}` : ""}</span>
          <div class="node-head-right">
            <span class="status-pill mono ${node.done ? "ok" : node.failed ? "err" : "warn"}">${node.done ? "done" : node.failed ? "halt" : "live"}</span>
            <button class="node-icon-btn" data-node-collapse="${esc(node.id)}">${node.collapsed ? "▸" : "▾"}</button>
            <button class="node-icon-btn" data-node-del="${esc(node.id)}">×</button>
          </div>
        </div>
        <div class="node-body">
          <div class="agent-prompt-quote">“${esc(node.prompt)}”</div>
          <div class="node-body-collapsible">
            <div class="trace-list">${stepsHtml}</div>
            ${node.answer ? `<div class="agent-answer-card">${esc(node.answer)}</div>` : ""}
          </div>
        </div>
      `;
    } else if (node.type === "result") {
      const j = node.job || {};
      const pct = j.progress?.percent ?? (j.status === "completed" ? 100 : 10);
      const isDone = j.status === "completed";
      const isErr = j.status === "failed" || j.status === "cancelled";
      const isActive = !isDone && !isErr;
      const dl = j.result?.download;
      const tr = j.result?.transcript;
      const ff = j.result?.metadata?.extra?.ffprobe;
      const title = dl?.filename || j.result?.title || j.url || "Extraction";

      const metaBits = [];
      if (isErr) metaBits.push(j.error || j.status);
      else if (isDone) metaBits.push(fmtBytes(dl?.size_bytes));
      else metaBits.push(j.progress?.message || j.status || "queued");
      if (isActive && j.progress?.speed_bps) metaBits.push(`${fmtBytes(j.progress.speed_bps)}/s`);
      if (ff) {
        const c = [ff.video_codec, ff.audio_codec, ff.width && ff.height ? `${ff.width}×${ff.height}` : null].filter(Boolean).join(" · ");
        if (c) metaBits.push(c);
      }
      if (j.created_at) metaBits.push(fmtRelativeTime(j.created_at));

      inner = `
        <div class="port port-in" data-port="in" data-node="${esc(node.id)}"></div>
        <div class="port port-out" data-port="out" data-node="${esc(node.id)}"><span class="port-tip mono">Drag to link or annotate</span></div>
        <div class="node-head" data-drag-handle="1">
          <span class="node-type-pill mono"><span class="node-type-dot"></span>ARTIFACT ${j.result?.engine?.engine_used ? `· ${esc(j.result.engine.engine_used.toUpperCase())}` : ""}</span>
          <div class="node-head-right">
            <span class="status-pill mono ${isDone ? "ok" : isErr ? "err" : "warn"}">${esc(j.status || "queued")}</span>
            <button class="node-icon-btn" data-node-collapse="${esc(node.id)}">${node.collapsed ? "▸" : "▾"}</button>
            <button class="node-icon-btn" data-node-del="${esc(node.id)}">×</button>
          </div>
        </div>
        <div class="node-body">
          <h3 class="node-title" title="${esc(j.url || "")}">${esc(title)}</h3>
          <div class="progress-rail"><div class="progress-bar ${isDone ? "done" : isErr ? "err" : ""}" style="width:${Math.min(100, Math.max(5, pct))}%"></div></div>
          <div class="node-meta mono">${esc(metaBits.filter(Boolean).join(" · "))}</div>
          <div class="node-body-collapsible">
            ${isDone ? buildInlinePreviewHtml(dl) : ""}
            <div class="node-actions">
              ${dl?.artifact_url ? `<a class="btn-link" href="${esc(dl.artifact_url)}" download="${esc(dl.filename || "download")}">Save File</a>` : ""}
              ${tr?.artifact_url ? `<a class="ghost-btn btn-xs" href="${esc(tr.artifact_url)}">Transcript .txt</a>` : ""}
              ${dl?.sha256 ? `<button class="ghost-btn btn-xs mono" data-sha="${esc(dl.sha256)}">SHA-256</button>` : ""}
              ${isActive ? `<button class="ghost-btn btn-xs" data-cancel-job="${esc(j.job_id)}">Cancel</button>` : ""}
            </div>
          </div>
        </div>
      `;
    } else if (node.type === "note") {
      inner = `
        <div class="port port-in" data-port="in" data-node="${esc(node.id)}"></div>
        <div class="port port-out" data-port="out" data-node="${esc(node.id)}"><span class="port-tip mono">Link note</span></div>
        <div class="node-head" data-drag-handle="1">
          <span class="node-type-pill mono"><span class="node-type-dot"></span>STUDIO NOTE</span>
          <div class="node-head-right"><button class="node-icon-btn" data-node-del="${esc(node.id)}">×</button></div>
        </div>
        <div class="node-body">
          <textarea class="note-textarea" data-note-input="${esc(node.id)}" placeholder="Write timestamps, edits, ideas…">${esc(node.text || "")}</textarea>
        </div>
      `;
    }

    el.innerHTML = inner;
    bindNodeElementEvents(el, node);
    requestAnimationFrame(renderThreads);
    syncWorkspaceMeta();
    renderHistoryList();
  }

  function findNearestInputPort(wx, wy, excludeNodeId) {
    let best = null;
    let bestDist = 42 / camera.zoom;
    for (const n of state.nodes.values()) {
      if (n.id === excludeNodeId) continue;
      const pt = getPortWorldCoords(n.id, "in");
      if (!pt) continue;
      const d = Math.hypot(pt.x - wx, pt.y - wy);
      if (d < bestDist) { bestDist = d; best = { nodeId: n.id, pt }; }
    }
    return best;
  }

  function bindNodeElementEvents(el, node) {
    el.onpointerdown = () => {
      state.selectedNodeId = node.id;
      document.querySelectorAll(".node.selected").forEach((x) => x.classList.remove("selected"));
      el.classList.add("selected");
    };

    // Drag handle (also allow dragging choice nodes if pointer moves >6px, else click blooms!)
    const dragTarget = el.querySelector("[data-drag-handle]") || (node.type === "choice" ? el.querySelector(".choice-card") : null);
    if (dragTarget) {
      dragTarget.onpointerdown = (e) => {
        if (e.target.closest("button") || e.target.closest("input") || e.target.closest(".port")) return;
        e.stopPropagation();
        const sx = e.clientX, sy = e.clientY, ox = node.x, oy = node.y;
        let moved = false;
        const onMove = (mv) => {
          if (Math.hypot(mv.clientX - sx, mv.clientY - sy) > 5) moved = true;
          if (!moved) return;
          node.x = ox + (mv.clientX - sx) / camera.zoom;
          node.y = oy + (mv.clientY - sy) / camera.zoom;
          el.style.left = `${node.x}px`;
          el.style.top = `${node.y}px`;
          renderThreads();
        };
        const onUp = () => {
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
          if (!moved && node.type === "choice" && !node.inputKind) {
            onChoiceClicked(node);
          } else if (moved && !node.demo) {
            persistWorkspace();
          }
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
      };
    }

    // Inline inputs on Trim / Agent Custom Prompt nodes
    const tStart = el.querySelector("[data-trim-start]");
    if (tStart) { tStart.onpointerdown = (e) => e.stopPropagation(); tStart.oninput = () => { node.clipStart = tStart.value; }; }
    const tEnd = el.querySelector("[data-trim-end]");
    if (tEnd) { tEnd.onpointerdown = (e) => e.stopPropagation(); tEnd.oninput = () => { node.clipEnd = tEnd.value; }; }
    const aPrompt = el.querySelector("[data-agent-prompt]");
    if (aPrompt) {
      aPrompt.onpointerdown = (e) => e.stopPropagation();
      aPrompt.oninput = () => { node.customPrompt = aPrompt.value; };
      aPrompt.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); executeChoiceLeaf(node); } };
    }
    el.querySelector("[data-run-inline-leaf]")?.addEventListener("click", (e) => { e.stopPropagation(); executeChoiceLeaf(node); });

    // Magnetic Patch Cable Dragging from Output Jack
    const outPort = el.querySelector(".port-out");
    if (outPort) {
      outPort.onpointerdown = (e) => {
        e.stopPropagation(); e.preventDefault(); sfx.click();
        $("viewport").classList.add("wiring");
        const draftGrp = $("draft-wire-group"), draft = $("draft-wire"), draftGlow = $("draft-wire-glow"), draftTip = $("draft-wire-tip");
        draftGrp.classList.remove("hidden");
        const start = getPortWorldCoords(node.id, "out");
        let snapped = null;
        const clearSnapCss = () => document.querySelectorAll(".port-in.snap-locked").forEach((p) => p.classList.remove("snap-locked"));

        const onMove = (mv) => {
          const cur = screenToWorld(mv.clientX, mv.clientY);
          const near = findNearestInputPort(cur.x, cur.y, node.id);
          clearSnapCss();
          if (near) {
            if (!snapped || snapped.nodeId !== near.nodeId) sfx.snap();
            snapped = near;
            document.querySelector(`[data-node="${CSS.escape(near.nodeId)}"][data-port="in"]`)?.classList.add("snap-locked");
          } else snapped = null;
          const end = snapped ? snapped.pt : cur;
          const d = bezierPath(start.x, start.y, end.x, end.y);
          draft.setAttribute("d", d); draftGlow.setAttribute("d", d);
          draftTip.setAttribute("cx", String(end.x)); draftTip.setAttribute("cy", String(end.y));
        };

        const onUp = (up) => {
          window.removeEventListener("pointermove", onMove); window.removeEventListener("pointerup", onUp);
          $("viewport").classList.remove("wiring"); draftGrp.classList.add("hidden"); clearSnapCss();
          if (snapped) {
            sfx.plug();
            addEdge(node.id, snapped.nodeId, node.type === "note" ? "note" : "custom", false, node.type === "note" ? "NOTE" : "LINKED", !!node.demo);
            toast("Patch cable connected!", "ok");
            return;
          }
          const targetEl = document.elementFromPoint(up.clientX, up.clientY);
          const targetNodeEl = targetEl?.closest("[data-node-id]");
          if (targetNodeEl && targetNodeEl.dataset.nodeId !== node.id) {
            sfx.plug();
            addEdge(node.id, targetNodeEl.dataset.nodeId, node.type === "note" ? "note" : "custom", false, "LINKED", !!node.demo);
            toast("Patch cable connected!", "ok");
          } else {
            const wPos = screenToWorld(up.clientX, up.clientY);
            openPatchMenu(node.id, up.clientX, up.clientY, wPos.x, wPos.y);
          }
        };
        window.addEventListener("pointermove", onMove); window.addEventListener("pointerup", onUp);
      };
    }

    // Standard buttons
    el.querySelector("[data-node-del]")?.addEventListener("click", (e) => { e.stopPropagation(); removeNode(node.id); });
    el.querySelector("[data-node-collapse]")?.addEventListener("click", (e) => {
      e.stopPropagation(); sfx.click(); node.collapsed = !node.collapsed; renderNode(node); if (!node.demo) persistWorkspace();
    });
    el.querySelector("[data-sha]")?.addEventListener("click", async (e) => {
      await navigator.clipboard.writeText(e.currentTarget.dataset.sha); sfx.click(); toast("SHA-256 copied", "ok");
    });
    el.querySelector("[data-cancel-job]")?.addEventListener("click", async (e) => {
      const id = e.currentTarget.dataset.cancelJob;
      const res = await fetch(`/v1/jobs/${id}/cancel`, { method: "POST" });
      if (res.ok) { node.job = await res.json(); renderNode(node); }
    });
    const noteArea = el.querySelector("[data-note-input]");
    if (noteArea) {
      noteArea.onpointerdown = (e) => e.stopPropagation();
      noteArea.oninput = () => { node.text = noteArea.value; if (!node.demo) persistWorkspace(); renderHistoryList(); };
    }
  }

  /* ---------- Patch Context Menu ---------- */
  function openPatchMenu(fromNodeId, sx, sy, wx, wy) {
    state.pendingPatch = { fromNodeId, wx, wy };
    const menu = $("patch-menu");
    menu.style.left = `${Math.min(window.innerWidth - 280, Math.max(12, sx))}px`;
    menu.style.top = `${Math.min(window.innerHeight - 280, Math.max(64, sy))}px`;
    menu.classList.remove("hidden");
  }
  function closePatchMenu() { $("patch-menu").classList.add("hidden"); state.pendingPatch = null; }

  function resolveSourceNodeFor(nodeId) {
    const n = state.nodes.get(nodeId);
    if (!n) return null;
    if (n.type === "source") return n;
    if (n.rootSourceId && state.nodes.has(n.rootSourceId)) return state.nodes.get(n.rootSourceId);
    const parentEdge = state.edges.find((e) => e.to === nodeId);
    return parentEdge ? resolveSourceNodeFor(parentEdge.from) : null;
  }

  /* ---------- Spawning Source & Probing Level-1 Possibilities ---------- */
  function spawnSourceWithPossibilities(url, dropCoords = null, autoProbe = true) {
    if (state.hasDemoGraph) clearDemoGraph();
    if (state.sourceByUrl.has(url)) {
      const existing = state.nodes.get(state.sourceByUrl.get(url));
      if (existing) {
        glideToWorld(existing.x + 260, existing.y + 110, 0.92);
        return existing;
      }
    }
    const id = `src_${hashUrl(url)}`;
    const pos = dropCoords || pickNewRootPosition();
    const node = { id, type: "source", url, x: pos.x, y: pos.y, probing: autoProbe, probe: null };
    state.nodes.set(id, node);
    state.sourceByUrl.set(url, id);
    renderNode(node);
    gravity.shockAt(node.x + 170, node.y + 90);
    glideToWorld(node.x + 280, node.y + 100, 0.94);
    if (autoProbe) probeAndBloomLevel1(node);
    return node;
  }

  async function probeAndBloomLevel1(node) {
    osc.set(true, "PROBING AVAILABLE PATHS…");
    try {
      const res = await fetch("/v1/probe", {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ url: node.url }),
      });
      const data = await res.json();
      if (res.status === 402) { openUpgradeModal(data.detail?.message); node.probing = false; renderNode(node); return; }
      if (res.ok) { node.probe = data; node.title = data.title; }
    } catch (_) {}
    node.probing = false;
    renderNode(node);
    // Bloom ONLY Level 1 paths cleanly (Video, Audio, Subtitles, Thumbnail, AI Agent) — click any one to bloom its sub-paths!
    spawnChoiceChildren(node, getLevel1Choices(node), true, true);
    osc.set(false, "CLICK ANY PATH TO EXPLORE");
  }

  function spawnNoteNode(wx, wy, text = "", linkFromId = null, customId = null) {
    if (!customId && state.hasDemoGraph && !linkFromId) clearDemoGraph();
    const id = customId || uid("note");
    const pos = (wx !== null && wy !== null) ? { x: wx, y: wy } : getViewportCenterWorld();
    const node = { id, type: "note", x: pos.x, y: pos.y, text };
    state.nodes.set(id, node);
    renderNode(node);
    if (linkFromId) addEdge(linkFromId, id, "note", false, "NOTE");
    if (!customId) { sfx.plug(); persistWorkspace(); }
    return node;
  }

  /* ---------- Jobs, Streaming Progress & AI Agent ---------- */
  function triggerBrowserSave(artifactUrl, filename) {
    const a = document.createElement("a");
    a.href = artifactUrl; a.download = filename || "download"; a.style.display = "none";
    document.body.appendChild(a); a.click(); setTimeout(() => a.remove(), 500);
  }

  function recordHistoryJob(job) {
    if (!job?.job_id) return;
    const idx = state.historyJobs.findIndex((x) => x.job_id === job.job_id);
    if (idx >= 0) state.historyJobs[idx] = job;
    else state.historyJobs.unshift(job);
  }

  function upsertResultNodeForJob(job, parentNode, forcedPos = null, kind = "extract") {
    recordHistoryJob(job);
    const existingId = state.resultByJobId.get(job.job_id);
    if (existingId && state.nodes.has(existingId)) {
      const node = state.nodes.get(existingId);
      node.job = job;
      renderNode(node);
      return node;
    }
    const id = `job_${job.job_id}`;
    const saved = state.savedPositions[id];
    const siblings = parentNode ? state.edges.filter((e) => e.from === parentNode.id).length : 0;
    const pos = forcedPos || saved || (parentNode ? pickChildPosition(parentNode, siblings) : pickNewRootPosition());
    const node = { id, type: "result", jobId: job.job_id, job, x: pos.x, y: pos.y, collapsed: !!saved?.collapsed };
    state.nodes.set(id, node);
    state.resultByJobId.set(job.job_id, id);
    renderNode(node);
    if (parentNode) {
      const isLive = !["completed", "failed", "cancelled"].includes(job.status);
      addEdge(parentNode.id, id, kind, isLive, jobThreadLabel(job));
    }
    return node;
  }

  function watchJob(jobId, resNode) {
    if (state.sse.has(jobId)) return;
    const es = new EventSource(`/v1/jobs/${jobId}/events`);
    state.sse.set(jobId, es);
    es.addEventListener("progress", async (ev) => {
      const p = JSON.parse(ev.data);
      resNode.job = { ...(resNode.job || {}), job_id: jobId, status: p.status, progress: p };
      renderNode(resNode);
      if (["completed", "failed", "cancelled"].includes(p.status)) {
        es.close(); state.sse.delete(jobId); osc.set(false, "EXTRACTION COMPLETE");
        const full = await (await fetch(`/v1/jobs/${jobId}`)).json();
        recordHistoryJob(full);
        resNode.job = full;
        for (const e of state.edges) if (e.to === resNode.id) e.active = false;
        renderNode(resNode);
        const dl = full.result?.download;
        if (p.status === "completed" && dl?.artifact_url) {
          sfx.complete(); gravity.pulseAt(resNode.x + 180, resNode.y + 100);
          if (state.autoSave && !state.autoSavedJobs.has(jobId)) {
            state.autoSavedJobs.add(jobId);
            triggerBrowserSave(dl.artifact_url, dl.filename);
            toast(`Saved ${dl.filename} to Downloads`, "ok");
          } else toast(`Ready: ${dl.filename}`, "ok");
        } else if (p.status === "failed") toast(full.error || "Extraction failed", "err");
      }
    });
    es.onerror = () => { es.close(); state.sse.delete(jobId); };
  }

  async function launchAgentFromParent(parentNode, promptText, explicitUrl = null) {
    const url = explicitUrl || parentNode.url || extractUrl(promptText);
    const siblings = state.edges.filter((e) => e.from === parentNode.id).length;
    const pos = pickChildPosition(parentNode, siblings);
    const cleanPrompt = promptText.replace(url || "", "").trim() || promptText;
    const shortTag = /clip|trim/i.test(cleanPrompt) ? "AI · CLIP" : /mp3|audio/i.test(cleanPrompt) ? "AI · AUDIO" : /summar|brief/i.test(cleanPrompt) ? "AI · BRIEF" : "AI AGENT";

    const agentNode = {
      id: uid("agent"), type: "agent", x: pos.x, y: pos.y, url,
      prompt: cleanPrompt, provider: "", steps: [], answer: "", done: false, failed: false,
    };
    state.nodes.set(agentNode.id, agentNode);
    renderNode(agentNode);
    const edgeIn = addEdge(parentNode.id, agentNode.id, "agent", true, shortTag);
    if (parentNode.rootSourceId) layoutTreeFromRoot(parentNode.rootSourceId);
    gravity.shockAt(agentNode.x + 180, agentNode.y + 80);
    glideToWorld((parentNode.x + agentNode.x) / 2 + 160, agentNode.y + 100, Math.min(camera.zoom, 0.9));
    sfx.capture();
    osc.set(true, "AGENT REASONING…");

    try {
      const res = await fetch("/v1/agent/run", {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ prompt: promptText, url }),
      });
      if (res.status === 402) {
        const err = await res.json();
        removeNode(agentNode.id);
        openUpgradeModal(`${err.detail?.message} ${err.detail?.upgrade_hint || ""}`.trim());
        osc.set(false, "UPGRADE REQUIRED");
        return;
      }
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        agentNode.failed = true;
        agentNode.steps.push({ tag: "ERR", cls: "err", msg: err.detail?.message || "Agent failed" });
        edgeIn.active = false;
        renderNode(agentNode);
        return;
      }

      refreshTier();
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let spawnedResNode = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() || "";

        for (const frame of frames) {
          let evtType = "message", dataStr = "";
          for (const line of frame.split("\n")) {
            if (line.startsWith("event:")) evtType = line.slice(6).trim();
            else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
          }
          if (!dataStr) continue;
          const ev = JSON.parse(dataStr);

          if (evtType === "thought") { sfx.step(); agentNode.steps.push({ tag: "THINK", cls: "", msg: ev.message }); renderNode(agentNode); }
          else if (evtType === "plan") { sfx.step(); if (ev.data?.provider) agentNode.provider = ev.data.provider; agentNode.steps.push({ tag: "PLAN", cls: "plan", msg: ev.message }); renderNode(agentNode); }
          else if (evtType === "tool") { sfx.step(); agentNode.steps.push({ tag: `TOOL·${ev.data?.tool || "exec"}`, cls: "tool", msg: ev.message }); renderNode(agentNode); }
          else if (evtType === "job" && ev.data?.job_id) {
            spawnedResNode = upsertResultNodeForJob(ev.data, agentNode, null, "agent");
            if (ev.data.status === "completed") {
              for (const e of state.edges) if (e.to === spawnedResNode.id) e.active = false;
              const dl = ev.data.result?.download;
              if (dl?.artifact_url && state.autoSave && !state.autoSavedJobs.has(ev.data.job_id)) {
                state.autoSavedJobs.add(ev.data.job_id);
                triggerBrowserSave(dl.artifact_url, dl.filename);
                toast(`Agent saved ${dl.filename}`, "ok");
              }
            }
          } else if (evtType === "progress" && ev.data?.job_id && spawnedResNode) {
            spawnedResNode.job = { ...(spawnedResNode.job || {}), status: ev.data.status, progress: ev.data };
            renderNode(spawnedResNode);
          } else if (evtType === "answer") {
            sfx.complete(); gravity.pulseAt(agentNode.x + 180, agentNode.y + 100);
            agentNode.done = true; agentNode.answer = ev.message; edgeIn.active = false; renderNode(agentNode);
          } else if (evtType === "error") {
            agentNode.failed = true; edgeIn.active = false;
            agentNode.steps.push({ tag: "HALT", cls: "err", msg: ev.message }); renderNode(agentNode);
            if (ev.data?.code === "ERR_PRO_FEATURE_REQUIRED" || ev.data?.code === "ERR_GUEST_QUOTA_EXCEEDED") {
              openUpgradeModal(`${ev.message} ${ev.data?.upgrade_hint || ""}`.trim());
            } else toast(ev.message, "err");
          }
        }
      }
      if (!agentNode.failed) agentNode.done = true;
      edgeIn.active = false;
      renderNode(agentNode);
    } catch (err) {
      agentNode.failed = true; edgeIn.active = false;
      agentNode.steps.push({ tag: "ERR", cls: "err", msg: err.message }); renderNode(agentNode);
    } finally {
      osc.set(false, "CANVAS READY");
      refreshTier();
    }
  }

  /* ---------- Dock Submission ---------- */
  function syncDockModeVisuals() {
    const raw = $("url-input").value.trim();
    const agent = isNaturalLanguageCommand(raw);
    $("reactor-shell").classList.toggle("agent-mode", agent);
    $("intent-label").textContent = agent ? "NEON AI AGENT" : "SPAWN POSSIBILITIES";
    $("fire-label").textContent = agent ? "Run Agent" : "Spawn";
    $("clear-url-btn").classList.toggle("hidden", !raw);
    const url = extractUrl(raw);
    if (url) { $("platform-tag").textContent = shortHost(url).toUpperCase(); $("platform-tag").classList.remove("hidden"); }
    else $("platform-tag").classList.add("hidden");
  }

  async function fireDockCommand() {
    const raw = $("url-input").value.trim();
    if (!raw) return toast("Paste a link or type an Agent command", "err");
    const url = extractUrl(raw);
    if (!url) return toast("Include a valid http(s) URL", "err");

    $("url-input").value = "";
    syncDockModeVisuals();
    sfx.capture();

    if (isNaturalLanguageCommand(raw)) {
      const src = spawnSourceWithPossibilities(url, null, false);
      await launchAgentFromParent(src, raw, url);
    } else {
      spawnSourceWithPossibilities(url, null, true);
    }
  }

  function spawnBatchTrees() {
    const urls = $("batch-input").value.split(/\r?\n/).map((u) => extractUrl(u)).filter(Boolean);
    if (!urls.length) return toast("Paste at least one valid URL", "err");
    if (state.hasDemoGraph) clearDemoGraph();
    $("dock-popover").classList.add("hidden"); $("dock-batch-btn").classList.remove("active"); state.batchOpen = false;
    sfx.capture();
    urls.slice(0, 6).forEach((u, idx) => spawnSourceWithPossibilities(u, { x: -560, y: idx * 460 - 150 }, true));
    toast(`Spawning ${Math.min(6, urls.length)} possibility trees…`, "ok");
  }

  /* ---------- Searchable History & Pin-to-Canvas Drawer ---------- */
  function pinJobToCanvas(job) {
    if (state.hasDemoGraph) clearDemoGraph();
    const src = spawnSourceWithPossibilities(job.url, null, false);
    if (job.result?.title && !src.title) {
      src.title = job.result.title;
      if (job.result.thumbnail) {
        src.probe = {
          title: job.result.title, thumbnail: job.result.thumbnail, duration_seconds: job.result.duration_seconds,
          source: job.result.source, engine: job.result.engine, content_type: job.result.content_type,
        };
      }
      renderNode(src);
    }
    const rn = upsertResultNodeForJob(job, src, null, "extract");
    sfx.plug();
    glideToWorld((src.x + rn.x) / 2 + 170, rn.y + 100, 0.92);
  }

  function renderHistoryList() {
    const host = $("history-list");
    if (!host) return;
    const q = ($("history-search")?.value || "").trim().toLowerCase();
    const filter = state.historyFilter;

    const rows = [];
    for (const n of state.nodes.values()) {
      if (n.demo || n.type === "source" || n.type === "result" || n.type === "choice") continue;
      rows.push({ kind: n.type, node: n });
    }
    for (const j of state.historyJobs) {
      const fn = (j.result?.download?.filename || "").toLowerCase();
      if (!q && (fn === "watch.html" || fn === "watch.htm")) continue;
      rows.push({ kind: "job", job: j, node: state.nodes.get(state.resultByJobId.get(j.job_id) || "") });
    }

    const filtered = rows.filter((r) => {
      if (filter === "completed" && !(r.kind === "job" && r.job.status === "completed")) return false;
      if (filter === "agent" && r.kind !== "agent") return false;
      if (filter === "note" && r.kind !== "note") return false;
      if (!q) return true;
      const hay = r.kind === "job"
        ? [r.job.result?.download?.filename, r.job.result?.title, r.job.url, r.job.result?.engine?.engine_used].filter(Boolean).join(" ").toLowerCase()
        : [r.node.prompt, r.node.text, r.node.url].filter(Boolean).join(" ").toLowerCase();
      return hay.includes(q);
    });

    if (!filtered.length) {
      host.innerHTML = `<div class="muted mono" style="padding:18px;text-align:center;font-size:11.5px;">No matching history yet</div>`;
      return;
    }

    host.innerHTML = filtered.map((r) => {
      if (r.kind === "job") {
        const j = r.job, dl = j.result?.download;
        const title = dl?.filename || j.result?.title || j.url;
        const sub = [fmtBytes(dl?.size_bytes), j.result?.engine?.engine_used, fmtRelativeTime(j.created_at)].filter(Boolean).join(" · ");
        const badgeCls = j.status === "completed" ? "ok" : j.status === "failed" ? "err" : "warn";
        return `
          <div class="history-item" data-pin-job="${esc(j.job_id)}">
            <div class="history-item-top"><span class="history-item-title">${esc(title)}</span><span class="status-pill mono ${badgeCls}">${esc(j.status)}</span></div>
            <div class="history-item-meta mono"><span>${esc(sub)}</span><span>${r.node ? "Glide →" : "+ Pin to Desk"}</span></div>
            ${dl?.artifact_url ? `<div class="history-item-actions"><a class="btn-link" href="${esc(dl.artifact_url)}" download="${esc(dl.filename || "download")}" onclick="event.stopPropagation()">Save File</a></div>` : ""}
          </div>
        `;
      }
      const n = r.node;
      const title = n.type === "agent" ? (n.prompt || "Agent run") : (n.text || "Empty note").slice(0, 60);
      const sub = n.type === "agent" ? (n.provider || "local-planner") : "Studio Note";
      return `
        <div class="history-item" data-focus-node="${esc(n.id)}">
          <div class="history-item-top"><span class="history-item-title">${esc(title)}</span><span class="status-pill mono ${n.done ? "ok" : ""}">${esc(n.type)}</span></div>
          <div class="history-item-meta mono"><span>${esc(sub)}</span><span>Glide →</span></div>
        </div>
      `;
    }).join("");

    host.querySelectorAll("[data-pin-job]").forEach((row) => {
      row.onclick = () => { const j = state.historyJobs.find((x) => x.job_id === row.dataset.pinJob); if (j) pinJobToCanvas(j); };
    });
    host.querySelectorAll("[data-focus-node]").forEach((row) => {
      row.onclick = () => {
        const n = state.nodes.get(row.dataset.focusNode);
        if (!n) return;
        sfx.click(); state.selectedNodeId = n.id;
        document.querySelectorAll(".node.selected").forEach((x) => x.classList.remove("selected"));
        document.querySelector(`[data-node-id="${CSS.escape(n.id)}"]`)?.classList.add("selected");
        glideToWorld(n.x + 175, n.y + 110, 1);
      };
    });
  }

  /* ---------- Engine Registry ---------- */
  async function loadEngines(refresh = false) {
    const res = await fetch(refresh ? "/v1/engines/refresh" : "/v1/engines", { method: refresh ? "POST" : "GET" });
    const list = await res.json();
    const installed = list.filter((e) => e.installation_status === "installed").length;
    $("engines-pill-text").textContent = `${installed}/${list.length}`;
    $("engines-modal-tbody").innerHTML = list.map((e) => {
      const ok = e.installation_status === "installed";
      return `<tr><td><strong>${esc(e.id)}</strong></td><td><span class="status-pill mono ${ok ? "ok" : "warn"}">${esc(ok ? "ready" : e.installation_status)}</span></td><td class="mono num">${esc(e.version)}</td><td class="mono num">Tier ${e.tier}</td><td class="muted">${esc(e.license)}</td></tr>`;
    }).join("");
    if (refresh) toast(`Engines refreshed (${installed}/${list.length} ready)`, "ok");
  }

  /* ---------- Viewport, Minimap & Event Wiring ---------- */
  function bindCanvasNavigation() {
    const vp = $("viewport");
    applyCamera();

    vp.addEventListener("pointerdown", (e) => {
      closePatchMenu();
      if (e.target.closest(".node") || e.target.closest("button")) return;
      vp.classList.add("panning");
      const sx = e.clientX, sy = e.clientY, ox = camera.x, oy = camera.y;
      const onMove = (mv) => { camera.x = ox + (mv.clientX - sx); camera.y = oy + (mv.clientY - sy); applyCamera(); };
      const onUp = () => { vp.classList.remove("panning"); window.removeEventListener("pointermove", onMove); window.removeEventListener("pointerup", onUp); };
      window.addEventListener("pointermove", onMove); window.addEventListener("pointerup", onUp);
    });

    vp.addEventListener("wheel", (e) => {
      if (e.target.closest(".trace-list") || e.target.closest(".agent-answer-card") || e.target.closest(".note-textarea")) return;
      e.preventDefault();
      if (e.ctrlKey || e.metaKey) {
        const rect = vp.getBoundingClientRect();
        const mx = e.clientX - rect.left, my = e.clientY - rect.top;
        const factor = Math.exp(-e.deltaY * 0.008);
        const nextZ = Math.max(0.3, Math.min(2.0, camera.zoom * factor));
        camera.x = mx - (mx - camera.x) * (nextZ / camera.zoom);
        camera.y = my - (my - camera.y) * (nextZ / camera.zoom);
        camera.zoom = nextZ;
      } else { camera.x -= e.deltaX; camera.y -= e.deltaY; }
      applyCamera();
    }, { passive: false });

    vp.addEventListener("dblclick", (e) => {
      if (e.target.closest(".node") || e.target.closest("button") || e.target.closest(".thread-pill")) return;
      const wPos = screenToWorld(e.clientX, e.clientY);
      spawnNoteNode(wPos.x - 135, wPos.y - 60, "");
    });

    const mm = $("minimap-shell");
    if (mm) {
      const jumpMinimap = (ev) => {
        if (!minimapMeta) return;
        const r = mm.getBoundingClientRect();
        const mx = (ev.clientX - r.left) * (168 / r.width), my = (ev.clientY - r.top) * (104 / r.height);
        const wx = (mx - minimapMeta.ox) / minimapMeta.scale, wy = (my - minimapMeta.oy) / minimapMeta.scale;
        const vpr = vp.getBoundingClientRect();
        camera.x = vpr.width / 2 - wx * camera.zoom;
        camera.y = vpr.height / 2 - wy * camera.zoom;
        applyCamera();
      };
      mm.onpointerdown = (e) => {
        e.stopPropagation(); jumpMinimap(e);
        const onMove = (mv) => jumpMinimap(mv);
        const onUp = () => { window.removeEventListener("pointermove", onMove); window.removeEventListener("pointerup", onUp); };
        window.addEventListener("pointermove", onMove); window.addEventListener("pointerup", onUp);
      };
    }

    $("zoom-in-btn").onclick = () => { sfx.click(); camera.zoom = Math.min(2, camera.zoom * 1.18); applyCamera(); };
    $("zoom-out-btn").onclick = () => { sfx.click(); camera.zoom = Math.max(0.3, camera.zoom / 1.18); applyCamera(); };
    $("zoom-reset-btn").onclick = () => { sfx.click(); camera.zoom = 1; applyCamera(); };
    $("fit-all-btn").onclick = () => { sfx.click(); fitAllNodes(); };
    $("add-note-btn").onclick = () => { const c = getViewportCenterWorld(); spawnNoteNode(c.x - 135, c.y - 60, ""); };
  }

  function bindAllEvents() {
    bindCanvasNavigation();

    const input = $("url-input");
    input.addEventListener("input", syncDockModeVisuals);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); fireDockCommand(); } });

    document.addEventListener("paste", (e) => {
      const active = document.activeElement;
      if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) return;
      const text = (e.clipboardData || window.clipboardData)?.getData("text");
      const url = extractUrl(text || "");
      if (url) {
        e.preventDefault(); sfx.capture();
        spawnSourceWithPossibilities(url, null, true);
        toast("Probing link & blooming available paths…", "ok");
      }
    });

    window.addEventListener("dragover", (e) => { e.preventDefault(); $("drop-overlay").classList.remove("hidden"); });
    window.addEventListener("dragleave", (e) => { if (e.relatedTarget === null) $("drop-overlay").classList.add("hidden"); });
    window.addEventListener("drop", (e) => {
      e.preventDefault(); $("drop-overlay").classList.add("hidden");
      const text = e.dataTransfer?.getData("text/uri-list") || e.dataTransfer?.getData("text/plain");
      const url = extractUrl(text || "");
      if (url) {
        sfx.capture();
        const wPos = screenToWorld(e.clientX, e.clientY);
        spawnSourceWithPossibilities(url, { x: Math.round(wPos.x - 170), y: Math.round(wPos.y - 100) }, true);
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closePatchMenu();
        $("upgrade-modal").classList.add("hidden"); $("engines-modal").classList.add("hidden");
        $("api-modal").classList.add("hidden"); $("history-drawer").classList.add("hidden");
        $("dock-popover").classList.add("hidden"); state.batchOpen = false;
      }
      if ((e.key === "/" || ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k")) &&
          document.activeElement?.tagName !== "INPUT" && document.activeElement?.tagName !== "TEXTAREA") {
        e.preventDefault(); sfx.click(); input.focus();
      }
    });

    document.querySelectorAll("[data-patch-action]").forEach((btn) => {
      btn.onclick = () => {
        const pending = state.pendingPatch;
        closePatchMenu();
        if (!pending) return;
        const act = btn.dataset.patchAction;
        const fromNode = state.nodes.get(pending.fromNodeId);
        if (!fromNode) return;
        if (act === "note") { spawnNoteNode(pending.wx, pending.wy - 50, "", fromNode.id); return; }
        const srcNode = resolveSourceNodeFor(fromNode.id);
        if (!srcNode) return toast("Drag from a Source or Path node first", "err");
        const l1 = getLevel1Choices(srcNode);
        const keyMap = { branch_video: "video", branch_audio: "audio", branch_subtitles: "subtitles", branch_image: "image", branch_agent: "agent" };
        const matched = l1.find((c) => c.key === keyMap[act]) || l1[0];
        if (matched) {
          const kids = spawnChoiceChildren(srcNode, [matched], true, true);
          if (kids[0]) onChoiceClicked(kids[0]);
        }
      };
    });

    $("clear-sandbox-btn").onclick = () => { sfx.click(); clearDemoGraph(); };
    $("show-demo-btn").onclick = () => { sfx.click(); $("history-drawer").classList.add("hidden"); spawnDefaultShowcase(); };

    $("dock-batch-btn").onclick = () => {
      sfx.click(); state.batchOpen = !state.batchOpen;
      $("dock-popover").classList.toggle("hidden", !state.batchOpen);
      $("dock-batch-btn").classList.toggle("active", state.batchOpen);
    };
    $("exit-batch-btn").onclick = () => $("dock-batch-btn").click();
    $("batch-input").oninput = () => { const n = $("batch-input").value.split(/\r?\n/).filter((x) => x.trim()).length; $("batch-count").textContent = `${n} link${n === 1 ? "" : "s"}`; };
    $("spawn-batch-btn").onclick = spawnBatchTrees;

    $("intent-mode-toggle").onclick = () => { sfx.click(); state.forceAgent = !state.forceAgent; syncDockModeVisuals(); };
    $("paste-btn").onclick = async () => {
      try { const text = await navigator.clipboard.readText(); if (text) { input.value = text.trim(); syncDockModeVisuals(); input.focus(); } }
      catch (_) { input.focus(); toast("Press ⌘V / Ctrl+V to paste"); }
    };
    $("clear-url-btn").onclick = () => { input.value = ""; syncDockModeVisuals(); input.focus(); };
    $("download-btn").onclick = fireDockCommand;

    $("history-toggle-btn").onclick = () => { sfx.click(); $("history-drawer").classList.toggle("hidden"); renderHistoryList(); };
    $("close-history-btn").onclick = () => $("history-drawer").classList.add("hidden");
    $("history-search").oninput = renderHistoryList;
    document.querySelectorAll("#history-filters [data-hfilter]").forEach((b) => {
      b.onclick = () => { sfx.click(); state.historyFilter = b.dataset.hfilter; document.querySelectorAll("#history-filters [data-hfilter]").forEach((x) => x.classList.toggle("active", x === b)); renderHistoryList(); };
    });
    $("tidy-graph-btn").onclick = tidyGraphLayout;
    $("reset-layout-btn").onclick = () => {
      localStorage.removeItem(LAYOUT_STORAGE_KEY); state.savedPositions = {};
      for (const id of Array.from(state.nodes.keys())) removeNode(id);
      $("sandbox-banner").classList.add("hidden");
      toast("Desk cleared", "ok");
    };

    // Pro / BYOK Upgrade Modal
    $("tier-pill").onclick = () => openUpgradeModal("");
    $("close-upgrade-modal").onclick = () => $("upgrade-modal").classList.add("hidden");
    $("tab-pro-key").onclick = () => { sfx.click(); $("tab-pro-key").classList.add("active"); $("tab-byok-key").classList.remove("active"); $("panel-pro-key").classList.remove("hidden"); $("panel-byok-key").classList.add("hidden"); };
    $("tab-byok-key").onclick = () => { sfx.click(); $("tab-byok-key").classList.add("active"); $("tab-pro-key").classList.remove("active"); $("panel-byok-key").classList.remove("hidden"); $("panel-pro-key").classList.add("hidden"); };
    $("fill-dev-pro-btn").onclick = () => { $("pro-key-input").value = "neon-pro-local"; $("save-pro-key-btn").click(); };
    $("save-pro-key-btn").onclick = async () => {
      state.proKey = $("pro-key-input").value.trim(); sessionStorage.setItem("neon_pro_key", state.proKey);
      await refreshTier();
      if (isPro()) { sfx.complete(); $("upgrade-modal").classList.add("hidden"); toast("Neon Pro unlocked!", "ok"); }
      else toast("Key not recognized (try neon-pro-local)", "err");
    };
    $("save-byok-key-btn").onclick = async () => {
      state.byokKey = $("byok-key-input").value.trim(); sessionStorage.setItem("neon_byok_key", state.byokKey);
      await refreshTier();
      if (isPro()) { sfx.complete(); $("upgrade-modal").classList.add("hidden"); toast("Gemini BYOK connected!", "ok"); }
    };
    $("clear-keys-btn").onclick = async () => {
      state.proKey = ""; state.byokKey = ""; sessionStorage.removeItem("neon_pro_key"); sessionStorage.removeItem("neon_byok_key");
      await refreshTier(); $("upgrade-modal").classList.add("hidden"); toast("Reverted to Guest Trial");
    };

    const syncAutosave = () => $("autosave-pill").classList.toggle("active", state.autoSave);
    syncAutosave();
    $("autosave-pill").onclick = () => { state.autoSave = !state.autoSave; localStorage.setItem("neon_autosave", String(state.autoSave)); syncAutosave(); sfx.click(); };

    const syncSfx = () => { $("sfx-btn").classList.toggle("active", state.sfxEnabled); $("sfx-text").textContent = state.sfxEnabled ? "SFX" : "Muted"; };
    syncSfx();
    $("sfx-btn").onclick = () => { state.sfxEnabled = !state.sfxEnabled; localStorage.setItem("neon_sfx", String(state.sfxEnabled)); syncSfx(); if (state.sfxEnabled) sfx.click(); };

    $("engines-modal-btn").onclick = () => { sfx.click(); $("engines-modal").classList.remove("hidden"); };
    $("close-engines-modal").onclick = () => $("engines-modal").classList.add("hidden");
    $("refresh-engines-btn").onclick = () => loadEngines(true);

    $("api-modal-btn").onclick = () => {
      sfx.click();
      const body = state.lastApiPayload || { url: SHOWCASE_VIDEO.url, mode: "video", video_quality: "1080", video_only: false, video_container: "mp4", video_codec: "h264" };
      const proHeader = state.proKey ? ` \\\n  -H "X-Neon-Pro-Key: ${state.proKey}"` : "";
      $("api-curl-pre").textContent = `curl -X POST "${window.location.origin}/v1/jobs" \\\n  -H "Content-Type: application/json"${proHeader} \\\n  -d '${JSON.stringify(body, null, 2)}'`;
      $("api-modal").classList.remove("hidden");
    };
    $("close-api-modal").onclick = () => $("api-modal").classList.add("hidden");
    $("copy-curl-btn").onclick = async () => { await navigator.clipboard.writeText($("api-curl-pre").textContent); sfx.click(); toast("cURL copied", "ok"); };
    $("copy-json-btn").onclick = async () => {
      const body = state.lastApiPayload || { url: SHOWCASE_VIDEO.url, mode: "video", video_quality: "1080" };
      await navigator.clipboard.writeText(JSON.stringify(body, null, 2)); sfx.click(); toast("JSON copied", "ok");
    };

    const applyTheme = (t) => { document.documentElement.setAttribute("data-theme", t); localStorage.setItem("neon_sync_theme", t); $("theme-text").textContent = t === "dark" ? "Light" : "Dark"; };
    applyTheme(localStorage.getItem("neon_sync_theme") || "dark");
    $("theme-btn").onclick = () => { sfx.click(); applyTheme((document.documentElement.getAttribute("data-theme") || "dark") === "dark" ? "light" : "dark"); };
  }

  /* ---------- Hydrate History Drawer & Launch Cascading Possibility Tree ---------- */
  async function hydrateFromBackend() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY) || "{}"); } catch (_) {}

    try {
      const jobs = await (await fetch("/v1/jobs?limit=40")).json();
      state.historyJobs = jobs;
      for (const j of jobs) state.autoSavedJobs.add(j.job_id);
    } catch (_) {}

    const pinnedJobIds = Object.keys(saved.positions || {}).filter((k) => k.startsWith("job_")).map((k) => k.slice(4));
    for (const jid of pinnedJobIds) {
      const j = state.historyJobs.find((x) => x.job_id === jid);
      if (j) pinJobToCanvas(j);
    }
    for (const note of (saved.notes || [])) spawnNoteNode(note.x, note.y, note.text, null, note.id);
    for (const edge of (saved.customEdges || [])) {
      if (state.nodes.has(edge.from) && state.nodes.has(edge.to)) {
        addEdge(edge.from, edge.to, edge.kind, false, edge.label || "", false, !!edge.customLabel);
      }
    }

    if (state.nodes.size === 0) {
      spawnDefaultShowcase();
    } else {
      setTimeout(() => { renderThreads(); fitAllNodes(); }, 60);
    }
    renderHistoryList();
    syncWorkspaceMeta();
  }

  /* ---------- Boot ---------- */
  (async () => {
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      window.lucide.createIcons();
    }
    bindAllEvents();
    await Promise.all([refreshTier(), loadEngines(false)]);
    await hydrateFromBackend();
  })();
})();
