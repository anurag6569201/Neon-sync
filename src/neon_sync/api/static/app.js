/* Neon Sync — Infinite Spatial Media Workbench, Live Patchbay Threads & Minimap Engine */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const URL_RE = /https?:\/\/[^\s<>"')]+/i;
  const LAYOUT_STORAGE_KEY = "neon_sync_spatial_layout_v2";
  const DEMO_VIDEO_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw";

  /* ---------- Application State ---------- */
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
    popoverOpen: false,
    autoSave: localStorage.getItem("neon_autosave") !== "false",
    sfxEnabled: localStorage.getItem("neon_sfx") !== "false",
    tier: null,
    proKey: sessionStorage.getItem("neon_pro_key") || "",
    byokKey: sessionStorage.getItem("neon_byok_key") || "",
    sse: new Map(),
    autoSavedJobs: new Set(),
    oscActive: false,
    historyFilter: "all",
    selectedNodeId: null,
    nodes: new Map(), // id -> { id, type: 'portal'|'source'|'agent'|'result'|'note', x, y, demo?: bool, collapsed?: bool, ... }
    edges: [],        // { id, from, to, kind, label, active?: bool, demo?: bool }
    sourceByUrl: new Map(),
    resultByJobId: new Map(),
    savedPositions: {},
    pendingPatch: null,
    hasDemoGraph: false,
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
      if (n.demo) continue;
      positions[n.id] = { x: Math.round(n.x), y: Math.round(n.y), collapsed: !!n.collapsed };
      if (n.type === "note") {
        notes.push({ id: n.id, x: Math.round(n.x), y: Math.round(n.y), text: n.text || "" });
      }
    }
    for (const e of state.edges) {
      if (e.demo) continue;
      if (e.kind === "custom" || e.kind === "note" || e.customLabel) {
        customEdges.push({ id: e.id, from: e.from, to: e.to, kind: e.kind, label: e.label, customLabel: !!e.customLabel });
      }
    }
    state.savedPositions = positions;
    try {
      localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify({ positions, notes, customEdges }));
    } catch (_) {}
  }

  /* ---------- Web Audio Tactile Micro-Synthesizer ---------- */
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
    function tone(freq, dur, type = "sine", gainVal = 0.036, slideTo = null) {
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
    return {
      x: (sx - vp.left - camera.x) / camera.zoom,
      y: (sy - vp.top - camera.y) / camera.zoom,
    };
  }

  function worldToScreen(wx, wy) {
    const vp = $("viewport").getBoundingClientRect();
    return {
      x: vp.left + camera.x + wx * camera.zoom,
      y: vp.top + camera.y + wy * camera.zoom,
    };
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
    const dur = 360;
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
      const nw = el ? el.offsetWidth : 350;
      const nh = el ? el.offsetHeight : 210;
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
    const spanX = Math.max(420, b.maxX - b.minX);
    const spanY = Math.max(260, b.maxY - b.minY);
    const z = Math.max(0.35, Math.min(1.0, Math.min((vp.width - 140) / spanX, (vp.height - 210) / spanY)));
    glideToWorld((b.minX + b.maxX) / 2, (b.minY + b.maxY) / 2 + 24, z);
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
    const bw = Math.max(900, (b.maxX - b.minX) + pad * 2);
    const bh = Math.max(560, (b.maxY - b.minY) + pad * 2);
    const scale = Math.min(mw / bw, mh / bh);
    const ox = (mw - bw * scale) / 2 - bx * scale;
    const oy = (mh - bh * scale) / 2 - by * scale;
    minimapMeta = { scale, ox, oy };

    // Draw threads
    ctx.lineWidth = 1;
    for (const e of state.edges) {
      const n1 = state.nodes.get(e.from);
      const n2 = state.nodes.get(e.to);
      if (!n1 || !n2) continue;
      ctx.strokeStyle = e.kind === "agent" ? "rgba(22,196,127,0.55)" : e.kind === "note" ? "rgba(245,183,0,0.55)" : "rgba(255,77,0,0.55)";
      ctx.beginPath();
      ctx.moveTo(ox + (n1.x + 170) * scale, oy + (n1.y + 90) * scale);
      ctx.lineTo(ox + (n2.x + 170) * scale, oy + (n2.y + 90) * scale);
      ctx.stroke();
    }

    // Draw nodes
    for (const n of state.nodes.values()) {
      const el = document.querySelector(`[data-node-id="${CSS.escape(n.id)}"]`);
      const nw = Math.max(8, (el ? el.offsetWidth : 350) * scale);
      const nh = Math.max(5, (el ? el.offsetHeight : 190) * scale);
      ctx.fillStyle = n.type === "agent" ? "#16c47f" : n.type === "note" ? "#f5b700" : n.type === "result" ? "#e4e4e7" : "#ff4d00";
      ctx.fillRect(ox + n.x * scale, oy + n.y * scale, nw, nh);
    }

    // Draw current viewport frame
    const vp = $("viewport").getBoundingClientRect();
    const tl = screenToWorld(vp.left, vp.top);
    const br = screenToWorld(vp.right, vp.bottom);
    ctx.strokeStyle = "rgba(255,77,0,0.9)";
    ctx.fillStyle = "rgba(255,77,0,0.08)";
    ctx.lineWidth = 1.2;
    const vx = ox + tl.x * scale;
    const vy = oy + tl.y * scale;
    const vw = (br.x - tl.x) * scale;
    const vh = (br.y - tl.y) * scale;
    ctx.fillRect(vx, vy, vw, vh);
    ctx.strokeRect(vx, vy, vw, vh);
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
            const vx = pointer.x - gx, vy = pointer.y - gy;
            const dist = Math.hypot(vx, vy);
            if (dist < 145 && dist > 1) {
              const pull = (1 - dist / 145) * 9;
              dx += (vx / dist) * pull;
              dy += (vy / dist) * pull;
              energy = Math.max(energy, 1 - dist / 145);
            }
          }
          for (const wave of waves) {
            const vx = gx - wave.x, vy = gy - wave.y;
            const dist = Math.hypot(vx, vy) || 1;
            const radius = wave.t * 480;
            const band = Math.abs(dist - radius);
            if (band < 80) {
              const force = (1 - band / 80) * (1 - wave.t);
              const dir = wave.kind === "implode" && wave.t < 0.25 ? -16 : 20;
              dx += (vx / dist) * force * dir;
              dy += (vy / dist) * force * dir;
              energy = Math.max(energy, force);
              if (wave.kind === "complete") useOk = true;
            }
          }
          const r = 1.1 + energy * 1.4;
          ctx.fillStyle = energy > 0.14
            ? `rgba(${useOk ? okRgb : accentRgb},${(baseAlpha + energy * 0.65).toFixed(2)})`
            : `rgba(${dotRgb},${baseAlpha})`;
          ctx.beginPath();
          ctx.arc(gx + dx, gy + dy, r, 0, Math.PI * 2);
          ctx.fill();
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
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transition = "opacity .18s";
      setTimeout(() => el.remove(), 200);
    }, 3200);
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
    if (!req) return "EXTRACT";
    if (req.mode === "audio" || req.audio_format) return `AUDIO · ${(req.audio_format || "MP3").toUpperCase()}`;
    if (req.mode === "image" || (req.image_format && req.image_format !== "original")) return `IMG · ${(req.image_format || "ORIG").toUpperCase()}`;
    if (req.clip_start_seconds !== null && req.clip_start_seconds !== undefined) return `CLIP · ${fmtDuration(req.clip_start_seconds)}–${fmtDuration(req.clip_end_seconds || 0)}`;
    if (req.video_quality && req.video_quality !== "best") return `VIDEO · ${req.video_quality}p`;
    return `${(req.video_container || "AUTO").toUpperCase()} · BEST`;
  }

  /* ---------- SaaS Tier & API Headers ---------- */
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
      $("tier-cta").textContent = t.has_llm_provider ? "Gemini ✓" : "Pro ✓";
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

  /* ---------- Interactive First-Run Sandbox Showcase (Default Outlook!) ---------- */
  function clearDemoGraph() {
    if (!state.hasDemoGraph) return;
    for (const [id, n] of Array.from(state.nodes.entries())) {
      if (n.demo) {
        state.nodes.delete(id);
        const el = document.querySelector(`[data-node-id="${CSS.escape(id)}"]`);
        if (el) el.remove();
      }
    }
    state.edges = state.edges.filter((e) => !e.demo);
    state.hasDemoGraph = false;
    $("sandbox-banner").classList.add("hidden");
    renderThreads();
    syncWorkspaceMeta();
  }

  function spawnDefaultShowcase() {
    clearDemoGraph();
    state.hasDemoGraph = true;
    $("sandbox-banner").classList.remove("hidden");

    const portal = {
      id: "demo_portal",
      type: "portal",
      demo: true,
      x: -440,
      y: -115,
      url: DEMO_VIDEO_URL,
    };
    const agent = {
      id: "demo_agent",
      type: "agent",
      demo: true,
      x: 65,
      y: -210,
      url: DEMO_VIDEO_URL,
      prompt: "clip 0:00 to 0:10 in 720p mp4 & summarize",
      provider: "local-planner",
      done: true,
      steps: [
        { tag: "PLAN", cls: "plan", msg: "Planned clip 0s–10s, 720p H.264 MP4 + summary." },
        { tag: "TOOL·probe_url", cls: "tool", msg: "Locked source metadata via yt-dlp." },
        { tag: "TOOL·execute_download", cls: "tool", msg: "Trimmed & remuxed via FFmpeg faststart." },
      ],
      answer: "Ready to run for real! Click “Try Live YouTube” on the left portal, or drag its orange jack anywhere onto the desk.",
    };
    const note = {
      id: "demo_note",
      type: "note",
      demo: true,
      x: 85,
      y: 120,
      text: "💡 Spatial Tip:\n• Drag any orange jack (●) onto empty canvas to chain clips, 320k MP3, or AI summaries.\n• Double-click any cable label to rename it.\n• Double-click empty desk to drop a note.",
    };

    state.nodes.set(portal.id, portal);
    state.nodes.set(agent.id, agent);
    state.nodes.set(note.id, note);
    renderNode(portal);
    renderNode(agent);
    renderNode(note);

    addEdge(portal.id, agent.id, "agent", true, "AI · CLIP + BRIEF", true);
    addEdge(portal.id, note.id, "note", false, "WORKFLOW TIP", true);
    setTimeout(() => { renderThreads(); fitAllNodes(); }, 45);
  }

  /* ---------- Spatial Graph Core: Nodes + Labeled Animated Bezier Threads ---------- */
  function syncWorkspaceMeta() {
    const realNodes = Array.from(state.nodes.values()).filter((n) => !n.demo);
    const realEdges = state.edges.filter((e) => !e.demo);
    $("history-count-chip").textContent = String(realNodes.length);
    $("history-stats").textContent = `${realNodes.length} nodes · ${realEdges.length} threads`;
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
    const defaultLabel = label || (kind === "agent" ? "AI AGENT" : kind === "note" ? "NOTE" : kind === "custom" ? "THREAD" : "EXTRACT");
    const edge = { id: uid("edge"), from: fromId, to: toId, kind, label: defaultLabel, active, demo: isDemo, customLabel };
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
    if (n.type === "source" && n.url) state.sourceByUrl.delete(n.url);
    if (n.type === "result" && n.jobId) state.resultByJobId.delete(n.jobId);
    state.nodes.delete(nodeId);
    state.edges = state.edges.filter((e) => e.from !== nodeId && e.to !== nodeId);
    const el = document.querySelector(`[data-node-id="${CSS.escape(nodeId)}"]`);
    if (el) el.remove();
    sfx.click();
    renderThreads();
    renderHistoryList();
    syncWorkspaceMeta();
    persistWorkspace();
  }

  function bezierPath(x1, y1, x2, y2) {
    const dist = Math.max(76, Math.abs(x2 - x1) * 0.46);
    return `M ${x1} ${y1} C ${x1 + dist} ${y1}, ${x2 - dist} ${y2}, ${x2} ${y2}`;
  }

  function bezierMidpoint(x1, y1, x2, y2) {
    const dist = Math.max(76, Math.abs(x2 - x1) * 0.46);
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
    const w = el ? el.offsetWidth : 348;
    const h = el ? el.offsetHeight : 180;
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
      const text = (e.label || "THREAD").slice(0, 22);
      const pillW = Math.max(52, text.length * 6.2 + 18);

      html += `
        <g class="thread-group kind-${esc(e.kind)} ${e.active ? "is-active" : ""}" data-edge-id="${esc(e.id)}">
          <path class="thread-hit" d="${d}" />
          <path class="thread-halo kind-${esc(e.kind)}" d="${d}" />
          <path class="thread-base kind-${esc(e.kind)}" d="${d}" />
          <path class="thread-pulse kind-${esc(e.kind)} ${e.active ? "" : "idle"}" d="${d}" />
          <g class="thread-pill" data-rename-edge="${esc(e.id)}" transform="translate(${mid.x}, ${mid.y})">
            <rect class="thread-pill-bg" x="${(-pillW / 2).toFixed(1)}" y="-10" width="${pillW.toFixed(1)}" height="20" rx="10" />
            <text class="thread-pill-text">${esc(text)}</text>
          </g>
          <g class="thread-unplug" data-del-edge="${esc(e.id)}" transform="translate(${(mid.x + pillW / 2 + 11).toFixed(1)}, ${mid.y.toFixed(1)})">
            <circle r="8.5" />
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
          edge.label = next.trim().toUpperCase().slice(0, 22);
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
    const pw = el ? el.offsetWidth : 350;
    return { x: parentNode.x + pw + 135, y: parentNode.y + offsetIndex * 265 };
  }

  function pickNewRootPosition() {
    const roots = Array.from(state.nodes.values()).filter((n) => n.type === "source" && !n.demo);
    if (!roots.length) {
      const c = getViewportCenterWorld();
      return { x: Math.round(c.x - 190), y: Math.round(c.y - 110) };
    }
    let maxY = -Infinity;
    for (const n of state.nodes.values()) if (!n.demo) maxY = Math.max(maxY, n.y);
    return { x: roots[0].x, y: maxY + 325 };
  }

  function tidyGraphLayout() {
    const incoming = new Map(), outgoing = new Map();
    for (const n of state.nodes.values()) { incoming.set(n.id, []); outgoing.set(n.id, []); }
    for (const e of state.edges) {
      if (outgoing.has(e.from) && incoming.has(e.to)) {
        outgoing.get(e.from).push(e.to);
        incoming.get(e.to).push(e.from);
      }
    }
    const roots = Array.from(state.nodes.values()).filter((n) => (incoming.get(n.id) || []).length === 0);
    const visited = new Set();
    let cursorY = 0;

    function layoutTree(nodeId, depth, baseY) {
      if (visited.has(nodeId)) return baseY;
      visited.add(nodeId);
      const n = state.nodes.get(nodeId);
      if (!n) return baseY;
      n.x = depth * 490;
      n.y = baseY;
      const el = document.querySelector(`[data-node-id="${CSS.escape(n.id)}"]`);
      if (el) { el.style.left = `${n.x}px`; el.style.top = `${n.y}px`; }
      const kids = (outgoing.get(nodeId) || []).filter((cid) => !visited.has(cid));
      if (!kids.length) return baseY + (el ? el.offsetHeight + 48 : 250);
      let childY = baseY;
      for (const cid of kids) childY = layoutTree(cid, depth + 1, childY);
      return Math.max(childY, baseY + (el ? el.offsetHeight + 48 : 250));
    }

    for (const r of roots) cursorY = layoutTree(r.id, 0, cursorY) + 36;
    for (const n of state.nodes.values()) if (!visited.has(n.id)) cursorY = layoutTree(n.id, 0, cursorY) + 36;

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
      layer.appendChild(el);
    }

    el.className = `node node-${node.type} ${state.selectedNodeId === node.id ? "selected" : ""} ${node.collapsed ? "collapsed" : ""}`;
    el.style.left = `${node.x}px`;
    el.style.top = `${node.y}px`;

    let inner = "";

    if (node.type === "portal") {
      inner = `
        <div class="port port-out" data-port="out" data-node="${esc(node.id)}"><span class="port-tip mono">Drag orange jack onto desk!</span></div>
        <div class="node-head" data-drag-handle="1">
          <span class="node-type-pill mono"><span class="node-type-dot"></span>START PORTAL · ZERO SIGNUP</span>
          <span class="status-pill mono ok">READY</span>
        </div>
        <div class="node-body">
          <h2 class="portal-heading">Drop any link.<br /><span class="accent">Wire it to anything.</span></h2>
          <p class="portal-sub">Paste a URL below (or hit <kbd>⌘V</kbd> anywhere) to spawn a live Media Cartridge, then drag threads to clip, convert, or command the AI Agent.</p>
          <div class="portal-input-wrap">
            <input type="text" class="portal-input" id="portal-url-input" placeholder="https://youtube.com/watch?v=…" />
            <button class="reactor-fire btn-sm" id="portal-go-btn">Go</button>
          </div>
          <div class="portal-demo-bar">
            <button class="tac-pill" id="portal-live-yt">▶ Try Live YouTube</button>
            <button class="tac-pill" id="portal-live-agent">🧠 Run Live AI Clip</button>
          </div>
        </div>
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
        : [shortHost(node.url), node.probing ? "Probing metadata…" : "Ready"];
      const fmts = p?.available_formats || [];
      const streamsHtml = node.streamsOpen && fmts.length > 1
        ? `<div class="node-streams"><table><thead><tr><th>ID</th><th>Res</th><th>Ext</th><th>Size</th></tr></thead><tbody>${
            fmts.slice(0, 20).map((f) => `<tr class="stream-row ${node.lockedFormatId === f.format_id ? "selected" : ""}" data-src-fmt="${esc(f.format_id)}"><td class="mono">${esc(f.format_id)}</td><td>${esc(f.resolution || f.label || "—")}</td><td class="mono">${esc(f.container)}</td><td class="mono num">${fmtBytes(f.size_bytes) || "—"}</td></tr>`).join("")
          }</tbody></table></div>` : "";

      inner = `
        <div class="port port-in" data-port="in" data-node="${esc(node.id)}"></div>
        <div class="port port-out" data-port="out" data-node="${esc(node.id)}"><span class="port-tip mono">Drag to link or chain action</span></div>
        <div class="node-head" data-drag-handle="1">
          <span class="node-type-pill mono"><span class="node-type-dot"></span>SOURCE · ${esc(shortHost(node.url).toUpperCase())}</span>
          <div class="node-head-right">
            <button class="node-icon-btn" data-node-collapse="${esc(node.id)}" title="Collapse / Expand">${node.collapsed ? "▸" : "▾"}</button>
            <button class="node-icon-btn" data-node-del="${esc(node.id)}" title="Remove">×</button>
          </div>
        </div>
        <div class="node-body">
          <h3 class="node-title">${esc(p?.title || node.title || node.url)}</h3>
          <div class="node-body-collapsible">
            ${thumbHtml}
            <div class="node-meta mono">${esc(metaBits.join(" · "))}</div>
            <div class="node-actions">
              <button class="tac-pill" data-src-extract="${esc(node.id)}">⚡ ${node.lockedFormatId ? `Extract #${esc(node.lockedFormatId)}` : "Extract"}</button>
              <button class="tac-pill" data-src-clip="${esc(node.id)}">✂️ Clip 15s</button>
              <button class="tac-pill" data-src-mp3="${esc(node.id)}">🎧 MP3</button>
              <button class="tac-pill" data-src-sum="${esc(node.id)}">🧠 Brief</button>
              ${fmts.length > 1 ? `<button class="ghost-btn btn-xs" data-src-streams="${esc(node.id)}">Streams (${fmts.length})</button>` : ""}
            </div>
            ${streamsHtml}
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
        <div class="port port-out" data-port="out" data-node="${esc(node.id)}"><span class="port-tip mono">Link note to any node</span></div>
        <div class="node-head" data-drag-handle="1">
          <span class="node-type-pill mono"><span class="node-type-dot"></span>STUDIO NOTE</span>
          <div class="node-head-right">
            <button class="node-icon-btn" data-node-del="${esc(node.id)}">×</button>
          </div>
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
    let bestDist = 42 / camera.zoom; // 42px magnetic snap radius!
    for (const n of state.nodes.values()) {
      if (n.id === excludeNodeId || n.type === "portal") continue;
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

    const head = el.querySelector("[data-drag-handle]");
    if (head) {
      head.onpointerdown = (e) => {
        if (e.target.closest("button")) return;
        e.stopPropagation();
        const sx = e.clientX, sy = e.clientY, ox = node.x, oy = node.y;
        const onMove = (mv) => {
          node.x = ox + (mv.clientX - sx) / camera.zoom;
          node.y = oy + (mv.clientY - sy) / camera.zoom;
          el.style.left = `${node.x}px`;
          el.style.top = `${node.y}px`;
          renderThreads();
        };
        const onUp = () => {
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
          if (!node.demo) persistWorkspace();
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
      };
    }

    // Magnetic Patch Cable Dragging from Output Jack!
    const outPort = el.querySelector(".port-out");
    if (outPort) {
      outPort.onpointerdown = (e) => {
        e.stopPropagation();
        e.preventDefault();
        sfx.click();
        $("viewport").classList.add("wiring");
        const draftGrp = $("draft-wire-group");
        const draft = $("draft-wire");
        const draftGlow = $("draft-wire-glow");
        const draftTip = $("draft-wire-tip");
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
            const pEl = document.querySelector(`[data-node="${CSS.escape(near.nodeId)}"][data-port="in"]`);
            if (pEl) pEl.classList.add("snap-locked");
          } else {
            snapped = null;
          }
          const end = snapped ? snapped.pt : cur;
          const d = bezierPath(start.x, start.y, end.x, end.y);
          draft.setAttribute("d", d);
          draftGlow.setAttribute("d", d);
          draftTip.setAttribute("cx", String(end.x));
          draftTip.setAttribute("cy", String(end.y));
        };

        const onUp = (up) => {
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
          $("viewport").classList.remove("wiring");
          draftGrp.classList.add("hidden");
          clearSnapCss();

          if (snapped) {
            sfx.plug();
            addEdge(node.id, snapped.nodeId, node.type === "note" ? "note" : "custom", false, node.type === "note" ? "NOTE" : "LINKED", !!(node.demo));
            toast("Patch cable connected!", "ok");
            return;
          }

          const targetEl = document.elementFromPoint(up.clientX, up.clientY);
          const targetNodeEl = targetEl?.closest("[data-node-id]");
          if (targetNodeEl && targetNodeEl.dataset.nodeId !== node.id) {
            sfx.plug();
            addEdge(node.id, targetNodeEl.dataset.nodeId, node.type === "note" ? "note" : "custom", false, "LINKED", !!(node.demo));
            toast("Patch cable connected!", "ok");
          } else {
            const wPos = screenToWorld(up.clientX, up.clientY);
            openPatchMenu(node.id, up.clientX, up.clientY, wPos.x, wPos.y);
          }
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
      };
    }

    // Portal Node buttons
    if (node.type === "portal") {
      const pIn = el.querySelector("#portal-url-input");
      const goReal = (urlStr) => {
        const clean = extractUrl(urlStr);
        if (!clean) return toast("Paste a valid http(s) URL", "err");
        clearDemoGraph();
        const src = ensureSourceNode(clean, { x: -240, y: -110 }, true);
        glideToWorld(src.x + 170, src.y + 100, 1);
      };
      if (pIn) {
        pIn.onpointerdown = (e) => e.stopPropagation();
        pIn.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); goReal(pIn.value); } };
      }
      el.querySelector("#portal-go-btn")?.addEventListener("click", () => goReal(pIn?.value || ""));
      el.querySelector("#portal-live-yt")?.addEventListener("click", () => goReal(DEMO_VIDEO_URL));
      el.querySelector("#portal-live-agent")?.addEventListener("click", () => {
        clearDemoGraph();
        const src = ensureSourceNode(DEMO_VIDEO_URL, { x: -340, y: -110 }, true);
        launchAgentFromSource(src, `clip 0:00 to 0:10 in 720p mp4 ${DEMO_VIDEO_URL}`);
      });
    }

    // Standard Node buttons
    el.querySelector("[data-node-del]")?.addEventListener("click", (e) => { e.stopPropagation(); removeNode(node.id); });
    el.querySelector("[data-node-collapse]")?.addEventListener("click", (e) => {
      e.stopPropagation();
      sfx.click();
      node.collapsed = !node.collapsed;
      renderNode(node);
      if (!node.demo) persistWorkspace();
    });

    el.querySelector("[data-src-extract]")?.addEventListener("click", () => launchDirectExtraction(node));
    el.querySelector("[data-src-clip]")?.addEventListener("click", () => launchAgentFromSource(node, `clip 0:00 to 0:15 in 720p mp4 ${node.url}`));
    el.querySelector("[data-src-mp3]")?.addEventListener("click", () => launchAgentFromSource(node, `extract audio as 320k mp3 and normalize volume ${node.url}`));
    el.querySelector("[data-src-sum]")?.addEventListener("click", () => launchAgentFromSource(node, `summarize the key points from ${node.url}`));
    el.querySelector("[data-src-streams]")?.addEventListener("click", () => { sfx.click(); node.streamsOpen = !node.streamsOpen; renderNode(node); });
    el.querySelectorAll("[data-src-fmt]").forEach((tr) => {
      tr.onclick = () => {
        sfx.click();
        const fid = tr.dataset.srcFmt;
        node.lockedFormatId = node.lockedFormatId === fid ? null : fid;
        renderNode(node);
      };
    });

    el.querySelector("[data-sha]")?.addEventListener("click", async (e) => {
      await navigator.clipboard.writeText(e.currentTarget.dataset.sha);
      sfx.click();
      toast("SHA-256 copied", "ok");
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

  function resolveSourceUrlForNode(nodeId) {
    const n = state.nodes.get(nodeId);
    if (!n) return null;
    if (n.url) return n.url;
    if (n.job?.url) return n.job.url;
    const parentEdge = state.edges.find((e) => e.to === nodeId);
    return parentEdge ? resolveSourceUrlForNode(parentEdge.from) : null;
  }

  /* ---------- Spawning Real Source, Note, Agent & Result Nodes ---------- */
  function ensureSourceNode(url, dropCoords = null, autoProbe = true) {
    if (state.hasDemoGraph) clearDemoGraph();
    if (state.sourceByUrl.has(url)) {
      const existing = state.nodes.get(state.sourceByUrl.get(url));
      if (existing) return existing;
    }
    const id = `src_${hashUrl(url)}`;
    const saved = state.savedPositions[id];
    const pos = dropCoords || saved || pickNewRootPosition();
    const node = { id, type: "source", url, x: pos.x, y: pos.y, collapsed: !!saved?.collapsed, probing: autoProbe, probe: null, streamsOpen: false, lockedFormatId: null };
    state.nodes.set(id, node);
    state.sourceByUrl.set(url, id);
    renderNode(node);
    gravity.shockAt(node.x + 170, node.y + 90);
    if (autoProbe) probeSourceNode(node);
    return node;
  }

  async function probeSourceNode(node) {
    osc.set(true, "PROBING SIGNAL…");
    try {
      const res = await fetch("/v1/probe", {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ url: node.url, preferred_engine: $("preferred-engine").value || null, allow_browser_fallback: state.browserFallback }),
      });
      const data = await res.json();
      if (res.status === 402) { openUpgradeModal(data.detail?.message); node.probing = false; renderNode(node); return; }
      if (res.ok) { node.probe = data; node.title = data.title; }
    } catch (_) {}
    node.probing = false;
    renderNode(node);
    osc.set(false, "SIGNAL LOCKED");
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

  /* ---------- Build Download Payload ---------- */
  function buildPayload(url, lockedFormatId = null) {
    const fps = $("fps-limit").value;
    const rangeEnd = $("gallery-range-end").value.trim();
    const langs = $("subtitle-languages").value.split(",").map((x) => x.trim()).filter(Boolean);
    const p = {
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
    if (lockedFormatId) p.format_id = lockedFormatId;
    return p;
  }

  /* ---------- Direct Extraction & Streaming Agent Nodes ---------- */
  function triggerBrowserSave(artifactUrl, filename) {
    const a = document.createElement("a");
    a.href = artifactUrl;
    a.download = filename || "download";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 500);
  }

  function upsertResultNodeForJob(job, parentNode, forcedPos = null, kind = "extract") {
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

  async function launchDirectExtraction(sourceNode, forcedPos = null) {
    osc.set(true, "EXTRACTING…");
    sfx.capture();
    try {
      const res = await fetch("/v1/jobs", {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(buildPayload(sourceNode.url, sourceNode.lockedFormatId || null)),
      });
      const job = await res.json();
      if (res.status === 402) { openUpgradeModal(`${job.detail?.message} ${job.detail?.upgrade_hint || ""}`.trim()); osc.set(false, "UPGRADE REQUIRED"); return; }
      if (!res.ok) { toast(job.detail?.message || "Extraction rejected", "err"); osc.set(false, "CANVAS READY"); return; }
      refreshTier();
      const resNode = upsertResultNodeForJob(job, sourceNode, forcedPos, "extract");
      gravity.shockAt(resNode.x + 170, resNode.y + 80);
      glideToWorld((sourceNode.x + resNode.x) / 2 + 170, resNode.y + 100, Math.min(camera.zoom, 0.95));
      watchJob(job.job_id, resNode);
    } catch (err) {
      toast(`Network error: ${err.message}`, "err");
      osc.set(false, "CANVAS READY");
    }
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
        es.close();
        state.sse.delete(jobId);
        osc.set(false, "EXTRACTION COMPLETE");
        const full = await (await fetch(`/v1/jobs/${jobId}`)).json();
        resNode.job = full;
        for (const e of state.edges) if (e.to === resNode.id) e.active = false;
        renderNode(resNode);
        const dl = full.result?.download;
        if (p.status === "completed" && dl?.artifact_url) {
          sfx.complete();
          gravity.pulseAt(resNode.x + 180, resNode.y + 100);
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

  async function launchAgentFromSource(sourceNode, promptText, forcedPos = null) {
    const siblings = state.edges.filter((e) => e.from === sourceNode.id).length;
    const pos = forcedPos || pickChildPosition(sourceNode, siblings);
    const cleanPrompt = promptText.replace(sourceNode.url, "").trim() || promptText;
    const shortTag = /clip|trim/i.test(cleanPrompt) ? "AI · CLIP" : /mp3|audio/i.test(cleanPrompt) ? "AI · AUDIO" : /summar|brief/i.test(cleanPrompt) ? "AI · BRIEF" : /webp|jpg|png/i.test(cleanPrompt) ? "AI · IMAGE" : "AI AGENT";

    const agentNode = {
      id: uid("agent"), type: "agent", x: pos.x, y: pos.y, url: sourceNode.url,
      prompt: cleanPrompt, provider: "", steps: [], answer: "", done: false, failed: false,
    };
    state.nodes.set(agentNode.id, agentNode);
    renderNode(agentNode);
    const edgeIn = addEdge(sourceNode.id, agentNode.id, "agent", true, shortTag);
    gravity.shockAt(agentNode.x + 180, agentNode.y + 80);
    glideToWorld((sourceNode.x + agentNode.x) / 2 + 180, agentNode.y + 110, Math.min(camera.zoom, 0.92));
    sfx.capture();
    osc.set(true, "AGENT REASONING…");

    try {
      const res = await fetch("/v1/agent/run", {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ prompt: promptText, url: sourceNode.url }),
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
            sfx.complete();
            gravity.pulseAt(agentNode.x + 180, agentNode.y + 100);
            agentNode.done = true;
            agentNode.answer = ev.message;
            edgeIn.active = false;
            renderNode(agentNode);
          } else if (evtType === "error") {
            agentNode.failed = true;
            edgeIn.active = false;
            agentNode.steps.push({ tag: "HALT", cls: "err", msg: ev.message });
            renderNode(agentNode);
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
      agentNode.failed = true;
      edgeIn.active = false;
      agentNode.steps.push({ tag: "ERR", cls: "err", msg: err.message });
      renderNode(agentNode);
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
    $("intent-label").textContent = agent ? "NEON AI AGENT" : "DIRECT EXTRACT";
    $("fire-label").textContent = agent ? "Run Agent" : "Spawn & Extract";
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

    const sourceNode = ensureSourceNode(url, null, true);
    $("url-input").value = "";
    syncDockModeVisuals();
    if (isNaturalLanguageCommand(raw)) await launchAgentFromSource(sourceNode, raw);
    else await launchDirectExtraction(sourceNode);
  }

  async function fireBatchCommand() {
    const urls = $("batch-input").value.split(/\r?\n/).map((u) => u.trim()).filter(Boolean);
    if (!urls.length) return toast("Paste at least one link", "err");
    const base = buildPayload(urls[0]);
    delete base.url;
    const res = await fetch("/v1/jobs/batch", {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ urls, ...base }),
    });
    const list = await res.json();
    if (res.status === 402) return openUpgradeModal(`${list.detail?.message} ${list.detail?.upgrade_hint || ""}`.trim());
    if (!res.ok) return toast(list.detail?.message || "Batch rejected", "err");
    refreshTier();
    sfx.capture();
    for (const j of list) {
      const src = ensureSourceNode(j.url, null, false);
      const rn = upsertResultNodeForJob(j, src, null, "extract");
      watchJob(j.job_id, rn);
    }
    $("dock-popover").classList.add("hidden");
    state.popoverOpen = false;
    fitAllNodes();
    toast(`Spawned ${list.length} threaded extractions`, "ok");
  }

  /* ---------- Searchable History & Spatial Fly-To Drawer ---------- */
  function renderHistoryList() {
    const host = $("history-list");
    if (!host) return;
    const q = ($("history-search")?.value || "").trim().toLowerCase();
    const filter = state.historyFilter;

    const items = Array.from(state.nodes.values()).filter((n) => {
      if (n.demo) return false;
      if (filter === "completed" && !(n.type === "result" && n.job?.status === "completed")) return false;
      if (filter === "agent" && n.type !== "agent") return false;
      if (filter === "note" && n.type !== "note") return false;
      if (!q) return true;
      const hay = [n.title, n.url, n.prompt, n.text, n.job?.result?.download?.filename, n.job?.result?.title, n.job?.url].filter(Boolean).join(" ").toLowerCase();
      return hay.includes(q);
    });

    if (!items.length) {
      host.innerHTML = `<div class="muted mono" style="padding:18px;text-align:center;font-size:11.5px;">No saved nodes yet — paste a link to begin</div>`;
      return;
    }

    host.innerHTML = items.map((n) => {
      let label = n.type.toUpperCase(), title = n.title || n.url || "Node", sub = "", badgeCls = "";
      if (n.type === "result") {
        title = n.job?.result?.download?.filename || n.job?.result?.title || n.job?.url || "Job";
        sub = [fmtBytes(n.job?.result?.download?.size_bytes), fmtRelativeTime(n.job?.created_at)].filter(Boolean).join(" · ");
        label = n.job?.status || "job";
        badgeCls = n.job?.status === "completed" ? "ok" : n.job?.status === "failed" ? "err" : "warn";
      } else if (n.type === "agent") {
        title = n.prompt || "Agent run";
        sub = n.provider || "local-planner";
        badgeCls = n.done ? "ok" : "warn";
      } else if (n.type === "note") {
        title = (n.text || "Empty note").slice(0, 60);
        sub = "Studio Note";
      } else sub = shortHost(n.url);

      return `
        <div class="history-item" data-focus-node="${esc(n.id)}">
          <div class="history-item-top">
            <span class="history-item-title">${esc(title)}</span>
            <span class="status-pill mono ${badgeCls}">${esc(label)}</span>
          </div>
          <div class="history-item-meta mono">
            <span>${esc(sub)}</span>
            <span>Glide →</span>
          </div>
        </div>
      `;
    }).join("");

    host.querySelectorAll("[data-focus-node]").forEach((row) => {
      row.onclick = () => {
        const n = state.nodes.get(row.dataset.focusNode);
        if (!n) return;
        sfx.click();
        state.selectedNodeId = n.id;
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
    $("preferred-engine").innerHTML = `<option value="">Auto Router</option>` + list.filter((e) => e.id !== "ffmpeg").map((e) => `<option value="${esc(e.id)}">${esc(e.id)} (${esc(e.installation_status)})</option>`).join("");
    $("engines-modal-tbody").innerHTML = list.map((e) => {
      const ok = e.installation_status === "installed";
      return `<tr><td><strong>${esc(e.id)}</strong></td><td><span class="status-pill mono ${ok ? "ok" : "warn"}">${esc(ok ? "ready" : e.installation_status)}</span></td><td class="mono num">${esc(e.version)}</td><td class="mono num">Tier ${e.tier}</td><td class="muted">${esc(e.license)}</td></tr>`;
    }).join("");
    if (refresh) toast(`Engines refreshed (${installed}/${list.length} ready)`, "ok");
  }

  /* ---------- Viewport & Minimap Navigation ---------- */
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
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    });

    vp.addEventListener("wheel", (e) => {
      if (e.target.closest(".trace-list") || e.target.closest(".agent-answer-card") || e.target.closest(".note-textarea") || e.target.closest(".node-streams")) return;
      e.preventDefault();
      if (e.ctrlKey || e.metaKey) {
        const rect = vp.getBoundingClientRect();
        const mx = e.clientX - rect.left, my = e.clientY - rect.top;
        const factor = Math.exp(-e.deltaY * 0.008);
        const nextZ = Math.max(0.3, Math.min(2.0, camera.zoom * factor));
        camera.x = mx - (mx - camera.x) * (nextZ / camera.zoom);
        camera.y = my - (my - camera.y) * (nextZ / camera.zoom);
        camera.zoom = nextZ;
      } else {
        camera.x -= e.deltaX;
        camera.y -= e.deltaY;
      }
      applyCamera();
    }, { passive: false });

    vp.addEventListener("dblclick", (e) => {
      if (e.target.closest(".node") || e.target.closest("button") || e.target.closest(".thread-pill")) return;
      const wPos = screenToWorld(e.clientX, e.clientY);
      spawnNoteNode(wPos.x - 135, wPos.y - 60, "");
    });

    // Interactive Minimap click & drag navigation!
    const mm = $("minimap-shell");
    if (mm) {
      const jumpMinimap = (ev) => {
        if (!minimapMeta) return;
        const r = mm.getBoundingClientRect();
        const mx = (ev.clientX - r.left) * (168 / r.width);
        const my = (ev.clientY - r.top) * (104 / r.height);
        const wx = (mx - minimapMeta.ox) / minimapMeta.scale;
        const wy = (my - minimapMeta.oy) / minimapMeta.scale;
        const vpr = vp.getBoundingClientRect();
        camera.x = vpr.width / 2 - wx * camera.zoom;
        camera.y = vpr.height / 2 - wy * camera.zoom;
        applyCamera();
      };
      mm.onpointerdown = (e) => {
        e.stopPropagation();
        jumpMinimap(e);
        const onMove = (mv) => jumpMinimap(mv);
        const onUp = () => { window.removeEventListener("pointermove", onMove); window.removeEventListener("pointerup", onUp); };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
      };
    }

    $("zoom-in-btn").onclick = () => { sfx.click(); camera.zoom = Math.min(2, camera.zoom * 1.18); applyCamera(); };
    $("zoom-out-btn").onclick = () => { sfx.click(); camera.zoom = Math.max(0.3, camera.zoom / 1.18); applyCamera(); };
    $("zoom-reset-btn").onclick = () => { sfx.click(); camera.zoom = 1; applyCamera(); };
    $("fit-all-btn").onclick = () => { sfx.click(); fitAllNodes(); };
    $("add-note-btn").onclick = () => { const c = getViewportCenterWorld(); spawnNoteNode(c.x - 135, c.y - 60, ""); };
  }

  function setMode(mode) {
    state.mode = mode;
    sfx.click();
    document.querySelectorAll("#mode-group .seg").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    $("context-strip-video").classList.toggle("hidden", !(mode === "auto" || mode === "video"));
    $("context-strip-audio").classList.toggle("hidden", mode !== "audio");
    $("context-strip-image").classList.toggle("hidden", mode !== "image");
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
        e.preventDefault();
        sfx.capture();
        const c = getViewportCenterWorld();
        const node = ensureSourceNode(url, { x: Math.round(c.x - 174), y: Math.round(c.y - 105) }, true);
        glideToWorld(node.x + 174, node.y + 105, 1);
        toast("Captured onto the desk — drag its orange jack to chain!", "ok");
      }
    });

    window.addEventListener("dragover", (e) => { e.preventDefault(); $("drop-overlay").classList.remove("hidden"); });
    window.addEventListener("dragleave", (e) => { if (e.relatedTarget === null) $("drop-overlay").classList.add("hidden"); });
    window.addEventListener("drop", (e) => {
      e.preventDefault();
      $("drop-overlay").classList.add("hidden");
      const text = e.dataTransfer?.getData("text/uri-list") || e.dataTransfer?.getData("text/plain");
      const url = extractUrl(text || "");
      if (url) {
        sfx.capture();
        const wPos = screenToWorld(e.clientX, e.clientY);
        ensureSourceNode(url, { x: Math.round(wPos.x - 174), y: Math.round(wPos.y - 80) }, true);
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closePatchMenu();
        $("upgrade-modal").classList.add("hidden");
        $("engines-modal").classList.add("hidden");
        $("api-modal").classList.add("hidden");
        $("history-drawer").classList.add("hidden");
        $("dock-popover").classList.add("hidden");
        state.popoverOpen = false;
      }
      if ((e.key === "/" || ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k")) &&
          document.activeElement?.tagName !== "INPUT" && document.activeElement?.tagName !== "TEXTAREA") {
        e.preventDefault();
        sfx.click();
        input.focus();
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
        const url = resolveSourceUrlForNode(fromNode.id);
        if (!url) return toast("Link a media URL node first", "err");
        const srcNode = fromNode.type === "source" ? fromNode : ensureSourceNode(url, null, false);
        const dropPos = { x: Math.round(pending.wx), y: Math.round(pending.wy - 80) };
        if (act === "extract") launchDirectExtraction(srcNode, dropPos);
        else if (act === "agent_clip") launchAgentFromSource(srcNode, `clip 0:00 to 0:15 in 720p mp4 ${url}`, dropPos);
        else if (act === "agent_mp3") launchAgentFromSource(srcNode, `extract audio as 320k mp3 and normalize volume ${url}`, dropPos);
        else if (act === "agent_summary") launchAgentFromSource(srcNode, `summarize the key points from ${url}`, dropPos);
        else if (act === "agent_webp") launchAgentFromSource(srcNode, `convert image to webp 1280px ${url}`, dropPos);
      };
    });

    $("clear-sandbox-btn").onclick = () => { sfx.click(); clearDemoGraph(); };
    $("show-demo-btn").onclick = () => { sfx.click(); $("history-drawer").classList.add("hidden"); spawnDefaultShowcase(); };

    $("dock-settings-btn").onclick = () => {
      sfx.click();
      state.popoverOpen = !state.popoverOpen;
      $("dock-popover").classList.toggle("hidden", !state.popoverOpen);
      $("dock-settings-btn").classList.toggle("active", state.popoverOpen);
    };
    $("intent-mode-toggle").onclick = () => { sfx.click(); state.forceAgent = !state.forceAgent; syncDockModeVisuals(); };
    $("paste-btn").onclick = async () => {
      try { const text = await navigator.clipboard.readText(); if (text) { input.value = text.trim(); syncDockModeVisuals(); input.focus(); } }
      catch (_) { input.focus(); toast("Press ⌘V / Ctrl+V to paste"); }
    };
    $("clear-url-btn").onclick = () => { input.value = ""; syncDockModeVisuals(); input.focus(); };
    $("download-btn").onclick = fireDockCommand;
    $("download-batch-btn").onclick = fireBatchCommand;

    document.querySelectorAll("#mode-group .seg").forEach((b) => { b.onclick = () => setMode(b.dataset.mode); });
    document.querySelectorAll("#quality-pills [data-vq]").forEach((b) => {
      b.onclick = () => {
        const vq = b.dataset.vq;
        if ((vq === "2160" || vq === "1440") && !isPro()) return openUpgradeModal(`${vq === "2160" ? "4K" : "2K"} Ultra-HD requires Neon Pro.`);
        sfx.click();
        state.videoQuality = vq;
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
    document.querySelectorAll("#audio-fmt-pills [data-af]").forEach((b) => {
      b.onclick = () => {
        const af = b.dataset.af;
        if ((af === "flac" || af === "wav") && !isPro()) return openUpgradeModal(`Lossless ${af.toUpperCase()} requires Neon Pro.`);
        sfx.click();
        state.audioFormat = af;
        document.querySelectorAll("#audio-fmt-pills [data-af]").forEach((x) => x.classList.toggle("active", x === b));
      };
    });
    document.querySelectorAll("#audio-br-pills [data-ab]").forEach((b) => { b.onclick = () => { sfx.click(); state.audioBitrate = b.dataset.ab; document.querySelectorAll("#audio-br-pills [data-ab]").forEach((x) => x.classList.toggle("active", x === b)); }; });
    $("loudnorm-pill").onclick = () => { sfx.click(); state.loudnorm = !state.loudnorm; $("loudnorm-pill").classList.toggle("toggle-on", state.loudnorm); };
    document.querySelectorAll("#img-fmt-pills [data-if]").forEach((b) => { b.onclick = () => { sfx.click(); state.imageFormat = b.dataset.if; document.querySelectorAll("#img-fmt-pills [data-if]").forEach((x) => x.classList.toggle("active", x === b)); }; });
    document.querySelectorAll("#img-width-pills [data-iw]").forEach((b) => { b.onclick = () => { sfx.click(); state.imageMaxWidth = b.dataset.iw; document.querySelectorAll("#img-width-pills [data-iw]").forEach((x) => x.classList.toggle("active", x === b)); }; });
    $("browser-fallback-pill").onclick = () => {
      if (!isPro()) return openUpgradeModal("Tier-3 Playwright headless browser extraction is a Pro feature.");
      sfx.click();
      state.browserFallback = !state.browserFallback;
      $("browser-fallback-pill").classList.toggle("toggle-on", state.browserFallback);
    };
    $("clip-toggle-btn").onclick = () => { sfx.click(); state.clipEnabled = !state.clipEnabled; $("clip-toggle-btn").classList.toggle("toggle-on", state.clipEnabled); $("clip-box").classList.toggle("hidden", !state.clipEnabled); };
    $("subs-toggle-btn").onclick = () => { sfx.click(); state.subsEnabled = !state.subsEnabled; $("subs-toggle-btn").classList.toggle("toggle-on", state.subsEnabled); $("subs-box").classList.toggle("hidden", !state.subsEnabled); };
    $("embed-subs-pill").onclick = () => { sfx.click(); state.embedSubs = !state.embedSubs; $("embed-subs-pill").classList.toggle("toggle-on", state.embedSubs); };
    $("transcript-pill").onclick = () => { sfx.click(); state.generateTranscript = !state.generateTranscript; $("transcript-pill").classList.toggle("toggle-on", state.generateTranscript); };
    $("batch-toggle-btn").onclick = () => { sfx.click(); state.batchMode = !state.batchMode; $("batch-toggle-btn").classList.toggle("toggle-on", state.batchMode); $("batch-bar").classList.toggle("hidden", !state.batchMode); };
    $("exit-batch-btn").onclick = () => $("batch-toggle-btn").click();
    $("batch-input").oninput = () => { const n = $("batch-input").value.split(/\r?\n/).filter((x) => x.trim()).length; $("batch-count").textContent = `${n} link${n === 1 ? "" : "s"}`; };
    $("finetune-toggle-btn").onclick = () => { sfx.click(); state.finetuneOpen = !state.finetuneOpen; $("finetune-toggle-btn").classList.toggle("toggle-on", state.finetuneOpen); $("finetune-drawer").classList.toggle("hidden", !state.finetuneOpen); };
    $("image-quality").oninput = (e) => { $("img-q-label").textContent = `${e.target.value}%`; };

    $("history-toggle-btn").onclick = () => { sfx.click(); $("history-drawer").classList.toggle("hidden"); renderHistoryList(); };
    $("close-history-btn").onclick = () => $("history-drawer").classList.add("hidden");
    $("history-search").oninput = renderHistoryList;
    document.querySelectorAll("#history-filters [data-hfilter]").forEach((b) => {
      b.onclick = () => { sfx.click(); state.historyFilter = b.dataset.hfilter; document.querySelectorAll("#history-filters [data-hfilter]").forEach((x) => x.classList.toggle("active", x === b)); renderHistoryList(); };
    });
    $("tidy-graph-btn").onclick = tidyGraphLayout;
    $("reset-layout-btn").onclick = () => { localStorage.removeItem(LAYOUT_STORAGE_KEY); state.savedPositions = {}; tidyGraphLayout(); toast("Spatial layout reset & tidied", "ok"); };

    $("tier-pill").onclick = () => openUpgradeModal("");
    $("close-upgrade-modal").onclick = () => $("upgrade-modal").classList.add("hidden");
    $("tab-pro-key").onclick = () => { sfx.click(); $("tab-pro-key").classList.add("active"); $("tab-byok-key").classList.remove("active"); $("panel-pro-key").classList.remove("hidden"); $("panel-byok-key").classList.add("hidden"); };
    $("tab-byok-key").onclick = () => { sfx.click(); $("tab-byok-key").classList.add("active"); $("tab-pro-key").classList.remove("active"); $("panel-byok-key").classList.remove("hidden"); $("panel-pro-key").classList.add("hidden"); };
    $("fill-dev-pro-btn").onclick = () => { $("pro-key-input").value = "neon-pro-local"; $("save-pro-key-btn").click(); };
    $("save-pro-key-btn").onclick = async () => {
      state.proKey = $("pro-key-input").value.trim();
      sessionStorage.setItem("neon_pro_key", state.proKey);
      await refreshTier();
      if (isPro()) { sfx.complete(); $("upgrade-modal").classList.add("hidden"); toast("Neon Pro unlocked!", "ok"); }
      else toast("Key not recognized (try neon-pro-local)", "err");
    };
    $("save-byok-key-btn").onclick = async () => {
      state.byokKey = $("byok-key-input").value.trim();
      sessionStorage.setItem("neon_byok_key", state.byokKey);
      await refreshTier();
      if (isPro()) { sfx.complete(); $("upgrade-modal").classList.add("hidden"); toast("Gemini BYOK connected!", "ok"); }
    };
    $("clear-keys-btn").onclick = async () => {
      state.proKey = ""; state.byokKey = "";
      sessionStorage.removeItem("neon_pro_key"); sessionStorage.removeItem("neon_byok_key");
      await refreshTier();
      $("upgrade-modal").classList.add("hidden");
      toast("Reverted to Guest Trial");
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
      const raw = input.value.trim();
      const sample = extractUrl(raw) || DEMO_VIDEO_URL;
      const useAgent = isNaturalLanguageCommand(raw);
      const endpoint = useAgent ? "/v1/agent/run" : "/v1/jobs";
      const body = useAgent ? { prompt: raw, url: sample } : buildPayload(sample);
      const proHeader = state.proKey ? ` \\\n  -H "X-Neon-Pro-Key: ${state.proKey}"` : "";
      $("api-curl-pre").textContent = `curl -X POST "${window.location.origin}${endpoint}" \\\n  -H "Content-Type: application/json"${proHeader} \\\n  -d '${JSON.stringify(body, null, 2)}'`;
      $("api-modal").classList.remove("hidden");
    };
    $("close-api-modal").onclick = () => $("api-modal").classList.add("hidden");
    $("copy-curl-btn").onclick = async () => { await navigator.clipboard.writeText($("api-curl-pre").textContent); sfx.click(); toast("cURL copied", "ok"); };
    $("copy-json-btn").onclick = async () => { const sample = extractUrl(input.value.trim()) || DEMO_VIDEO_URL; await navigator.clipboard.writeText(JSON.stringify(buildPayload(sample), null, 2)); sfx.click(); toast("JSON copied", "ok"); };

    const applyTheme = (t) => { document.documentElement.setAttribute("data-theme", t); localStorage.setItem("neon_sync_theme", t); $("theme-text").textContent = t === "dark" ? "Light" : "Dark"; };
    applyTheme(localStorage.getItem("neon_sync_theme") || "dark");
    $("theme-btn").onclick = () => { sfx.click(); applyTheme((document.documentElement.getAttribute("data-theme") || "dark") === "dark" ? "light" : "dark"); };
  }

  /* ---------- Hydrate Spatial Graph or Launch Default Interactive Showcase ---------- */
  async function hydrateFromBackend() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY) || "{}"); } catch (_) {}

    try {
      const jobs = await (await fetch("/v1/jobs?limit=25")).json();
      for (const j of jobs.reverse()) {
        state.autoSavedJobs.add(j.job_id);
        const src = ensureSourceNode(j.url, null, false);
        if (j.result?.title && !src.title) {
          src.title = j.result.title;
          if (j.result.thumbnail) src.probe = { thumbnail: j.result.thumbnail, duration_seconds: j.result.duration_seconds, source: j.result.source, engine: j.result.engine, content_type: j.result.content_type };
          renderNode(src);
        }
        upsertResultNodeForJob(j, src, null, "extract");
      }
    } catch (_) {}

    for (const note of (saved.notes || [])) spawnNoteNode(note.x, note.y, note.text, null, note.id);
    for (const edge of (saved.customEdges || [])) {
      if (state.nodes.has(edge.from) && state.nodes.has(edge.to)) {
        addEdge(edge.from, edge.to, edge.kind, false, edge.label || "", false, !!edge.customLabel);
      }
    }

    if (state.nodes.size === 0) {
      // First-run default outlook: live interactive threaded workbench immediately!
      spawnDefaultShowcase();
    } else if (!Object.keys(saved.positions || {}).length) {
      tidyGraphLayout();
    } else {
      setTimeout(() => { renderThreads(); fitAllNodes(); }, 60);
    }
    renderHistoryList();
    syncWorkspaceMeta();
  }

  /* ---------- Boot ---------- */
  (async () => {
    bindAllEvents();
    await Promise.all([refreshTier(), loadEngines(false)]);
    await hydrateFromBackend();
  })();
})();
