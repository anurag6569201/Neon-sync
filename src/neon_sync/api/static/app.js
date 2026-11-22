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
    batchOpen: false,
    autoSave: localStorage.getItem("neon_autosave") !== "false",
    sfxEnabled: localStorage.getItem("neon_sfx") !== "false",
    layoutMode: localStorage.getItem("neon_layout_mode") || "compass", // "compass" (4-way NESW) | "tree" (L->R)
    sse: new Map(),
    autoSavedJobs: new Set(),
    oscActive: false,
    historyJobs: [],
    selectedNodeId: null,
    nodes: new Map(), // id -> { id, type: 'source'|'choice'|'result'|'player'|'note', x, y, ... }
    edges: [],        // { id, from, to, kind, label, active?: bool, demo?: bool }
    sourceByUrl: new Map(),
    resultByJobId: new Map(),
    savedPositions: {},
    pendingPatch: null,
    hasDemoGraph: false,
    spaceHeld: false,
  };

  const playerHls = new Map();
  function destroyPlayerHls(nodeId) {
    const inst = playerHls.get(nodeId);
    if (inst) {
      try { inst.destroy(); } catch (_) {}
      playerHls.delete(nodeId);
    }
    const n = state.nodes.get(nodeId);
    if (n && n.hls) {
      try { n.hls.destroy(); } catch (_) {}
      n.hls = null;
    }
  }

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
      ctx.strokeStyle = e.kind === "live" ? "rgba(6,182,212,0.55)" : e.kind === "audio" ? "rgba(168,85,247,0.55)" : e.kind === "subtitles" ? "rgba(59,130,246,0.55)" : "rgba(255,77,0,0.55)";
      ctx.beginPath();
      ctx.moveTo(ox + (n1.x + 120) * scale, oy + (n1.y + 30) * scale);
      ctx.lineTo(ox + (n2.x + 120) * scale, oy + (n2.y + 30) * scale);
      ctx.stroke();
    }

    for (const n of state.nodes.values()) {
      const el = document.querySelector(`[data-node-id="${CSS.escape(n.id)}"]`);
      const nw = Math.max(6, (el ? el.offsetWidth : 248) * scale);
      const nh = Math.max(3.5, (el ? el.offsetHeight : 58) * scale);
      const rx = ox + n.x * scale;
      const ry = oy + n.y * scale;
      ctx.fillStyle = n.type === "source" ? "#ff4d00"
        : n.type === "result" ? "#16c47f"
        : n.type === "player" || n.theme === "live" ? "#06b6d4"
        : n.theme === "audio" ? "#a855f7"
        : n.theme === "subtitles" ? "#3b82f6"
        : n.type === "note" || n.theme === "image" ? "#f5b700"
        : "#a1a1aa";
      ctx.fillRect(rx, ry, nw, nh);
      if (state.selectedNodeId === n.id) {
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 1.2;
        ctx.strokeRect(rx - 0.8, ry - 0.8, nw + 1.6, nh + 1.6);
      }
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

  /* ---------- 60fps Infinite Camera-Synced Gravitational Dot & Architectural Grid Field ---------- */
  const gravity = (() => {
    const canvas = $("gravity-field");
    if (!canvas) return { shockAt: () => {}, pulseAt: () => {}, wake: () => {} };
    const ctx = canvas.getContext("2d");
    let w = 0, h = 0, dpr = 1;
    let lastCamKey = "";
    let lastPointerX = -9999, lastPointerY = -9999;
    let idleFrames = 0;
    const pointer = { x: -9999, y: -9999, active: false };
    const waves = [];

    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = window.innerWidth; h = window.innerHeight;
      canvas.width = w * dpr; canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      idleFrames = 0;
    }
    resize();
    window.addEventListener("resize", resize);

    function updateCoordHud(sx, sy) {
      const pt = screenToWorld(sx, sy);
      const cr = $("coord-readout");
      if (cr) {
        const px = (pt.x >= 0 ? "+" : "-") + String(Math.min(9999, Math.abs(Math.round(pt.x)))).padStart(4, "0");
        const py = (pt.y >= 0 ? "+" : "-") + String(Math.min(9999, Math.abs(Math.round(pt.y)))).padStart(4, "0");
        const nCount = state.nodes.size;
        cr.textContent = `X:${px} Y:${py} · ${Math.round(camera.zoom * 100)}% · ${nCount} NODE${nCount === 1 ? "" : "S"}`;
      }
    }

    window.addEventListener("pointermove", (e) => {
      pointer.x = e.clientX; pointer.y = e.clientY; pointer.active = true;
      idleFrames = 0;
      updateCoordHud(e.clientX, e.clientY);
    });
    window.addEventListener("pointerleave", () => { pointer.active = false; idleFrames = 0; });
    function pushWave(sx, sy, kind) { waves.push({ x: sx, y: sy, t: 0, kind }); idleFrames = 0; }

    function frame() {
      const camKey = `${camera.x.toFixed(1)}:${camera.y.toFixed(1)}:${camera.zoom.toFixed(3)}:${document.documentElement.getAttribute("data-theme")}`;
      const moved = camKey !== lastCamKey || pointer.x !== lastPointerX || pointer.y !== lastPointerY || waves.length > 0;
      if (!moved) {
        idleFrames++;
        if (idleFrames > 12) {
          requestAnimationFrame(frame);
          return;
        }
      } else {
        idleFrames = 0;
        lastCamKey = camKey;
        lastPointerX = pointer.x;
        lastPointerY = pointer.y;
      }

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

      // Subtle architectural 5x5 major grid crosshairs
      const majorStep = step * 5;
      const mox = ((camera.x % majorStep) + majorStep) % majorStep;
      const moy = (((camera.y + 54) % majorStep) + majorStep) % majorStep;
      ctx.strokeStyle = `rgba(${dotRgb},${isDark ? 0.08 : 0.09})`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let mx = mox; mx < w; mx += majorStep) {
        for (let my = moy; my < h; my += majorStep) {
          ctx.moveTo(mx - 4, my); ctx.lineTo(mx + 4, my);
          ctx.moveTo(mx, my - 4); ctx.lineTo(mx, my + 4);
        }
      }
      ctx.stroke();

      // World Origin (0,0) Reticle
      const originX = camera.x;
      const originY = camera.y + 54;
      if (originX > -20 && originX < w + 20 && originY > 34 && originY < h + 20) {
        ctx.strokeStyle = `rgba(${accentRgb},0.28)`;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(originX, originY, 7, 0, Math.PI * 2);
        ctx.moveTo(originX - 12, originY); ctx.lineTo(originX + 12, originY);
        ctx.moveTo(originX, originY - 12); ctx.lineTo(originX, originY + 12);
        ctx.stroke();
      }

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
      wake: () => { idleFrames = 0; },
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

  /* ---------- Request Headers ---------- */
  function apiHeaders(extra = {}) {
    return { ...extra };
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
    bookmark: '<path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z"/>',
    list: '<line x1="8" x2="21" y1="6" y2="6"/><line x1="8" x2="21" y1="12" y2="12"/><line x1="8" x2="21" y1="18" y2="18"/><line x1="3" x2="3.01" y1="6" y2="6"/><line x1="3" x2="3.01" y1="12" y2="12"/><line x1="3" x2="3.01" y1="18" y2="18"/>',
    radio: '<path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9"/><path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5"/><circle cx="12" cy="12" r="2"/><path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5"/><path d="M19.1 4.9C23 8.8 23 15.1 19.1 19"/>',
    "refresh-cw": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    compass: '<circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/>',
    archive: '<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>',
    shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
    "external-link": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
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
     CONTENT-DRIVEN DYNAMIC ROUTE ENGINE (Zero Guessing — Built From Real Probe!)
     ========================================================================= */

  const _LIVE_VIDEO_EXTS = new Set(["mp4", "webm", "mov", "m4v", "ogv", "m3u8"]);
  const _LIVE_AUDIO_EXTS = new Set(["m4a", "mp3", "opus", "webm", "ogg", "aac", "wav", "flac"]);

  function _normalizeCodecLabel(raw) {
    const c = String(raw || "").toLowerCase();
    if (!c || c === "none") return null;
    if (c.startsWith("avc") || c.includes("h264") || c.includes("h.264")) return "H.264";
    if (c.startsWith("hev") || c.startsWith("hvc") || c.includes("h265")) return "HEVC";
    if (c.startsWith("vp09") || c.startsWith("vp9")) return "VP9";
    if (c.startsWith("av01") || c === "av1") return "AV1";
    if (c.startsWith("mp4a") || c.includes("aac")) return "AAC";
    if (c.includes("opus")) return "Opus";
    if (c.includes("mp3")) return "MP3";
    if (c.includes("flac")) return "FLAC";
    return c.split(".")[0].toUpperCase();
  }

  function _snapVideoQualityParam(h) {
    if (!h) return "best";
    if (h >= 2000) return "2160";
    if (h >= 1350) return "1440";
    if (h >= 1000) return "1080";
    if (h >= 680) return "720";
    if (h >= 440) return "480";
    return "360";
  }

  function _resolutionDisplayTitle(h) {
    if (h >= 2160) return `4K · ${h}p`;
    if (h >= 1440) return `2K · ${h}p`;
    if (h >= 1080) return `${h}p · Full HD`;
    if (h >= 720) return `${h}p · HD`;
    if (h >= 480) return `${h}p · SD`;
    return `${h}p · Compact`;
  }

  function buildTrimPresets(durationSec) {
    const d = Number(durationSec || 0);
    if (!d || d <= 0) {
      return [
        { label: "0:00–0:15", start: "0:00", end: "0:15" },
        { label: "0:00–0:30", start: "0:00", end: "0:30" },
        { label: "0:30–1:00", start: "0:30", end: "1:00" },
      ];
    }
    if (d <= 15) {
      const half = Math.max(2, Math.floor(d / 2));
      return [
        { label: `0:00–${fmtDuration(half)}`, start: "0:00", end: fmtDuration(half) },
        { label: `${fmtDuration(half)}–${fmtDuration(d)}`, start: fmtDuration(half), end: fmtDuration(d) },
      ];
    }
    if (d <= 45) {
      const s15 = fmtDuration(15);
      const mid = Math.floor(d / 2);
      return [
        { label: `0:00–${s15}`, start: "0:00", end: s15 },
        { label: `${fmtDuration(Math.max(0, mid - 7))}–${fmtDuration(Math.min(d, mid + 8))}`, start: fmtDuration(Math.max(0, mid - 7)), end: fmtDuration(Math.min(d, mid + 8)) },
        { label: `Last 15s`, start: fmtDuration(Math.max(0, d - 15)), end: fmtDuration(d) },
      ];
    }
    const midStart = Math.max(0, Math.floor(d / 2 - 15));
    const midEnd = Math.min(Math.floor(d), midStart + 30);
    return [
      { label: "0:00–0:15", start: "0:00", end: "0:15" },
      { label: "0:00–0:30", start: "0:00", end: "0:30" },
      { label: `Mid 30s`, start: fmtDuration(midStart), end: fmtDuration(midEnd) },
    ];
  }

  function analyzeProbe(probe) {
    if (!probe) {
      return {
        ready: false,
        ct: "unknown",
        isLive: false,
        duration: null,
        videoFormats: [],
        audioFormats: [],
        videoTiers: [],
        hasVideo: false,
        hasAudio: false,
        hasSeparateVideoOnly: false,
        canStreamLive: false,
        liveMode: "video",
        audioSummary: null,
        subtitles: [],
        chapters: [],
        items: [],
        itemCount: 0,
        thumbnail: null,
        sourceWidth: null,
      };
    }

    const ct = probe.content_type || "unknown";
    const isLive = Boolean(probe.is_live || ct === "livestream");
    const duration = probe.duration_seconds || null;
    const formats = probe.available_formats || [];

    const videoFormats = [];
    const audioFormats = [];
    let canStreamVideo = false;
    let canStreamAudio = false;
    let maxVideoWidth = 0;

    for (const f of formats) {
      const mime = (f.mime_type || "").toLowerCase();
      const ext = (f.container || "").toLowerCase();
      const proto = (f.protocol || "").toLowerCase();
      const isHls = proto === "hls" || ext === "m3u8" || mime.includes("mpegurl");
      const hasV = Boolean(f.has_video || mime.startsWith("video/") || (f.height && !f.has_audio && f.video_codec !== "none"));
      const hasA = Boolean(f.has_audio || mime.startsWith("audio/") || (ct === "audio" && !hasV));

      if (hasV) {
        videoFormats.push(f);
        if (f.width && f.width > maxVideoWidth) maxVideoWidth = f.width;
      }
      if (hasA) audioFormats.push(f);

      if (f.direct_media_url && ["https", "http", "hls"].includes(proto)) {
        if (isHls || (hasV && hasA && (_LIVE_VIDEO_EXTS.has(ext) || mime.startsWith("video/"))) || (!hasV && !hasA && mime.startsWith("video/"))) {
          canStreamVideo = true;
        } else if (hasA && !hasV && (_LIVE_AUDIO_EXTS.has(ext) || mime.startsWith("audio/"))) {
          canStreamAudio = true;
        }
      }
    }

    // Best audio size estimate for muxed size calculation
    const bestAudioFmt = [...audioFormats].sort((a, b) => (b.audio_bitrate_kbps || 0) - (a.audio_bitrate_kbps || 0) || (b.size_bytes || 0) - (a.size_bytes || 0))[0] || null;
    const bestAudioSize = bestAudioFmt?.size_bytes || 0;

    // Group REAL detected video tiers by snapped resolution height (zero fake resolutions!)
    const tierMap = new Map();
    for (const f of videoFormats) {
      const rawH = f.height || 0;
      const qParam = rawH ? _snapVideoQualityParam(rawH) : "best";
      const key = rawH ? qParam : "best";
      let entry = tierMap.get(key);
      if (!entry) {
        entry = {
          qualityParam: qParam,
          height: rawH,
          maxFps: 0,
          codecs: new Set(),
          containers: new Set(),
          bestSize: null,
          hasMuxed: false,
          hasVideoOnly: false,
          formats: [],
        };
        tierMap.set(key, entry);
      }
      entry.formats.push(f);
      if (rawH > entry.height) entry.height = rawH;
      if (f.fps && f.fps > entry.maxFps) entry.maxFps = f.fps;
      const cLbl = _normalizeCodecLabel(f.video_codec);
      if (cLbl) entry.codecs.add(cLbl);
      if (f.container) entry.containers.add(f.container.toLowerCase());
      if (f.has_video && f.has_audio) entry.hasMuxed = true;
      if (f.has_video && !f.has_audio) entry.hasVideoOnly = true;
      if (f.size_bytes) {
        const est = f.requires_muxing ? f.size_bytes + bestAudioSize : f.size_bytes;
        if (!entry.bestSize || est < entry.bestSize) entry.bestSize = est;
      }
    }

    const videoTiers = Array.from(tierMap.values()).sort((a, b) => (b.height || 0) - (a.height || 0));
    const hasVideo = videoFormats.length > 0 || ct === "video" || isLive;
    const hasAudio = audioFormats.length > 0 || ct === "audio" || (ct === "video" && videoFormats.length === 0);
    const canStreamLive = isLive || canStreamVideo || canStreamAudio;
    const liveMode = (!canStreamVideo && canStreamAudio) || ct === "audio" ? "audio" : "video";

    const audioSummary = hasAudio ? {
      maxBitrateKbps: Math.round(bestAudioFmt?.audio_bitrate_kbps || 0) || null,
      nativeContainer: bestAudioFmt?.container ? bestAudioFmt.container.toUpperCase() : null,
      nativeCodec: _normalizeCodecLabel(bestAudioFmt?.audio_codec),
      nativeSize: bestAudioFmt?.size_bytes || null,
      formatId: bestAudioFmt?.format_id || null,
    } : null;

    // Sort subtitles: manual human tracks first, then auto-generated
    const subtitles = [...(probe.subtitles || [])].sort((a, b) => {
      if (Boolean(a.is_auto_generated) !== Boolean(b.is_auto_generated)) {
        return a.is_auto_generated ? 1 : -1;
      }
      return String(a.language || "").localeCompare(String(b.language || ""));
    });

    const chapters = probe.metadata?.chapters || [];
    const items = probe.items || [];
    const itemCount = probe.metadata?.item_count_total || items.length;
    const thumbnail = probe.thumbnail?.url ? probe.thumbnail : null;
    const webpageStats = probe.metadata?.extra?.webpage_stats || null;
    const webpageAssets = probe.metadata?.extra?.webpage_assets_preview || null;
    const feedType = probe.metadata?.extra?.feed_type || null;

    // Source image width & height (for smart non-upscaling resize & aspect-ratio calculation)
    let sourceWidth = null;
    let sourceHeight = null;
    if (ct === "image") {
      sourceWidth = formats[0]?.width || thumbnail?.width || null;
      sourceHeight = formats[0]?.height || thumbnail?.height || null;
    } else if (thumbnail?.width) {
      sourceWidth = thumbnail.width;
      sourceHeight = thumbnail.height || null;
    }

    return {
      ready: true,
      ct,
      isLive,
      duration,
      videoFormats,
      audioFormats,
      videoTiers,
      hasVideo,
      hasAudio,
      hasSeparateVideoOnly: videoFormats.some((f) => f.has_video && !f.has_audio) || videoFormats.length > 0,
      canStreamLive,
      liveMode,
      audioSummary,
      subtitles,
      chapters,
      items,
      itemCount,
      thumbnail,
      sourceWidth,
      sourceHeight,
      webpageStats,
      webpageAssets,
      feedType,
    };
  }

  function getLevel1Choices(sourceNode) {
    const p = sourceNode.probe;

    // 1. Probe failed -> show real recovery routes instead of guessing fake video options!
    if (sourceNode.probeError) {
      const recovery = [
        { key: "retry_probe", theme: "video", icon: "refresh-cw", title: "Retry Probe", sub: sourceNode.probeError.slice(0, 48), edgeLabel: "RETRY", isLeaf: true, delta: { _reprobe: true } },
      ];
      if (!sourceNode.usedDeepScan) {
        recovery.push({
          key: "deep_scan",
          theme: "live",
          icon: "compass",
          title: "Deep Scan Page",
          sub: "Dynamic Web Renderer for JS media",
          edgeLabel: "RENDER",
          isLeaf: true,
          delta: { _deepScan: true },
        });
      }
      return recovery;
    }

    // 2. Not probed yet -> prompt real probe
    if (!p) {
      return [
        { key: "probe_now", theme: "video", icon: "refresh-cw", title: "Inspect Source", sub: "Discover real streams, formats & tracks", edgeLabel: "PROBE", isLeaf: true, delta: { _reprobe: true } },
      ];
    }

    const cap = analyzeProbe(p);
    const { ct, isLive, videoTiers, hasAudio, canStreamLive, liveMode, audioSummary, subtitles, chapters, items, itemCount, thumbnail } = cap;

    // 3. Direct File / Document / Archive -> ONLY Direct Download with real metadata
    if (ct === "document" || ct === "archive" || ct === "file") {
      const mime = p.available_formats?.[0]?.mime_type || ct.toUpperCase();
      const sizeStr = p.size_bytes ? fmtBytes(p.size_bytes) : "Verified transfer";
      return [
        { key: "direct", theme: "video", icon: "file-down", title: "Direct Download", sub: `${mime} · ${sizeStr}`, edgeLabel: "FILE", isLeaf: true, detected: true, delta: { mode: "direct" } },
      ];
    }

    // 4. Single Image -> ONLY real Image format/resize tree
    if (ct === "image") {
      const f0 = p.available_formats?.[0];
      const dims = f0?.width && f0?.height ? `${f0.width}×${f0.height}` : null;
      const ext = f0?.container ? f0.container.toUpperCase() : "Original";
      const subParts = [dims, ext, p.size_bytes ? fmtBytes(p.size_bytes) : null].filter(Boolean);
      return [
        { key: "image", theme: "image", icon: "image", title: "Image", sub: subParts.length ? subParts.join(" · ") : "Original · WebP · PNG · JPEG", edgeLabel: "IMAGE", detected: true, nextStep: "image_format", delta: { mode: "image" } },
      ];
    }

    // 5. Image Gallery -> Real item count, range selection & individual picks
    if (ct === "gallery") {
      const list = [
        {
          key: "gallery",
          theme: "image",
          icon: "archive",
          title: itemCount ? `Gallery (${itemCount} items)` : "Gallery Archive",
          sub: "Select range · ZIP / TAR · WebP / PNG / Original",
          edgeLabel: itemCount ? `${itemCount} IMGS` : "GALLERY",
          detected: true,
          nextStep: itemCount > 1 ? "gallery_range" : "image_format",
          delta: { mode: "image" },
        },
      ];
      if (items.length > 0) {
        list.push({
          key: "gallery_items",
          theme: "image",
          icon: "layers",
          title: "Pick Single Image",
          sub: `Inspect ${Math.min(items.length, 8)} detected photo(s)`,
          edgeLabel: "BROWSE",
          detected: true,
          nextStep: "discovered_items",
          delta: {},
        });
      }
      return list;
    }

    // 6. Playlist or Podcast / RSS Feed
    if (ct === "playlist") {
      if (cap.feedType) {
        const isPod = cap.feedType === "podcast";
        const list = [];
        if (items.length > 0) {
          list.push({
            key: "feed_items",
            theme: isPod ? "audio" : "subtitles",
            icon: "list",
            title: isPod ? `Browse Episodes (${items.length})` : `Browse Entries (${items.length})`,
            sub: "Spawn any episode onto the canvas",
            edgeLabel: isPod ? "EPISODES" : "ENTRIES",
            detected: true,
            nextStep: "discovered_items",
            delta: {},
          });
        }
        if (canStreamLive) {
          list.push({
            key: "watch_live",
            theme: "live",
            icon: "play",
            title: "Listen Latest Live",
            sub: "Stream newest episode immediately",
            edgeLabel: "LIVE PLAY",
            isLeaf: true,
            detected: true,
            delta: { _livePlay: "audio" },
          });
        }
        list.push({
          key: "feed_md",
          theme: "subtitles",
          icon: "file-text",
          title: "Feed Index (Markdown)",
          sub: `Export ${items.length || itemCount} links & titles as .md`,
          edgeLabel: "INDEX MD",
          detected: true,
          isLeaf: true,
          delta: { mode: "webpage", webpage_format: "markdown" },
        });
        if (thumbnail) {
          list.push({ key: "image", theme: "image", icon: "image", title: "Show Cover Art", sub: "Original · WebP · PNG · JPEG", edgeLabel: "COVER", detected: true, nextStep: "image_format", delta: { mode: "image", _useThumbnailUrl: true } });
        }
        return list;
      }
      const countLbl = itemCount ? `${itemCount} items` : "Playlist";
      const list = [
        { key: "pl_video", theme: "video", icon: "film", title: "Playlist Video", sub: `${countLbl} · Pick range & quality`, edgeLabel: "PL VIDEO", detected: true, nextStep: "playlist_range", delta: { mode: "video" } },
        { key: "pl_audio", theme: "audio", icon: "headphones", title: "Playlist Audio", sub: `${countLbl} · MP3 · M4A · Opus · FLAC`, edgeLabel: "PL AUDIO", detected: true, nextStep: "playlist_range", delta: { mode: "audio" } },
      ];
      if (items.length > 0) {
        list.push({
          key: "pl_items",
          theme: "subtitles",
          icon: "list",
          title: `Browse Items (${items.length})`,
          sub: "Spawn any single track onto the canvas",
          edgeLabel: "ITEMS",
          detected: true,
          nextStep: "discovered_items",
          delta: {},
        });
      }
      if (thumbnail) {
        list.push({ key: "image", theme: "image", icon: "image", title: "Playlist Cover", sub: "Original · WebP · PNG · JPEG", edgeLabel: "COVER", detected: true, nextStep: "image_format", delta: { mode: "image", _useThumbnailUrl: true } });
      }
      return list;
    }

    // 7. Active Livestream -> Live playback + bounded live capture (no static trim!)
    if (isLive) {
      const list = [];
      if (canStreamLive) {
        list.push({ key: "watch_live", theme: "live", icon: "radio", title: "Watch Live", sub: "Instant HLS / stream player · Zero download", edgeLabel: "LIVE PLAY", isLeaf: true, detected: true, delta: { _livePlay: liveMode } });
      }
      list.push({
        key: "record_live",
        theme: "video",
        icon: "film",
        title: "Record Live Stream",
        sub: "Capture bounded clip (1m – 30m)",
        edgeLabel: "CAPTURE",
        detected: true,
        nextStep: "live_duration",
        delta: { mode: "video", video_container: "mp4" },
      });
      if (hasAudio) {
        list.push({
          key: "record_live_audio",
          theme: "audio",
          icon: "headphones",
          title: "Record Live Audio",
          sub: "Bounded MP3 audio capture",
          edgeLabel: "LIVE AUD",
          nextStep: "live_duration",
          delta: { mode: "audio", audio_format: "mp3", audio_bitrate: "192k" },
        });
      }
      if (thumbnail) {
        list.push({ key: "image", theme: "image", icon: "image", title: "Stream Thumbnail", sub: "HD Cover · WebP · PNG · JPEG", edgeLabel: "COVER", detected: true, nextStep: "image_format", delta: { mode: "image", _useThumbnailUrl: true } });
      }
      return list;
    }

    // 8. Pure Audio -> Listen Live (if streamable), Audio Formats, Chapters, Cover Art
    if (ct === "audio") {
      const list = [];
      if (canStreamLive) {
        list.push({ key: "watch_live", theme: "live", icon: "play", title: "Listen Live", sub: "Stream instantly · Zero download", edgeLabel: "LIVE PLAY", isLeaf: true, detected: true, delta: { _livePlay: "audio" } });
      }
      const aBits = [
        audioSummary?.nativeContainer,
        audioSummary?.maxBitrateKbps ? `${audioSummary.maxBitrateKbps}kbps` : null,
        audioSummary?.nativeSize ? fmtBytes(audioSummary.nativeSize) : null,
      ].filter(Boolean);
      list.push({
        key: "audio",
        theme: "audio",
        icon: "headphones",
        title: "Audio",
        sub: aBits.length ? `Source ${aBits.join(" · ")} · Convert or Master` : "MP3 · M4A · Opus · FLAC · WAV",
        edgeLabel: "AUDIO",
        detected: true,
        nextStep: "audio_format",
        delta: { mode: "audio" },
      });
      if (chapters.length > 0) {
        list.push({ key: "chapters", theme: "subtitles", icon: "bookmark", title: `Chapters (${chapters.length})`, sub: "Extract any chapter segment directly", edgeLabel: "CHAPTERS", detected: true, nextStep: "chapter_pick", delta: {} });
      }
      if (thumbnail) {
        list.push({ key: "image", theme: "image", icon: "image", title: "Cover Art", sub: "Extract & convert artwork", edgeLabel: "COVER", detected: true, nextStep: "image_format", delta: { mode: "image", _useThumbnailUrl: true } });
      }
      return list;
    }

    // 9. Webpage / Unknown -> Article Reader, Page Snapshot, SVG/Image Assets, Embedded Media & Deep Scan
    if (ct === "webpage" || ct === "unknown") {
      const list = [];
      const ws = cap.webpageStats;
      if (ws?.has_article) {
        const readStr = ws.reading_minutes ? `~${ws.reading_minutes} min read · ` : "";
        list.push({
          key: "web_article",
          theme: "subtitles",
          icon: "file-text",
          title: "Clean Article",
          sub: `${readStr}${ws.word_count.toLocaleString()} words · Preview, Copy, .md / .txt`,
          edgeLabel: "ARTICLE",
          detected: true,
          inputKind: "article_preview",
          isLeaf: true,
          delta: { mode: "webpage", webpage_format: "markdown" },
        });
      }
      list.push({
        key: "web_snapshot",
        theme: "live",
        icon: "camera",
        title: "Page Snapshot",
        sub: "Full-Page PNG Screenshot or Printable PDF",
        edgeLabel: "SNAPSHOT",
        detected: true,
        nextStep: "webpage_snapshot",
        delta: { mode: "webpage" },
      });
      const totalAssets = (ws?.image_count || 0) + (ws?.svg_count || 0);
      if (totalAssets > 0) {
        const assetBits = [
          ws.svg_count ? `${ws.svg_count} SVG${ws.svg_count === 1 ? "" : "s"}` : null,
          ws.image_count ? `${ws.image_count} image${ws.image_count === 1 ? "" : "s"}` : null,
        ].filter(Boolean).join(" · ");
        list.push({
          key: "web_assets",
          theme: "image",
          icon: "archive",
          title: "Page Assets & Logos",
          sub: `${assetBits} · Preview, Copy SVG, or ZIP`,
          edgeLabel: "ASSETS",
          detected: true,
          inputKind: "assets_preview",
          isLeaf: true,
          delta: { mode: "webpage", webpage_format: "assets_zip" },
        });
      }
      if (items.length > 0) {
        list.push({
          key: "web_items",
          theme: "video",
          icon: "layers",
          title: `Found Media (${items.length})`,
          sub: "Embedded video/image assets discovered on page",
          edgeLabel: `${items.length} FOUND`,
          detected: true,
          nextStep: "discovered_items",
          delta: {},
        });
      }
      if (canStreamLive) {
        list.push({ key: "watch_live", theme: "live", icon: "play", title: "Watch Live", sub: "Playable stream found on page", edgeLabel: "LIVE PLAY", isLeaf: true, detected: true, delta: { _livePlay: liveMode } });
      }
      if (videoTiers.length > 0) {
        list.push({ key: "video", theme: "video", icon: "film", title: "Video", sub: `${videoTiers.length} detected quality tier(s)`, edgeLabel: "VIDEO", detected: true, nextStep: "video_quality", delta: { mode: "video" } });
      }
      if (thumbnail) {
        list.push({ key: "image", theme: "image", icon: "image", title: "Page Cover Image", sub: "OpenGraph preview image", edgeLabel: "COVER", detected: true, nextStep: "image_format", delta: { mode: "image", _useThumbnailUrl: true } });
      }
      if (!p.engine?.used_browser_fallback && !sourceNode.usedDeepScan) {
        list.push({
          key: "deep_scan",
          theme: "live",
          icon: "compass",
          title: "Deep Scan Page",
          sub: "Run Dynamic Web Renderer to sniff JS media",
          edgeLabel: "RENDER",
          isLeaf: true,
          delta: { _deepScan: true },
        });
      }
      return list;
    }

    // 10. Video (VOD) -> Strictly build ONLY the routes backed by real probe data!
    const list = [];
    if (canStreamLive) {
      list.push({
        key: "watch_live",
        theme: "live",
        icon: "play",
        title: "Watch Live",
        sub: "Instant network stream · VLC mode",
        edgeLabel: "LIVE PLAY",
        isLeaf: true,
        detected: true,
        delta: { _livePlay: liveMode },
      });
    }

    const topTier = videoTiers[0];
    const vidSubParts = [];
    if (topTier?.height) vidSubParts.push(`Up to ${topTier.height >= 2160 ? "4K" : topTier.height + "p"}`);
    if (topTier?.maxFps && topTier.maxFps >= 48) vidSubParts.push(`${Math.round(topTier.maxFps)}fps`);
    if (videoTiers.length > 1) vidSubParts.push(`${videoTiers.length} qualities`);
    else if (topTier?.bestSize) vidSubParts.push(fmtBytes(topTier.bestSize));
    if (!hasAudio) vidSubParts.push("Muted source");

    // If video has NO audio track and no trim needed, skip unnecessary track-split step!
    const needsTrackChoice = hasAudio || (!isLive && (!cap.duration || cap.duration > 3));
    list.push({
      key: "video",
      theme: "video",
      icon: "film",
      title: "Video",
      sub: vidSubParts.length ? vidSubParts.join(" · ") : "Detected video stream",
      edgeLabel: topTier?.height ? (topTier.height >= 2160 ? "4K VID" : `${topTier.height}p`) : "VIDEO",
      detected: videoTiers.length > 0,
      nextStep: needsTrackChoice ? "video_track" : "video_quality",
      delta: { mode: "video", video_only: !hasAudio },
    });

    if (hasAudio) {
      const aBits = [
        audioSummary?.nativeContainer,
        audioSummary?.maxBitrateKbps ? `${audioSummary.maxBitrateKbps}k` : null,
      ].filter(Boolean);
      list.push({
        key: "audio",
        theme: "audio",
        icon: "headphones",
        title: "Audio",
        sub: aBits.length ? `Native ${aBits.join(" ")} · MP3 · M4A · Opus · FLAC` : "MP3 · M4A · Opus · FLAC · WAV",
        edgeLabel: "AUDIO",
        detected: cap.audioFormats.length > 0,
        nextStep: "audio_format",
        delta: { mode: "audio" },
      });
    }

    if (chapters.length > 0) {
      list.push({
        key: "chapters",
        theme: "subtitles",
        icon: "bookmark",
        title: `Chapters (${chapters.length})`,
        sub: "Clip any real chapter as Video or Audio",
        edgeLabel: "CHAPTERS",
        detected: true,
        nextStep: "chapter_pick",
        delta: {},
      });
    }

    if (subtitles.length > 0) {
      const manualCount = subtitles.filter((s) => !s.is_auto_generated).length;
      const subSummary = manualCount
        ? `${manualCount} manual · ${subtitles.length} total track(s)`
        : `${subtitles.length} auto caption track(s)`;
      list.push({
        key: "subtitles",
        theme: "subtitles",
        icon: "subtitles",
        title: `Subtitles (${subtitles.length})`,
        sub: subSummary,
        edgeLabel: "CAPTIONS",
        detected: true,
        nextStep: "sub_lang",
        delta: { mode: "video", extract_subtitles: true, subtitles_only: true },
      });
    }

    if (thumbnail) {
      const tDims = thumbnail.width && thumbnail.height ? `${thumbnail.width}×${thumbnail.height} · ` : "";
      list.push({
        key: "image",
        theme: "image",
        icon: "image",
        title: "Thumbnail",
        sub: `${tDims}Original · WebP · PNG · JPEG`,
        edgeLabel: "COVER",
        detected: true,
        nextStep: "image_format",
        delta: { mode: "image", _useThumbnailUrl: true },
      });
    }

    if (!isLive) {
      list.push({
        key: "studio_tools",
        theme: "video",
        icon: "sparkles",
        title: "Studio Tools",
        sub: "GIF / WebP · Discord 8MB Compress · Frame Sheet",
        edgeLabel: "STUDIO",
        nextStep: "video_studio",
        delta: { mode: "video", video_quality: "720", video_container: "mp4", video_codec: "h264" },
      });
    }

    return list;
  }

  function getNextChoicesFor(choiceNode) {
    const src = state.nodes.get(choiceNode.rootSourceId);
    const probe = src?.probe;
    const cap = analyzeProbe(probe);
    const step = choiceNode.nextStep;
    const acc = choiceNode.accumulated || {};

    if (step === "video_track") {
      const tracks = [];
      if (cap.hasAudio) {
        tracks.push({ key: "with_audio", theme: "video", icon: "volume-2", title: "Video + Audio", sub: "Full video with sound track", edgeLabel: "WITH AUDIO", detected: true, nextStep: "video_quality", delta: { video_only: false } });
      }
      if (cap.hasSeparateVideoOnly || !cap.hasAudio) {
        tracks.push({ key: "video_only", theme: "video", icon: "volume-x", title: "Video Only", sub: "Muted visual track for B-roll", edgeLabel: "MUTED", nextStep: "video_quality", delta: { video_only: true } });
      }
      if (!cap.isLive && (!cap.duration || cap.duration > 3)) {
        const bestClipQ = cap.videoTiers.find((t) => t.height <= 1080)?.qualityParam || cap.videoTiers[0]?.qualityParam || "best";
        tracks.push({
          key: "trim_video",
          theme: "video",
          icon: "scissors",
          title: "Trim Clip Range",
          sub: cap.duration ? `Cut exact segment (0:00 – ${fmtDuration(cap.duration)})` : "Cut exact start → end seconds",
          edgeLabel: "CLIP",
          inputKind: "trim",
          isLeaf: true,
          delta: { video_only: !cap.hasAudio, video_quality: bestClipQ, video_container: "mp4", video_codec: "h264" },
        });
      }
      return tracks;
    }

    if (step === "video_quality") {
      // Dynamically build ONLY the resolutions actually present in this media!
      if (cap.videoTiers.length > 0) {
        return cap.videoTiers.slice(0, 6).map((t) => {
          const details = [];
          if (t.maxFps && t.maxFps >= 48) details.push(`${Math.round(t.maxFps)}fps`);
          if (t.codecs.size) details.push(Array.from(t.codecs).slice(0, 2).join(" / "));
          if (t.bestSize) details.push(`~${fmtBytes(t.bestSize)}`);
          const icon = t.height >= 2160 ? "zap" : t.height >= 1080 ? "monitor" : t.height >= 720 ? "tv" : "smartphone";
          const lbl = t.height ? _resolutionDisplayTitle(t.height) : "Best Available";
          const edgeLbl = t.height >= 2160 ? "4K" : t.height >= 1440 ? "2K" : t.height ? `${t.height}p` : "BEST";
          return {
            key: `q_${t.qualityParam}_${t.height || "best"}`,
            theme: "video",
            icon,
            title: lbl,
            sub: details.length ? details.join(" · ") : "Detected stream",
            edgeLabel: edgeLbl,
            detected: true,
            nextStep: "video_container",
            delta: { video_quality: t.qualityParam },
          };
        });
      }
      // Fallback only when probe returned zero format details (e.g., playlist)
      return [
        { key: "q_best", theme: "video", icon: "monitor", title: "Best Available", sub: "Highest detected stream up to 1080p", edgeLabel: "BEST", nextStep: "video_container", delta: { video_quality: "1080" } },
        { key: "q_720", theme: "video", icon: "tv", title: "720p · HD", sub: "Balanced quality & size", edgeLabel: "720p", nextStep: "video_container", delta: { video_quality: "720" } },
        { key: "q_2160", theme: "video", icon: "zap", title: "4K · 2160p", sub: "Ultra-HD if available", edgeLabel: "4K", nextStep: "video_container", delta: { video_quality: "2160" } },
      ];
    }

    if (step === "video_container") {
      const qVal = acc.video_quality || "best";
      const tier = cap.videoTiers.find((t) => t.qualityParam === qVal) || cap.videoTiers[0];
      const qLbl = qVal === "2160" ? "4K" : qVal === "1440" ? "2K" : qVal === "best" ? "Best" : `${qVal}p`;
      const mLbl = acc.video_only ? "Muted" : "With Audio";
      const hasH264 = tier?.codecs?.has("H.264");
      const hasWebmCodec = tier?.codecs?.has("VP9") || tier?.codecs?.has("AV1") || tier?.containers?.has("webm");

      const containers = [
        {
          key: "c_mp4",
          theme: "video",
          icon: "download",
          title: "MP4 · H.264",
          sub: `${qLbl} ${mLbl} · ${hasH264 ? "Native H.264 remux" : "Universal compatibility"}`,
          edgeLabel: "MP4",
          detected: Boolean(hasH264 || tier?.containers?.has("mp4")),
          isLeaf: true,
          delta: { video_container: "mp4", video_codec: "h264", remux_faststart: true },
        },
        {
          key: "c_mkv",
          theme: "video",
          icon: "download",
          title: "MKV · Native Best",
          sub: `${qLbl} ${mLbl} · Zero re-encode (${tier?.codecs?.size ? Array.from(tier.codecs)[0] : "source codec"})`,
          edgeLabel: "MKV",
          isLeaf: true,
          delta: { video_container: "mkv", video_codec: "auto" },
        },
      ];
      if (hasWebmCodec || !tier) {
        containers.push({
          key: "c_webm",
          theme: "video",
          icon: "download",
          title: tier?.codecs?.has("AV1") ? "WebM · AV1 / VP9" : "WebM · VP9",
          sub: `${qLbl} ${mLbl} · Smallest native stream`,
          edgeLabel: "WEBM",
          detected: Boolean(hasWebmCodec),
          isLeaf: true,
          delta: { video_container: "webm", video_codec: tier?.codecs?.has("AV1") ? "av1" : "vp9" },
        });
      }
      if (cap.subtitles.length > 0 && !acc.video_only) {
        const prefSub = cap.subtitles.find((s) => !s.is_auto_generated) || cap.subtitles[0];
        containers.push({
          key: "c_subs",
          theme: "video",
          icon: "subtitles",
          title: "MP4 + Embedded Subs",
          sub: `${qLbl} · Muxes ${(prefSub.language || "en").toUpperCase()} captions`,
          edgeLabel: "MP4+SUBS",
          detected: true,
          isLeaf: true,
          delta: {
            video_container: "mp4",
            video_codec: "h264",
            extract_subtitles: true,
            embed_subtitles: true,
            subtitle_languages: [prefSub.language || "en"],
            subtitle_mode: prefSub.is_auto_generated ? "include_auto" : "manual_only",
          },
        });
      }
      return containers;
    }

    if (step === "audio_format") {
      const list = [];
      if (cap.audioSummary?.nativeContainer) {
        const natDesc = [
          cap.audioSummary.nativeCodec || cap.audioSummary.nativeContainer,
          cap.audioSummary.maxBitrateKbps ? `${cap.audioSummary.maxBitrateKbps}kbps` : null,
          cap.audioSummary.nativeSize ? fmtBytes(cap.audioSummary.nativeSize) : null,
        ].filter(Boolean).join(" · ");
        list.push({
          key: "af_native",
          theme: "audio",
          icon: "zap",
          title: `Native ${cap.audioSummary.nativeContainer}`,
          sub: `Zero re-encode · ${natDesc}`,
          edgeLabel: "NATIVE",
          detected: true,
          isLeaf: true,
          delta: { audio_format: "best", audio_bitrate: "auto", embed_metadata: true },
        });
      }
      list.push(
        { key: "af_mp3", theme: "audio", icon: "music", title: "MP3", sub: "Universal stereo compatibility", edgeLabel: "MP3", nextStep: "audio_quality", delta: { audio_format: "mp3" } },
        { key: "af_m4a", theme: "audio", icon: "disc-3", title: "M4A · AAC", sub: "Apple & mobile friendly", edgeLabel: "M4A", nextStep: "audio_quality", delta: { audio_format: "m4a" } },
        { key: "af_opus", theme: "audio", icon: "sliders-horizontal", title: "Opus", sub: "High-efficiency web audio", edgeLabel: "OPUS", nextStep: "audio_quality", delta: { audio_format: "opus" } },
        { key: "af_flac", theme: "audio", icon: "gem", title: "FLAC · Lossless", sub: "Bit-perfect studio master", edgeLabel: "FLAC", nextStep: "audio_lossless", delta: { audio_format: "flac", audio_bitrate: "auto" } },
        { key: "af_wav", theme: "audio", icon: "audio-waveform", title: "WAV · Raw PCM", sub: "Uncompressed DAW track", edgeLabel: "WAV", nextStep: "audio_lossless", delta: { audio_format: "wav", audio_bitrate: "auto" } },
      );
      return list;
    }

    if (step === "audio_quality") {
      const fmt = (acc.audio_format || "mp3").toUpperCase();
      const list = [
        { key: "aq_320n", theme: "audio", icon: "audio-waveform", title: "320k · Loudnorm", sub: `${fmt} · EBU R128 leveled volume`, edgeLabel: "320K+NORM", isLeaf: true, delta: { audio_bitrate: "320k", normalize_audio: true, embed_metadata: true } },
        { key: "aq_320", theme: "audio", icon: "download", title: "320k · Original", sub: `${fmt} · Maximum bitrate stereo`, edgeLabel: "320K", isLeaf: true, delta: { audio_bitrate: "320k", normalize_audio: false, embed_metadata: true } },
        { key: "aq_192", theme: "audio", icon: "download", title: "192k · Balanced", sub: `${fmt} · Fast & compact`, edgeLabel: "192K", isLeaf: true, delta: { audio_bitrate: "192k", normalize_audio: false, embed_metadata: true } },
      ];
      if (!cap.isLive && cap.ct !== "playlist" && (!cap.duration || cap.duration > 3)) {
        list.push({
          key: "aq_trim",
          theme: "audio",
          icon: "scissors",
          title: "Trim Audio Range",
          sub: cap.duration ? `Cut clip in ${fmt} (0:00 – ${fmtDuration(cap.duration)})` : `Cut exact clip in ${fmt} 320k`,
          edgeLabel: "TRIM",
          inputKind: "trim",
          isLeaf: true,
          delta: { audio_bitrate: "320k", normalize_audio: true },
        });
      }
      return list;
    }

    if (step === "audio_lossless") {
      const fmt = (acc.audio_format || "flac").toUpperCase();
      return [
        { key: "al_norm", theme: "audio", icon: "audio-waveform", title: `${fmt} · Loudnorm`, sub: "Broadcast EBU R128 loudness", edgeLabel: "NORM", isLeaf: true, delta: { normalize_audio: true, embed_metadata: true } },
        { key: "al_raw", theme: "audio", icon: "download", title: `${fmt} · Untouched`, sub: "Original dynamic range", edgeLabel: "RAW", isLeaf: true, delta: { normalize_audio: false, embed_metadata: true } },
      ];
    }

    if (step === "chapter_pick") {
      return cap.chapters.slice(0, 8).map((ch, idx) => ({
        key: `ch_${idx}`,
        theme: "subtitles",
        icon: "bookmark",
        title: ch.title.slice(0, 28),
        sub: `${fmtDuration(ch.start_seconds)} → ${fmtDuration(ch.end_seconds)} (${fmtDuration(Math.max(1, ch.end_seconds - ch.start_seconds))})`,
        edgeLabel: fmtDuration(ch.start_seconds),
        detected: true,
        nextStep: "chapter_export",
        delta: {
          clip_start_seconds: ch.start_seconds,
          clip_end_seconds: ch.end_seconds,
          custom_filename_prefix: ch.title.replace(/[^a-zA-Z0-9_-]+/g, "_").slice(0, 28),
        },
      }));
    }

    if (step === "chapter_export") {
      const bestClipQ = cap.videoTiers.find((t) => t.height <= 1080)?.qualityParam || "best";
      const list = [];
      if (cap.hasVideo) {
        list.push({ key: "ch_mp4", theme: "video", icon: "film", title: "Extract Chapter MP4", sub: "1080p H.264 video clip", edgeLabel: "MP4", isLeaf: true, delta: { mode: "video", video_quality: bestClipQ, video_container: "mp4", video_codec: "h264", remux_faststart: true } });
      }
      if (cap.hasAudio) {
        list.push({ key: "ch_mp3", theme: "audio", icon: "headphones", title: "Extract Chapter MP3", sub: "320k loudness-normalized audio", edgeLabel: "MP3", isLeaf: true, delta: { mode: "audio", audio_format: "mp3", audio_bitrate: "320k", normalize_audio: true } });
      }
      return list;
    }

    if (step === "sub_lang") {
      // Strictly real detected caption tracks!
      return cap.subtitles.slice(0, 6).map((s) => ({
        key: `lang_${s.language}_${s.is_auto_generated ? "auto" : "man"}`,
        theme: "subtitles",
        icon: "globe",
        title: s.name ? `${s.name} (${s.language.toUpperCase()})` : `${s.language.toUpperCase()} Captions`,
        sub: s.is_auto_generated ? "Auto-generated track" : "Manual human track",
        edgeLabel: s.language.toUpperCase(),
        detected: true,
        nextStep: "sub_format",
        delta: {
          subtitle_languages: [s.language],
          subtitle_mode: s.is_auto_generated ? "include_auto" : "manual_only",
        },
      }));
    }

    if (step === "sub_format") {
      return [
        { key: "sf_inspect", theme: "subtitles", icon: "file-text", title: "Live Caption Reader & Search", sub: "Preview, search quotes, copy text or export", edgeLabel: "PREVIEW", detected: true, inputKind: "sub_preview", isLeaf: true, delta: { subtitle_format: "vtt", generate_transcript: true } },
        { key: "sf_srt", theme: "subtitles", icon: "subtitles", title: "SubRip (.srt)", sub: "Ready for Premiere / DaVinci", edgeLabel: "SRT", isLeaf: true, delta: { subtitle_format: "srt", generate_transcript: true } },
        { key: "sf_vtt", theme: "subtitles", icon: "subtitles", title: "WebVTT (.vtt)", sub: "Standard HTML5 player track", edgeLabel: "VTT", isLeaf: true, delta: { subtitle_format: "vtt", generate_transcript: false } },
      ];
    }

    if (step === "live_duration") {
      return [
        { key: "ld_60", theme: "live", icon: "radio", title: "1 Minute Clip", sub: "Capture next 60s of live stream", edgeLabel: "1 MIN", isLeaf: true, delta: { livestream_max_duration: 60 } },
        { key: "ld_300", theme: "live", icon: "radio", title: "5 Minutes", sub: "Capture next 5 min segment", edgeLabel: "5 MIN", isLeaf: true, delta: { livestream_max_duration: 300 } },
        { key: "ld_900", theme: "live", icon: "radio", title: "15 Minutes", sub: "Extended broadcast capture", edgeLabel: "15 MIN", isLeaf: true, delta: { livestream_max_duration: 900 } },
        { key: "ld_1800", theme: "live", icon: "radio", title: "30 Minutes Max", sub: "Full bounded broadcast capture", edgeLabel: "30 MIN", isLeaf: true, delta: { livestream_max_duration: 1800 } },
      ];
    }

    if (step === "playlist_range") {
      const nextAfter = acc.mode === "audio" ? "audio_format" : "video_quality";
      const n = cap.itemCount || 10;
      const list = [];
      if (n <= 20) {
        list.push({ key: "pr_all", theme: choiceNode.theme, icon: "list", title: `All ${n} Items`, sub: "Download full playlist", edgeLabel: `1–${n}`, detected: true, nextStep: nextAfter, delta: { playlist_items: `1-${n}` } });
      }
      if (n > 5) {
        list.push({ key: "pr_5", theme: choiceNode.theme, icon: "list", title: "First 5 Items", sub: "Items 1 through 5", edgeLabel: "1–5", nextStep: nextAfter, delta: { playlist_items: "1-5" } });
      }
      if (n > 10) {
        list.push({ key: "pr_10", theme: choiceNode.theme, icon: "list", title: "First 10 Items", sub: "Items 1 through 10", edgeLabel: "1–10", nextStep: nextAfter, delta: { playlist_items: "1-10" } });
      }
      list.push({ key: "pr_custom", theme: choiceNode.theme, icon: "sliders-horizontal", title: "Custom Selection", sub: "e.g. 1-3 or 1,4,7", edgeLabel: "RANGE", inputKind: "playlist_range", isLeaf: true, delta: acc.mode === "audio" ? { audio_format: "mp3", audio_bitrate: "320k" } : { video_quality: "1080", video_container: "mp4" } });
      return list;
    }

    if (step === "gallery_range") {
      const n = cap.itemCount || 10;
      const list = [
        { key: "gr_all", theme: "image", icon: "archive", title: `All ${Math.min(n, 100)} Images`, sub: "Pack entire gallery into archive", edgeLabel: `1–${Math.min(n, 100)}`, detected: true, nextStep: "image_format", delta: { gallery_range_start: 1, gallery_range_end: Math.min(n, 100) } },
      ];
      if (n > 10) {
        list.push({ key: "gr_10", theme: "image", icon: "layers", title: "First 10 Images", sub: "Quick subset archive", edgeLabel: "1–10", nextStep: "image_format", delta: { gallery_range_start: 1, gallery_range_end: 10 } });
      }
      if (n > 25) {
        list.push({ key: "gr_25", theme: "image", icon: "layers", title: "First 25 Images", sub: "Medium gallery batch", edgeLabel: "1–25", nextStep: "image_format", delta: { gallery_range_start: 1, gallery_range_end: 25 } });
      }
      return list;
    }

    if (step === "discovered_items") {
      return cap.items.slice(0, 8).map((item, idx) => ({
        key: `item_${item.index || idx}`,
        theme: item.content_type === "image" ? "image" : item.content_type === "audio" ? "audio" : "video",
        icon: item.content_type === "image" ? "image" : item.content_type === "audio" ? "headphones" : "external-link",
        title: (item.title || `Item #${item.index || idx + 1}`).slice(0, 28),
        sub: [item.content_type?.toUpperCase(), item.duration_seconds ? fmtDuration(item.duration_seconds) : null, item.width && item.height ? `${item.width}×${item.height}` : null].filter(Boolean).join(" · ") || "Spawn onto canvas",
        edgeLabel: `#${item.index || idx + 1}`,
        detected: true,
        isLeaf: true,
        delta: { _spawnUrl: item.direct_media_url || item.url },
      }));
    }

    if (step === "webpage_article") {
      return [
        { key: "wa_md", theme: "subtitles", icon: "file-text", title: "Clean Markdown (.md)", sub: "Headings, links & source header · Zero clutter", edgeLabel: "MD", detected: true, isLeaf: true, delta: { mode: "webpage", webpage_format: "markdown" } },
        { key: "wa_txt", theme: "subtitles", icon: "file-text", title: "Plain Text (.txt)", sub: "Clean readable prose for notes", edgeLabel: "TXT", isLeaf: true, delta: { mode: "webpage", webpage_format: "text" } },
      ];
    }

    if (step === "webpage_snapshot") {
      return [
        { key: "ws_png", theme: "live", icon: "camera", title: "Full-Page PNG", sub: "1440px full-length lossless screenshot", edgeLabel: "PNG", detected: true, isLeaf: true, delta: { mode: "webpage", webpage_format: "screenshot_png" } },
        { key: "ws_pdf", theme: "live", icon: "file-down", title: "Printable PDF", sub: "Clean A4 page render with backgrounds", edgeLabel: "PDF", isLeaf: true, delta: { mode: "webpage", webpage_format: "pdf" } },
      ];
    }

    if (step === "video_studio") {
      return [
        { key: "st_gif", theme: "image", icon: "sparkles", title: "Animated GIF / WebP", sub: "Palette-optimized loopable clips", edgeLabel: "GIF/WEBP", nextStep: "studio_gif", delta: {} },
        { key: "st_comp", theme: "video", icon: "zap", title: "Compress to Size", sub: "Discord 8MB · Slack 25MB · Compact 50MB", edgeLabel: "COMPRESS", nextStep: "studio_compress", delta: {} },
        { key: "st_frames", theme: "image", icon: "camera", title: "Extract Frames", sub: "4×3 Contact Sheet or Full-Res Keyframe PNG", edgeLabel: "FRAMES", nextStep: "studio_frames", delta: {} },
      ];
    }

    if (step === "studio_gif") {
      const clipEnd = Math.min(8, cap.duration || 8);
      return [
        { key: "sg_gif_trim", theme: "image", icon: "scissors", title: "Trim → Crisp GIF", sub: "Pick exact start & end · 12fps 640px", edgeLabel: "TRIM GIF", inputKind: "trim", isLeaf: true, delta: { video_output_action: "gif", gif_fps: 12, gif_width: 640 } },
        { key: "sg_webp_trim", theme: "image", icon: "scissors", title: "Trim → Animated WebP", sub: "Pick exact range · Ultra-small loop", edgeLabel: "TRIM WEBP", inputKind: "trim", isLeaf: true, delta: { video_output_action: "webp_anim", gif_fps: 14, gif_width: 640 } },
        { key: "sg_gif_quick", theme: "image", icon: "sparkles", title: `Quick ${clipEnd}s GIF`, sub: `Instant 0:00 → 0:${String(Math.round(clipEnd)).padStart(2, "0")} GIF`, edgeLabel: "QUICK GIF", isLeaf: true, delta: { video_output_action: "gif", clip_start_seconds: 0, clip_end_seconds: clipEnd, gif_fps: 12, gif_width: 540 } },
      ];
    }

    if (step === "studio_compress") {
      const dur = cap.duration || 0;
      const hintFor = (mb, fallback) => {
        if (!dur || dur <= 0) return fallback;
        const kbps = Math.round(((mb * 8192 * 0.9) / dur) - 96);
        if (kbps >= 1500) return `~${kbps.toLocaleString()} kbps · Crisp HD`;
        if (kbps >= 500) return `~${kbps.toLocaleString()} kbps · Clean web quality`;
        return `~${Math.max(120, kbps)} kbps · Tight fit — trim clip for higher quality`;
      };
      return [
        { key: "sc_8", theme: "video", icon: "zap", title: "Discord · ≤ 8 MB", sub: hintFor(8, "Auto-tuned H.264 MP4 under 8 MB"), edgeLabel: "8 MB", isLeaf: true, delta: { target_size_mb: 8.0, video_quality: "720", video_container: "mp4" } },
        { key: "sc_25", theme: "video", icon: "zap", title: "Email / Slack · ≤ 25 MB", sub: hintFor(25, "Balanced HD MP4 under 25 MB"), edgeLabel: "25 MB", isLeaf: true, delta: { target_size_mb: 25.0, video_quality: "720", video_container: "mp4" } },
        { key: "sc_50", theme: "video", icon: "zap", title: "Compact Share · ≤ 50 MB", sub: hintFor(50, "High quality MP4 under 50 MB"), edgeLabel: "50 MB", isLeaf: true, delta: { target_size_mb: 50.0, video_quality: "1080", video_container: "mp4" } },
      ];
    }

    if (step === "studio_frames") {
      const midSec = Math.round((cap.duration || 6) / 2);
      return [
        { key: "sf_kf", theme: "image", icon: "camera", title: "Pick Exact Frame (PNG)", sub: "Scrub live preview & capture exact still", edgeLabel: "STILL PNG", inputKind: "frame", isLeaf: true, delta: { video_output_action: "keyframe", keyframe_seconds: midSec, video_quality: "1080" } },
        { key: "sf_sheet", theme: "image", icon: "layers", title: "Contact Sheet (4×3 PNG)", sub: "12 evenly spaced preview frames in one grid", edgeLabel: "4×3 GRID", isLeaf: true, delta: { video_output_action: "contact_sheet", video_quality: "720" } },
      ];
    }

    if (step === "image_format") {
      return [
        { key: "if_studio", theme: "image", icon: "sliders-horizontal", title: "Visual Image Studio", sub: "Preview, format, resize, quality & strip EXIF", edgeLabel: "STUDIO", detected: true, inputKind: "image_studio", isLeaf: true, delta: { image_format: "webp", image_quality: 90, strip_metadata: true } },
        { key: "if_orig", theme: "image", icon: "download", title: "Original HD", sub: "Exact source resolution", edgeLabel: "ORIGINAL", isLeaf: true, delta: { image_format: "original" } },
        { key: "if_strip", theme: "image", icon: "shield", title: "Privacy Clean (No EXIF)", sub: "Strip GPS & camera metadata", edgeLabel: "CLEAN", isLeaf: true, delta: { image_format: "original", strip_metadata: true } },
      ];
    }

    if (step === "image_size") {
      const fmt = (acc.image_format || "webp").toUpperCase();
      const maxW = cap.sourceWidth;
      const list = [
        { key: "is_orig", theme: "image", icon: "download", title: `${fmt} · Full Size`, sub: maxW ? `Keep original ${maxW}px width` : "Keep original dimensions", edgeLabel: "FULL", detected: true, isLeaf: true, delta: { image_max_width: null } },
      ];
      // Never offer upscaling wider than the real detected image width!
      const widths = [
        { w: 1920, icon: "monitor", sub: "Full HD width resize" },
        { w: 1280, icon: "tv", sub: "Web / Thumbnail HD size" },
        { w: 800, icon: "smartphone", sub: "Compact social preview" },
      ];
      for (const opt of widths) {
        if (!maxW || opt.w < maxW) {
          list.push({ key: `is_${opt.w}`, theme: "image", icon: opt.icon, title: `${fmt} · ${opt.w}px`, sub: opt.sub, edgeLabel: `${opt.w}px`, isLeaf: true, delta: { image_max_width: opt.w } });
        }
      }
      return list;
    }

    return [];
  }

  /* ---------- Tree Bloom, Pruning & Non-Overlapping Subtree Layout ---------- */
  function subtreeHasRealOutput(nodeId) {
    const n = state.nodes.get(nodeId);
    if (!n) return false;
    if (n.type === "result" || n.type === "note" || n.type === "player") return true;
    const kids = state.edges.filter((e) => e.from === nodeId).map((e) => e.to);
    return kids.some((cid) => subtreeHasRealOutput(cid));
  }

  function pruneChoiceSubtree(nodeId) {
    if (subtreeHasRealOutput(nodeId)) return; // Never wipe branches with real downloads/agents!
    const kids = state.edges.filter((e) => e.from === nodeId).map((e) => e.to);
    for (const cid of kids) pruneChoiceSubtree(cid);
    const n = state.nodes.get(nodeId);
    if (n && n.type === "choice") {
      destroyPlayerHls(nodeId);
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
      if (sib.expanded || sib.panelOpen) {
        const grandKids = state.edges.filter((e) => e.from === sib.id).map((e) => e.to);
        for (const gk of grandKids) pruneChoiceSubtree(gk);
        if (sib.panelOpen) destroyPlayerHls(sib.id);
        sib.expanded = false;
        sib.panelOpen = false;
        se.active = false;
        renderNode(sib);
      }
    }
  }

  const _NORTH_KEYS = new Set(["watch_live", "web_snapshot", "deep_scan", "retry_probe", "probe_now"]);
  const _WEST_KEYS = new Set(["audio", "subtitles", "chapters", "web_article", "pl_audio", "record_live_audio", "feed_items", "feed_md"]);
  const _SOUTH_KEYS = new Set(["image", "web_assets", "studio_tools"]);

  function resolveBranchDir(parentNode, spec) {
    if (parentNode.type !== "source") return parentNode.branchDir || "east";
    const key = String(spec?.key || "");
    if (_NORTH_KEYS.has(key)) return "north";
    if (_WEST_KEYS.has(key)) return "west";
    if (_SOUTH_KEYS.has(key)) return "south";
    return "east";
  }

  function getEffectiveDir(node) {
    if (state.layoutMode === "tree") return "east";
    return node?.branchDir || "east";
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
      const srcDur = state.nodes.get(rootId)?.probe?.duration_seconds;
      const defaultPresets = buildTrimPresets(srcDur);
      const branchDir = resolveBranchDir(parentNode, spec);
      const node = {
        id,
        type: "choice",
        parentId: parentNode.id,
        rootSourceId: rootId,
        branchDir,
        depth: parentDepth + 1,
        theme: spec.theme || parentNode.theme || "video",
        icon: spec.icon,
        title: spec.title,
        sub: spec.sub,
        detected: !!spec.detected,
        isLeaf: !!spec.isLeaf,
        nextStep: spec.nextStep || null,
        inputKind: spec.inputKind || null,
        accumulated: { ...parentAcc, ...(spec.delta || {}) },
        expanded: false,
        panelOpen: false,
        demo: !!parentNode.demo,
        animDelayMs: idx * 32,
        clipStart: defaultPresets[0]?.start || "0:00",
        clipEnd: defaultPresets[0]?.end || (spec.delta?.video_output_action ? fmtDuration(Math.min(6, srcDur || 6)) : "0:15"),
        frameSec: spec.delta?.keyframe_seconds ?? Math.min(2, (srcDur || 4) / 2),
        gifFps: spec.delta?.gif_fps || 12,
        gifWidth: spec.delta?.gif_width || 640,
        loopPreview: Boolean(spec.delta?.video_output_action === "gif" || spec.delta?.video_output_action === "webp_anim"),
        previewMuted: Boolean(spec.delta?.video_output_action === "gif" || spec.delta?.video_output_action === "webp_anim"),
        subExportFmt: spec.delta?.subtitle_format || "vtt",
        subGenTxt: spec.delta?.generate_transcript !== false,
        articleFmt: spec.delta?.webpage_format || "markdown",
        imgFmt: spec.delta?.image_format || "webp",
        imgQuality: spec.delta?.image_quality || 90,
        imgMaxWidth: null,
        imgStripExif: spec.delta?.strip_metadata !== false,
        playlistRangeVal: "1-5",
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
    // Highlight the active lineage AND keep every user-expanded branch vivid (multi-path exploration!)
    const litNodes = new Set();
    const litEdges = new Set();
    const walkUp = (startId) => {
      let cur = startId;
      while (cur) {
        litNodes.add(cur);
        const inEdge = state.edges.find((e) => e.to === cur && state.nodes.get(e.from)?.type !== "note");
        if (!inEdge) break;
        litEdges.add(inEdge.id);
        cur = inEdge.from;
      }
    };
    walkUp(activeNodeId);
    const activeNode = state.nodes.get(activeNodeId);
    const rootId = activeNode?.type === "source" ? activeNode.id : activeNode?.rootSourceId;
    for (const n of state.nodes.values()) {
      if (n.type === "choice" && (!rootId || n.rootSourceId === rootId) && (n.expanded || n.panelOpen)) {
        walkUp(n.id);
      }
    }
    for (const n of state.nodes.values()) {
      if (n.type === "choice" && (!rootId || n.rootSourceId === rootId)) {
        const el = document.querySelector(`[data-node-id="${CSS.escape(n.id)}"]`);
        if (el) {
          const isLit = litNodes.has(n.id);
          el.classList.toggle("path-lit", isLit);
          el.classList.remove("path-dimmed");
        }
      }
    }
    for (const e of state.edges) {
      const target = state.nodes.get(e.to);
      if (target?.type === "choice" && (!rootId || target.rootSourceId === rootId)) {
        e.active = litEdges.has(e.id);
        e.dimmed = false;
      }
    }
    renderThreads();
  }

  function layoutTreeFromRoot(rootId) {
    const root = state.nodes.get(rootId);
    if (!root) return;

    const hGap = 118;   // Horizontal gap between columns (East / West)
    const rowGap = 18;  // Vertical gap between siblings in horizontal trees
    const vGap = 86;    // Vertical gap between rows (North / South)
    const hSibGap = 24; // Horizontal gap between siblings in vertical trees

    const getSize = (nodeId) => {
      const n = state.nodes.get(nodeId);
      const el = document.querySelector(`[data-node-id="${CSS.escape(nodeId)}"]`);
      return {
        w: el ? el.offsetWidth : (n?.type === "source" ? 344 : n?.type === "result" ? 372 : n?.type === "player" ? 420 : 252),
        h: el ? el.offsetHeight : (n?.type === "source" ? 260 : 62),
      };
    };

    const getChildren = (nodeId, visited) =>
      state.edges
        .filter((e) => e.from === nodeId)
        .map((e) => e.to)
        .filter((cid) => state.nodes.has(cid) && state.nodes.get(cid)?.type !== "note" && !visited.has(cid));

    const rootSize = getSize(rootId);
    const rootCx = root.x + rootSize.w / 2;
    const rootCy = root.y + rootSize.h / 2;

    // Classic Left-to-Right Tree Mode
    if (state.layoutMode === "tree") {
      const infoMap = new Map();
      const maxWByDepth = new Map([[0, rootSize.w]]);
      const vis = new Set();

      const measureH = (nodeId, depth) => {
        if (vis.has(nodeId)) return 0;
        vis.add(nodeId);
        const { w, h } = getSize(nodeId);
        maxWByDepth.set(depth, Math.max(maxWByDepth.get(depth) || 0, w));
        const kids = getChildren(nodeId, vis);
        let total = h;
        if (kids.length) {
          total = Math.max(h, kids.reduce((a, cid) => a + measureH(cid, depth + 1), 0) + rowGap * (kids.length - 1));
        }
        infoMap.set(nodeId, { ownH: h, ownW: w, total, kids, depth });
        return total;
      };

      measureH(rootId, 0);
      const colX = new Map([[0, root.x]]);
      const maxD = Math.max(0, ...Array.from(maxWByDepth.keys()));
      for (let d = 0; d < maxD; d++) {
        colX.set(d + 1, (colX.get(d) || root.x) + (maxWByDepth.get(d) || 252) + hGap);
      }

      const placeEast = (nodeId, topY) => {
        const n = state.nodes.get(nodeId);
        const info = infoMap.get(nodeId);
        if (!n || !info) return;
        if (nodeId !== rootId) {
          n.x = Math.round(colX.get(info.depth) ?? root.x);
          n.y = Math.round(topY + (info.total - info.ownH) / 2);
          const el = document.querySelector(`[data-node-id="${CSS.escape(nodeId)}"]`);
          if (el) { el.style.left = `${n.x}px`; el.style.top = `${n.y}px`; }
        }
        if (!info.kids.length) return;
        const kidsTotal = info.kids.reduce((s, cid) => s + (infoMap.get(cid)?.total || 64), 0) + rowGap * (info.kids.length - 1);
        let curY = (n.y + info.ownH / 2) - kidsTotal / 2;
        for (const cid of info.kids) {
          const ch = infoMap.get(cid)?.total || 64;
          placeEast(cid, curY);
          curY += ch + rowGap;
        }
      };

      const rInfo = infoMap.get(rootId);
      placeEast(rootId, rootCy - (rInfo?.total || 200) / 2);
      requestAnimationFrame(renderThreads);
      return;
    }

    // 4-Way Semantic Compass Mode (Zero-Overlap Quadrant Solver)
    const rootVis = new Set([rootId]);
    const directKids = getChildren(rootId, rootVis);
    const eastKids = [];
    const westKids = [];
    const northKids = [];
    const southKids = [];
    for (const cid of directKids) {
      const dir = state.nodes.get(cid)?.branchDir || "east";
      if (dir === "west") westKids.push(cid);
      else if (dir === "north") northKids.push(cid);
      else if (dir === "south") southKids.push(cid);
      else eastKids.push(cid);
    }

    // Helper: lay out a horizontal side ("east" or "west") and return its exact vertical/horizontal bounds
    const layoutHorizontalSide = (sideKids, isWest) => {
      if (!sideKids.length) return null;
      const vis = new Set([rootId]);
      const infoMap = new Map();
      const maxWByDepth = new Map();

      const measure = (nodeId, depth) => {
        if (vis.has(nodeId)) return 0;
        vis.add(nodeId);
        const { w, h } = getSize(nodeId);
        maxWByDepth.set(depth, Math.max(maxWByDepth.get(depth) || 0, w));
        const kids = getChildren(nodeId, vis);
        let total = h;
        if (kids.length) {
          total = Math.max(h, kids.reduce((a, cid) => a + measure(cid, depth + 1), 0) + rowGap * (kids.length - 1));
        }
        infoMap.set(nodeId, { ownH: h, ownW: w, total, kids, depth });
        return total;
      };

      const sideTotalH = sideKids.reduce((a, cid) => a + measure(cid, 1), 0) + rowGap * (sideKids.length - 1);
      const maxD = Math.max(1, ...Array.from(maxWByDepth.keys()));
      const colPos = new Map();
      if (!isWest) {
        colPos.set(1, root.x + rootSize.w + hGap);
        for (let d = 1; d < maxD; d++) {
          colPos.set(d + 1, colPos.get(d) + (maxWByDepth.get(d) || 252) + hGap);
        }
      } else {
        // Right-edge X coordinate for each West column so cards align cleanly facing the Source
        colPos.set(1, root.x - hGap);
        for (let d = 1; d < maxD; d++) {
          colPos.set(d + 1, colPos.get(d) - (maxWByDepth.get(d) || 252) - hGap);
        }
      }

      const bounds = { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity };
      const place = (nodeId, topY) => {
        const n = state.nodes.get(nodeId);
        const info = infoMap.get(nodeId);
        if (!n || !info) return;
        n.x = Math.round(isWest ? (colPos.get(info.depth) - info.ownW) : colPos.get(info.depth));
        n.y = Math.round(topY + (info.total - info.ownH) / 2);
        const el = document.querySelector(`[data-node-id="${CSS.escape(nodeId)}"]`);
        if (el) { el.style.left = `${n.x}px`; el.style.top = `${n.y}px`; }
        bounds.minX = Math.min(bounds.minX, n.x);
        bounds.maxX = Math.max(bounds.maxX, n.x + info.ownW);
        bounds.minY = Math.min(bounds.minY, n.y);
        bounds.maxY = Math.max(bounds.maxY, n.y + info.ownH);

        if (!info.kids.length) return;
        const kidsTotal = info.kids.reduce((s, cid) => s + (infoMap.get(cid)?.total || 64), 0) + rowGap * (info.kids.length - 1);
        let curY = (n.y + info.ownH / 2) - kidsTotal / 2;
        for (const cid of info.kids) {
          const ch = infoMap.get(cid)?.total || 64;
          place(cid, curY);
          curY += ch + rowGap;
        }
      };

      let curY = rootCy - sideTotalH / 2;
      for (const cid of sideKids) {
        const ch = infoMap.get(cid)?.total || 64;
        place(cid, curY);
        curY += ch + rowGap;
      }
      return bounds;
    };

    const eastBounds = layoutHorizontalSide(eastKids, false);
    const westBounds = layoutHorizontalSide(westKids, true);

    // Helper: lay out a vertical side ("north" or "south") with strict anti-collision against East & West!
    const layoutVerticalSide = (sideKids, isNorth) => {
      if (!sideKids.length) return null;
      const vis = new Set([rootId]);
      const infoMap = new Map();
      const maxHByDepth = new Map();

      const measure = (nodeId, depth) => {
        if (vis.has(nodeId)) return 0;
        vis.add(nodeId);
        const { w, h } = getSize(nodeId);
        maxHByDepth.set(depth, Math.max(maxHByDepth.get(depth) || 0, h));
        const kids = getChildren(nodeId, vis);
        let total = w;
        if (kids.length) {
          total = Math.max(w, kids.reduce((a, cid) => a + measure(cid, depth + 1), 0) + hSibGap * (kids.length - 1));
        }
        infoMap.set(nodeId, { ownH: h, ownW: w, total, kids, depth });
        return total;
      };

      const sideTotalW = sideKids.reduce((a, cid) => a + measure(cid, 1), 0) + hSibGap * (sideKids.length - 1);
      const spanLeft = rootCx - sideTotalW / 2;
      const spanRight = rootCx + sideTotalW / 2;
      const pad = 28;

      // Guarantee zero collision with East/West subtrees whenever horizontal spans overlap!
      let baseEdgeY = isNorth ? (root.y - vGap) : (root.y + rootSize.h + vGap);
      if (eastBounds && spanRight + pad > eastBounds.minX) {
        baseEdgeY = isNorth
          ? Math.min(baseEdgeY, eastBounds.minY - vGap)
          : Math.max(baseEdgeY, eastBounds.maxY + vGap);
      }
      if (westBounds && spanLeft - pad < westBounds.maxX) {
        baseEdgeY = isNorth
          ? Math.min(baseEdgeY, westBounds.minY - vGap)
          : Math.max(baseEdgeY, westBounds.maxY + vGap);
      }

      const maxD = Math.max(1, ...Array.from(maxHByDepth.keys()));
      const rowPos = new Map();
      rowPos.set(1, baseEdgeY);
      for (let d = 1; d < maxD; d++) {
        rowPos.set(
          d + 1,
          isNorth
            ? (rowPos.get(d) - (maxHByDepth.get(d) || 64) - vGap)
            : (rowPos.get(d) + (maxHByDepth.get(d) || 64) + vGap),
        );
      }

      const place = (nodeId, leftX) => {
        const n = state.nodes.get(nodeId);
        const info = infoMap.get(nodeId);
        if (!n || !info) return;
        n.x = Math.round(leftX + (info.total - info.ownW) / 2);
        n.y = Math.round(isNorth ? (rowPos.get(info.depth) - info.ownH) : rowPos.get(info.depth));
        const el = document.querySelector(`[data-node-id="${CSS.escape(nodeId)}"]`);
        if (el) { el.style.left = `${n.x}px`; el.style.top = `${n.y}px`; }

        if (!info.kids.length) return;
        const kidsTotal = info.kids.reduce((s, cid) => s + (infoMap.get(cid)?.total || 252), 0) + hSibGap * (info.kids.length - 1);
        let curX = (n.x + info.ownW / 2) - kidsTotal / 2;
        for (const cid of info.kids) {
          const cw = infoMap.get(cid)?.total || 252;
          place(cid, curX);
          curX += cw + hSibGap;
        }
      };

      let curX = rootCx - sideTotalW / 2;
      for (const cid of sideKids) {
        const cw = infoMap.get(cid)?.total || 252;
        place(cid, curX);
        curX += cw + hSibGap;
      }
    };

    layoutVerticalSide(northKids, true);
    layoutVerticalSide(southKids, false);
    requestAnimationFrame(renderThreads);
  }

  /* ---------- Click Handler for Cascading Choice Nodes (Multi-Branch Open!) ---------- */
  function onChoiceClicked(choiceNode) {
    // Interactive Studio / Preview choice nodes toggle open/closed independently without closing siblings
    if (choiceNode.inputKind) {
      const opening = !choiceNode.panelOpen;
      if (!opening) {
        destroyPlayerHls(choiceNode.id);
      }
      choiceNode.panelOpen = opening;
      choiceNode.expanded = opening;
      renderNode(choiceNode);
      refreshActiveLineageHighlight(choiceNode.id);
      layoutTreeFromRoot(choiceNode.rootSourceId);
      sfx.click();
      if (opening) {
        const el = document.querySelector(`[data-node-id="${CSS.escape(choiceNode.id)}"]`);
        const cx = choiceNode.x + (el ? el.offsetWidth / 2 : 178);
        const cy = choiceNode.y + (el ? el.offsetHeight / 2 : 160);
        glideToWorld(cx, cy, Math.min(camera.zoom, 0.96));
      }
      return;
    }

    // If leaf node -> execute real download!
    if (choiceNode.isLeaf) {
      return executeChoiceLeaf(choiceNode);
    }

    // If already expanded -> toggle ONLY this branch closed (keeping all other open branches!)
    const existingChoiceKids = state.edges
      .filter((e) => e.from === choiceNode.id)
      .map((e) => state.nodes.get(e.to))
      .filter((n) => n && n.type === "choice");
    if (existingChoiceKids.length) {
      for (const kid of existingChoiceKids) pruneChoiceSubtree(kid.id);
      const remainingKids = state.edges.filter((e) => e.from === choiceNode.id).length;
      choiceNode.expanded = remainingKids > 0;
      renderNode(choiceNode);
      refreshActiveLineageHighlight(choiceNode.parentId || choiceNode.id);
      layoutTreeFromRoot(choiceNode.rootSourceId);
      sfx.click();
      return;
    }

    // Otherwise -> bloom the NEXT column of possible paths while keeping all sibling paths open!
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

    // Handle Live Network Stream Watch / Listen leaf (VLC-style zero-download playback!)
    if (acc._livePlay) {
      return launchLivePlayerFromParent(choiceNode, src.url, acc._livePlay);
    }

    // Handle Dynamic Recovery / Re-probe / Deep Scan / Spawn Discovered Item leaves!
    if (acc._reprobe) {
      return probeAndBloomLevel1(src, { bypassCache: true });
    }
    if (acc._deepScan) {
      return probeAndBloomLevel1(src, { allowBrowserFallback: true, bypassCache: true });
    }
    if (acc._spawnUrl) {
      const pos = pickChildPosition(choiceNode, 0);
      const childSrc = spawnSourceWithPossibilities(acc._spawnUrl, pos, true);
      if (childSrc) addEdge(choiceNode.id, childSrc.id, choiceNode.theme, false, "SPAWNED");
      return;
    }

    // Handle inline Trim Clip / GIF / Frame Picker leaves
    if (choiceNode.inputKind === "trim") {
      acc.clip_start_seconds = parseTime(choiceNode.clipStart);
      acc.clip_end_seconds = parseTime(choiceNode.clipEnd);
      if (acc.video_output_action === "gif" || acc.video_output_action === "webp_anim") {
        acc.gif_fps = choiceNode.gifFps || acc.gif_fps || 12;
        acc.gif_width = choiceNode.gifWidth || acc.gif_width || 640;
      }
    }
    if (choiceNode.inputKind === "frame") {
      acc.keyframe_seconds = Math.max(0, Number(choiceNode.frameSec) || 0);
    }
    if (choiceNode.inputKind === "sub_preview") {
      acc.subtitle_format = choiceNode.subExportFmt || "vtt";
      acc.generate_transcript = choiceNode.subGenTxt !== false;
    }
    if (choiceNode.inputKind === "article_preview") {
      acc.mode = "webpage";
      acc.webpage_format = choiceNode.articleFmt || "markdown";
    }
    if (choiceNode.inputKind === "image_studio") {
      acc.mode = "image";
      acc.image_format = choiceNode.imgFmt || "webp";
      acc.image_quality = choiceNode.imgQuality || 90;
      acc.image_max_width = choiceNode.imgMaxWidth || null;
      acc.strip_metadata = !!choiceNode.imgStripExif;
    }
    if (choiceNode.inputKind === "playlist_range") {
      const rVal = (choiceNode.playlistRangeVal || "").trim();
      if (!rVal) return toast("Enter playlist items (e.g. 1-5 or 1,3,5)", "err");
      acc.playlist_items = rVal;
    }

    // Build clean DownloadRequest payload
    const targetUrl = acc._useThumbnailUrl && src?.probe?.thumbnail?.url ? src.probe.thumbnail.url : src.url;
    delete acc._useThumbnailUrl;
    delete acc._reprobe;
    delete acc._deepScan;
    delete acc._spawnUrl;

    const payload = { url: targetUrl, ...acc };
    if (targetUrl === src?.probe?.thumbnail?.url && src?.title) {
      payload.custom_filename_prefix = src.title.replace(/[^a-zA-Z0-9_-]+/g, "_").slice(0, 32);
    }
    osc.set(true, `EXTRACTING · ${choiceNode.title.toUpperCase()}…`);
    sfx.capture();

    try {
      const res = await fetch("/v1/jobs", {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      const job = await res.json();
      if (!res.ok) {
        toast(job.detail?.message || "Extraction rejected", "err");
        osc.set(false, "CANVAS READY");
        return;
      }
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
    $("sandbox-banner")?.classList.add("hidden");
    renderThreads();
    syncWorkspaceMeta();
  }

  function promoteDemoTreeToReal() {
    if (!state.hasDemoGraph) return;
    state.hasDemoGraph = false;
    $("sandbox-banner")?.classList.add("hidden");
    for (const n of state.nodes.values()) n.demo = false;
    for (const e of state.edges) e.demo = false;
    persistWorkspace();
    syncWorkspaceMeta();
  }

  function spawnDefaultShowcase() {
    clearDemoGraph();
    state.hasDemoGraph = true;
    $("sandbox-banner")?.classList.remove("hidden");

    const v = SHOWCASE_VIDEO;
    const src = {
      id: `src_${hashUrl(v.url)}`,
      type: "source",
      demo: true,
      url: v.url,
      title: v.title,
      x: state.layoutMode === "compass" ? -172 : -560,
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
        engine: { engine_used: "neon-sync" },
        available_formats: [
          { format_id: "137", resolution: "1920x1080", height: 1080, fps: 30, container: "mp4", mime_type: "video/mp4", video_codec: "avc1", has_video: true, has_audio: false, requires_muxing: true, size_bytes: 34200000 },
          { format_id: "22", resolution: "1280x720", height: 720, fps: 30, container: "mp4", mime_type: "video/mp4", video_codec: "avc1", audio_codec: "mp4a", has_video: true, has_audio: true, direct_media_url: v.url, protocol: "https", size_bytes: 18900000 },
          { format_id: "135", resolution: "854x480", height: 480, fps: 30, container: "webm", mime_type: "video/webm", video_codec: "vp09", has_video: true, has_audio: false, requires_muxing: true, size_bytes: 9400000 },
          { format_id: "140", resolution: "audio only", container: "m4a", mime_type: "audio/mp4", audio_codec: "mp4a", audio_bitrate_kbps: 128, has_video: false, has_audio: true, size_bytes: 4200000 },
        ],
        subtitles: [{ language: "hi", name: "Hindi", is_auto_generated: true }, { language: "en", name: "English", is_auto_generated: true }],
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
    drawMinimap();
    const emptyHud = $("empty-canvas-hud");
    if (emptyHud) emptyHud.classList.toggle("hidden", state.nodes.size > 0);
  }

  function clearEntireCanvas() {
    for (const n of state.nodes.values()) {
      if (n.type === "player" && n.hls) {
        try { n.hls.destroy(); } catch (_) {}
      }
    }
    state.nodes.clear();
    state.edges = [];
    state.sourceByUrl.clear();
    state.resultByJobId.clear();
    state.selectedNodeId = null;
    state.hasDemoGraph = false;
    $("sandbox-banner")?.classList.add("hidden");
    const layer = $("nodes-layer");
    if (layer) layer.innerHTML = "";
    renderThreads();
    syncWorkspaceMeta();
    persistWorkspace();
    sfx.click();
    toast("Canvas cleared", "ok");
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
    if (n.type === "player" && n.hls) {
      try { n.hls.destroy(); } catch (_) {}
      n.hls = null;
    }
    state.nodes.delete(nodeId);
    state.edges = state.edges.filter((e) => e.from !== nodeId && e.to !== nodeId);
    document.querySelector(`[data-node-id="${CSS.escape(nodeId)}"]`)?.remove();
    sfx.click();
    renderThreads();
    syncWorkspaceMeta();
    persistWorkspace();
  }

  function bezierPath(x1, y1, x2, y2, dir = "east") {
    if (dir === "north" || dir === "south") {
      const sign = dir === "south" ? 1 : -1;
      const dist = Math.max(46, Math.abs(y2 - y1) * 0.48);
      return `M ${x1} ${y1} C ${x1} ${y1 + sign * dist}, ${x2} ${y2 - sign * dist}, ${x2} ${y2}`;
    }
    const sign = dir === "west" ? -1 : 1;
    const dist = Math.max(66, Math.abs(x2 - x1) * 0.46);
    return `M ${x1} ${y1} C ${x1 + sign * dist} ${y1}, ${x2 - sign * dist} ${y2}, ${x2} ${y2}`;
  }

  function bezierMidpoint(x1, y1, x2, y2, dir = "east") {
    let cx1, cy1, cx2, cy2;
    if (dir === "north" || dir === "south") {
      const sign = dir === "south" ? 1 : -1;
      const dist = Math.max(46, Math.abs(y2 - y1) * 0.48);
      cx1 = x1; cy1 = y1 + sign * dist;
      cx2 = x2; cy2 = y2 - sign * dist;
    } else {
      const sign = dir === "west" ? -1 : 1;
      const dist = Math.max(66, Math.abs(x2 - x1) * 0.46);
      cx1 = x1 + sign * dist; cy1 = y1;
      cx2 = x2 - sign * dist; cy2 = y2;
    }
    return {
      x: 0.125 * x1 + 0.375 * cx1 + 0.375 * cx2 + 0.125 * x2,
      y: 0.125 * y1 + 0.375 * cy1 + 0.375 * cy2 + 0.125 * y2,
    };
  }

  function getPortWorldCoords(nodeId, side) {
    const n = state.nodes.get(nodeId);
    if (!n) return null;
    const el = document.querySelector(`[data-node-id="${CSS.escape(nodeId)}"]`);
    const w = el ? el.offsetWidth : (n.type === "source" ? 344 : 252);
    const h = el ? el.offsetHeight : 64;
    if (side === "east") return { x: n.x + w, y: n.y + h / 2 };
    if (side === "west") return { x: n.x, y: n.y + h / 2 };
    if (side === "north") return { x: n.x + w / 2, y: n.y };
    if (side === "south") return { x: n.x + w / 2, y: n.y + h };

    const dir = getEffectiveDir(n);
    if (side === "out") {
      if (dir === "west") return { x: n.x, y: n.y + h / 2 };
      if (dir === "north") return { x: n.x + w / 2, y: n.y };
      if (dir === "south") return { x: n.x + w / 2, y: n.y + h };
      return { x: n.x + w, y: n.y + h / 2 };
    }
    // side === "in"
    if (dir === "west") return { x: n.x + w, y: n.y + h / 2 };
    if (dir === "north") return { x: n.x + w / 2, y: n.y + h };
    if (dir === "south") return { x: n.x + w / 2, y: n.y };
    return { x: n.x, y: n.y + h / 2 };
  }

  let _threadsRaf = null;
  function scheduleRenderThreads() {
    if (_threadsRaf !== null) return;
    _threadsRaf = requestAnimationFrame(() => {
      _threadsRaf = null;
      renderThreads();
    });
  }

  function renderThreads() {
    const group = $("threads-group");
    if (!group) return;
    let html = "";
    for (const e of state.edges) {
      const fromN = state.nodes.get(e.from);
      const toN = state.nodes.get(e.to);
      if (!fromN || !toN) continue;
      const dir = (toN.type === "choice" || toN.type === "result" || toN.type === "player")
        ? getEffectiveDir(toN)
        : "east";
      const fromSide = fromN.type === "source" && state.layoutMode === "compass" ? dir : "out";
      const p1 = getPortWorldCoords(e.from, fromSide);
      const p2 = getPortWorldCoords(e.to, "in");
      if (!p1 || !p2) continue;
      const d = bezierPath(p1.x, p1.y, p2.x, p2.y, dir);
      const mid = bezierMidpoint(p1.x, p1.y, p2.x, p2.y, dir);
      const text = (e.label || "THREAD").slice(0, 18);
      const pillW = Math.max(44, text.length * 5.7 + 14);

      html += `
        <g class="thread-group kind-${esc(e.kind)} ${e.active ? "is-active" : ""} ${e.dimmed ? "is-dimmed" : ""}" data-edge-id="${esc(e.id)}" data-from="${esc(e.from)}" data-to="${esc(e.to)}">
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
    group.querySelectorAll(".thread-group").forEach((tg) => {
      const setLinked = (on) => {
        document.querySelector(`[data-node-id="${CSS.escape(tg.dataset.from)}"]`)?.classList.toggle("thread-linked", on);
        document.querySelector(`[data-node-id="${CSS.escape(tg.dataset.to)}"]`)?.classList.toggle("thread-linked", on);
      };
      tg.onpointerenter = () => setLinked(true);
      tg.onpointerleave = () => setLinked(false);
    });
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
    let nextTopY = -140;
    const baseX = state.layoutMode === "compass" ? -172 : -560;
    for (const r of roots) {
      // Lay out first to measure full 4-way subtree height (including North above root!)
      r.x = baseX;
      r.y = nextTopY;
      const el = document.querySelector(`[data-node-id="${CSS.escape(r.id)}"]`);
      if (el) { el.style.left = `${r.x}px`; el.style.top = `${r.y}px`; }
      layoutTreeFromRoot(r.id);

      // Measure actual minY..maxY of this root's entire connected subtree
      const treeNodes = Array.from(state.nodes.values()).filter((n) => n.id === r.id || n.rootSourceId === r.id);
      let minY = Infinity, maxY = -Infinity;
      for (const tn of treeNodes) {
        const tel = document.querySelector(`[data-node-id="${CSS.escape(tn.id)}"]`);
        const th = tel ? tel.offsetHeight : 64;
        minY = Math.min(minY, tn.y);
        maxY = Math.max(maxY, tn.y + th);
      }
      if (Number.isFinite(minY) && minY < nextTopY) {
        const shiftY = nextTopY - minY;
        r.y += shiftY;
        if (el) el.style.top = `${r.y}px`;
        layoutTreeFromRoot(r.id);
        maxY += shiftY;
      }
      nextTopY = (Number.isFinite(maxY) ? maxY : nextTopY + 420) + 140;
    }
    sfx.click();
    renderThreads();
    persistWorkspace();
    fitAllNodes();
  }

  function applyLayoutMode(mode) {
    state.layoutMode = mode === "tree" ? "tree" : "compass";
    localStorage.setItem("neon_layout_mode", state.layoutMode);
    const btn = $("layout-mode-btn");
    const txt = $("layout-mode-text");
    if (btn) btn.classList.toggle("active", state.layoutMode === "compass");
    if (txt) txt.textContent = state.layoutMode === "compass" ? "Compass" : "Tree";
    // Re-render all nodes so directional ports update cleanly, then tidy
    for (const n of state.nodes.values()) renderNode(n);
    tidyGraphLayout();
  }

  /* ---------- Live Preview Stream Cache & Visual Scrubber Helpers ---------- */
  function fmtPreciseTime(sec) {
    const s = Math.max(0, Number(sec) || 0);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const rem = s % 60;
    const secStr = rem < 10 ? `0${rem.toFixed(1)}` : rem.toFixed(1);
    return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${secStr}` : `${m}:${secStr}`;
  }

  function estimateClipFeedback(node, startSec, endSec) {
    const dur = Math.max(0.2, (endSec || 0) - (startSec || 0));
    const act = node.accumulated?.video_output_action;
    if (act === "gif" || act === "webp_anim") {
      const fps = node.gifFps || 12;
      const w = node.gifWidth || 640;
      const scaleFactor = (w / 640) * (w / 640);
      // Empirical lightweight size estimate for palette-dithered GIF vs animated WebP
      const kbpsPerFrame = act === "gif" ? 36 : 12;
      const estMb = (dur * fps * kbpsPerFrame * scaleFactor) / 1024;
      const estStr = estMb < 1 ? `~${Math.round(estMb * 1024)} KB` : `~${estMb.toFixed(1)} MB`;
      if (act === "gif" && dur > 12) {
        return { cls: "warn", left: `${dur.toFixed(1)}s · ${estStr}`, right: "Tip: 3–8s or WebP for small GIFs" };
      }
      return { cls: "good", left: `${dur.toFixed(1)}s · ${fps}fps · ${w}px`, right: `Est. ${estStr}` };
    }
    return { cls: "", left: `Clip length: ${dur.toFixed(1)}s`, right: `${fmtDuration(startSec)} → ${fmtDuration(endSec)}` };
  }

  async function resolvePreviewStreamForSource(srcNode, wantAudioOnly = false) {
    if (!srcNode?.url) return null;
    const cacheKey = wantAudioOnly ? "_previewStreamAudio" : "_previewStreamVideo";
    const promKey = wantAudioOnly ? "_previewPromiseAudio" : "_previewPromiseVideo";
    if (srcNode[cacheKey]) return srcNode[cacheKey];
    if (srcNode[promKey]) return srcNode[promKey];

    srcNode[promKey] = (async () => {
      try {
        const res = await fetch("/v1/stream/resolve", {
          method: "POST",
          headers: apiHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({
            url: srcNode.url,
            mode: wantAudioOnly ? "audio" : "video",
            preferred_quality: "480",
          }),
        });
        if (!res.ok) return null;
        const data = await res.json();
        srcNode[cacheKey] = data;
        return data;
      } catch (_) {
        return null;
      } finally {
        srcNode[promKey] = null;
      }
    })();
    return srcNode[promKey];
  }

  function attachScrubberPreview(el, node) {
    const src = state.nodes.get(node.rootSourceId);
    const mediaEl = el.querySelector("[data-scrubber-media]");
    if (!mediaEl || !src) return;

    const isFrame = node.inputKind === "frame";
    const isAudioOnly = node.accumulated?.mode === "audio";
    const isAnim = node.accumulated?.video_output_action === "gif" || node.accumulated?.video_output_action === "webp_anim";
    const overlay = el.querySelector("[data-scrubber-overlay]");
    const loadBtn = el.querySelector("[data-load-scrubber]");
    const timeBadge = el.querySelector("[data-scrubber-time]");
    const playBtn = el.querySelector("[data-scrubber-play]");
    const loopBtn = el.querySelector("[data-scrubber-loop]");
    const muteBtn = el.querySelector("[data-scrubber-mute]");
    const rail = el.querySelector("[data-timeline-rail]");
    const winEl = el.querySelector("[data-timeline-window]");
    const playheadEl = el.querySelector("[data-timeline-playhead]");
    const hStart = el.querySelector("[data-handle-start]");
    const hEnd = el.querySelector("[data-handle-end]");
    const tStart = el.querySelector("[data-trim-start]");
    const tEnd = el.querySelector("[data-trim-end]");
    const estBox = el.querySelector("[data-scrubber-est]");
    const runBtnLbl = el.querySelector("[data-run-inline-label]");

    let totalDur = Math.max(1, Number(src.probe?.duration_seconds) || 60);
    let streamLoaded = false;

    const syncTimelineVisuals = () => {
      if (isFrame) {
        const cur = Math.max(0, Math.min(totalDur, Number(node.frameSec) || 0));
        const pct = (cur / totalDur) * 100;
        if (playheadEl) playheadEl.style.left = `${pct}%`;
        if (hStart) hStart.style.left = `${pct}%`;
        if (timeBadge) timeBadge.textContent = `${fmtPreciseTime(cur)} / ${fmtDuration(totalDur)}`;
        if (runBtnLbl) runBtnLbl.textContent = `Capture Frame (${fmtPreciseTime(cur)})`;
        return;
      }
      const s = Math.max(0, Math.min(totalDur, parseTime(node.clipStart) || 0));
      const e = Math.max(s + 0.5, Math.min(totalDur, parseTime(node.clipEnd) || Math.min(totalDur, s + 8)));
      const sPct = (s / totalDur) * 100;
      const ePct = (e / totalDur) * 100;
      if (winEl) {
        winEl.style.left = `${sPct}%`;
        winEl.style.width = `${Math.max(1.5, ePct - sPct)}%`;
      }
      if (hStart) hStart.style.left = `${sPct}%`;
      if (hEnd) hEnd.style.left = `${ePct}%`;
      const cur = Number.isFinite(mediaEl.currentTime) ? mediaEl.currentTime : s;
      if (playheadEl) playheadEl.style.left = `${Math.min(100, (cur / totalDur) * 100)}%`;
      if (timeBadge) timeBadge.textContent = `${fmtPreciseTime(cur)} / ${fmtDuration(totalDur)}`;
      if (estBox) {
        const est = estimateClipFeedback(node, s, e);
        estBox.className = `scrubber-est mono ${est.cls}`;
        estBox.innerHTML = `<span>${esc(est.left)}</span><span>${esc(est.right)}</span>`;
      }
    };

    const loadStreamNow = async (autoPlay = false) => {
      if (streamLoaded) {
        if (autoPlay) mediaEl.play().catch(() => {});
        return;
      }
      if (loadBtn) loadBtn.textContent = "Connecting preview…";
      const data = await resolvePreviewStreamForSource(src, isAudioOnly);
      const track = data?.tracks?.find((t) => (isAudioOnly ? t.kind === "audio" : t.kind === "video")) || data?.tracks?.[0];
      if (!track) {
        if (loadBtn) loadBtn.textContent = "Preview unavailable · Use timestamps";
        return;
      }
      streamLoaded = true;
      if (overlay) overlay.classList.add("hidden");
      destroyPlayerHls(node.id);
      if (track.protocol === "hls" && window.Hls && window.Hls.isSupported()) {
        const hls = new window.Hls({ enableWorker: true });
        hls.loadSource(track.stream_url);
        hls.attachMedia(mediaEl);
        playerHls.set(node.id, hls);
      } else {
        mediaEl.src = track.stream_url;
      }
      if (!isAudioOnly) mediaEl.muted = !!node.previewMuted;
      mediaEl.onloadedmetadata = () => {
        if (Number.isFinite(mediaEl.duration) && mediaEl.duration > 0) totalDur = mediaEl.duration;
        const initSeek = isFrame ? (Number(node.frameSec) || 0) : (parseTime(node.clipStart) || 0);
        try { mediaEl.currentTime = Math.min(totalDur - 0.1, Math.max(0, initSeek)); } catch (_) {}
        syncTimelineVisuals();
        if (autoPlay) mediaEl.play().catch(() => {});
      };
    };

    mediaEl.ontimeupdate = () => {
      if (!isFrame && node.loopPreview) {
        const s = parseTime(node.clipStart) || 0;
        const e = parseTime(node.clipEnd) || totalDur;
        if (mediaEl.currentTime >= e) {
          mediaEl.currentTime = s;
        }
      }
      if (isFrame && !mediaEl.paused) {
        node.frameSec = Number(mediaEl.currentTime.toFixed(1));
      }
      syncTimelineVisuals();
    };

    mediaEl.onplay = () => { if (playBtn) playBtn.textContent = "⏸ Pause"; };
    mediaEl.onpause = () => { if (playBtn) playBtn.textContent = isAnim ? "▶ Loop GIF" : "▶ Play"; };

    if (loadBtn) {
      loadBtn.onpointerdown = (e) => e.stopPropagation();
      loadBtn.onclick = (e) => { e.stopPropagation(); sfx.click(); loadStreamNow(true); };
    }

    if (playBtn) {
      playBtn.onpointerdown = (e) => e.stopPropagation();
      playBtn.onclick = async (e) => {
        e.stopPropagation();
        sfx.click();
        if (!streamLoaded) {
          await loadStreamNow(true);
          return;
        }
        if (mediaEl.paused) {
          if (!isFrame) {
            const s = parseTime(node.clipStart) || 0;
            const en = parseTime(node.clipEnd) || totalDur;
            if (mediaEl.currentTime < s || mediaEl.currentTime >= en - 0.15) {
              mediaEl.currentTime = s;
            }
          }
          mediaEl.play().catch(() => {});
        } else {
          mediaEl.pause();
        }
      };
    }

    if (loopBtn) {
      loopBtn.onpointerdown = (e) => e.stopPropagation();
      loopBtn.onclick = (e) => {
        e.stopPropagation();
        node.loopPreview = !node.loopPreview;
        loopBtn.classList.toggle("active", node.loopPreview);
        sfx.click();
      };
    }

    if (muteBtn) {
      muteBtn.onpointerdown = (e) => e.stopPropagation();
      muteBtn.onclick = (e) => {
        e.stopPropagation();
        node.previewMuted = !node.previewMuted;
        mediaEl.muted = node.previewMuted;
        muteBtn.textContent = node.previewMuted ? "Muted" : "Sound";
        sfx.click();
      };
    }

    // Set In / Set Out buttons capture exact live playhead
    const setInBtn = el.querySelector("[data-set-in]");
    const setOutBtn = el.querySelector("[data-set-out]");
    if (setInBtn) {
      setInBtn.onpointerdown = (e) => e.stopPropagation();
      setInBtn.onclick = (e) => {
        e.stopPropagation();
        const cur = Math.max(0, Number(mediaEl.currentTime) || 0);
        const curEnd = parseTime(node.clipEnd) || totalDur;
        node.clipStart = fmtPreciseTime(cur);
        if (cur >= curEnd - 0.5) node.clipEnd = fmtPreciseTime(Math.min(totalDur, cur + (isAnim ? 5 : 10)));
        if (tStart) tStart.value = node.clipStart;
        if (tEnd) tEnd.value = node.clipEnd;
        syncTimelineVisuals();
        sfx.click();
      };
    }
    if (setOutBtn) {
      setOutBtn.onpointerdown = (e) => e.stopPropagation();
      setOutBtn.onclick = (e) => {
        e.stopPropagation();
        const cur = Math.max(0.5, Number(mediaEl.currentTime) || 0);
        const curStart = parseTime(node.clipStart) || 0;
        node.clipEnd = fmtPreciseTime(cur);
        if (cur <= curStart + 0.5) node.clipStart = fmtPreciseTime(Math.max(0, cur - (isAnim ? 5 : 10)));
        if (tStart) tStart.value = node.clipStart;
        if (tEnd) tEnd.value = node.clipEnd;
        syncTimelineVisuals();
        sfx.click();
      };
    }

    // Frame nudge buttons (-5s, -1s, -0.2s, +0.2s, +1s, +5s)
    el.querySelectorAll("[data-frame-nudge]").forEach((btn) => {
      btn.onpointerdown = (e) => e.stopPropagation();
      btn.onclick = async (e) => {
        e.stopPropagation();
        const delta = parseFloat(btn.dataset.frameNudge);
        if (!streamLoaded) await loadStreamNow(false);
        const next = Math.max(0, Math.min(totalDur, (Number(node.frameSec) || 0) + delta));
        node.frameSec = Number(next.toFixed(1));
        mediaEl.pause();
        try { mediaEl.currentTime = node.frameSec; } catch (_) {}
        syncTimelineVisuals();
        sfx.click();
      };
    });

    // GIF FPS / Width quick chips
    el.querySelectorAll("[data-gif-fps]").forEach((btn) => {
      btn.onpointerdown = (e) => e.stopPropagation();
      btn.onclick = (e) => {
        e.stopPropagation();
        node.gifFps = parseInt(btn.dataset.gifFps, 10);
        el.querySelectorAll("[data-gif-fps]").forEach((b) => b.classList.toggle("active", b === btn));
        syncTimelineVisuals();
        sfx.click();
      };
    });
    el.querySelectorAll("[data-gif-width]").forEach((btn) => {
      btn.onpointerdown = (e) => e.stopPropagation();
      btn.onclick = (e) => {
        e.stopPropagation();
        node.gifWidth = parseInt(btn.dataset.gifWidth, 10);
        el.querySelectorAll("[data-gif-width]").forEach((b) => b.classList.toggle("active", b === btn));
        syncTimelineVisuals();
        sfx.click();
      };
    });

    // Interactive Rail Click & Handle Dragging (live-seeks video as you drag!)
    if (rail) {
      const secFromClientX = (clientX) => {
        const r = rail.getBoundingClientRect();
        const pct = Math.max(0, Math.min(1, (clientX - r.left) / Math.max(1, r.width)));
        return pct * totalDur;
      };

      const startDrag = (mode, ev) => {
        ev.stopPropagation();
        ev.preventDefault();
        if (!streamLoaded) loadStreamNow(false);
        const handleEl = mode === "start" ? hStart : mode === "end" ? hEnd : null;
        if (handleEl) handleEl.classList.add("dragging");

        const applyAt = (cx) => {
          const sec = secFromClientX(cx);
          if (isFrame) {
            node.frameSec = Number(sec.toFixed(1));
            mediaEl.pause();
            try { mediaEl.currentTime = node.frameSec; } catch (_) {}
          } else if (mode === "start") {
            const curEnd = parseTime(node.clipEnd) || totalDur;
            const clamped = Math.min(curEnd - 0.5, sec);
            node.clipStart = fmtPreciseTime(clamped);
            if (tStart) tStart.value = node.clipStart;
            try { mediaEl.currentTime = clamped; } catch (_) {}
          } else if (mode === "end") {
            const curStart = parseTime(node.clipStart) || 0;
            const clamped = Math.max(curStart + 0.5, sec);
            node.clipEnd = fmtPreciseTime(clamped);
            if (tEnd) tEnd.value = node.clipEnd;
            try { mediaEl.currentTime = clamped; } catch (_) {}
          } else {
            try { mediaEl.currentTime = sec; } catch (_) {}
          }
          syncTimelineVisuals();
        };

        applyAt(ev.clientX);
        const onMove = (mv) => applyAt(mv.clientX);
        const onUp = () => {
          if (handleEl) handleEl.classList.remove("dragging");
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
      };

      if (hStart) hStart.onpointerdown = (e) => startDrag("start", e);
      if (hEnd) hEnd.onpointerdown = (e) => startDrag("end", e);
      rail.onpointerdown = (e) => {
        if (e.target.closest("[data-handle-start]") || e.target.closest("[data-handle-end]")) return;
        startDrag(isFrame ? "start" : "seek", e);
      };
    }

    // Sync two-way when user types manual timestamps
    if (tStart) {
      tStart.addEventListener("input", () => {
        const s = parseTime(tStart.value);
        if (streamLoaded && Number.isFinite(s)) try { mediaEl.currentTime = s; } catch (_) {}
        syncTimelineVisuals();
      });
    }
    if (tEnd) {
      tEnd.addEventListener("input", () => {
        const en = parseTime(tEnd.value);
        if (streamLoaded && Number.isFinite(en)) try { mediaEl.currentTime = en; } catch (_) {}
        syncTimelineVisuals();
      });
    }

    syncTimelineVisuals();
    // Auto-connect lightweight preview stream for real probed media so user immediately sees frames!
    if (!node.demo && analyzeProbe(src.probe).canStreamLive) {
      loadStreamNow(false);
    }
  }

  /* ---------- Node Renderer & Magnetic Port Wiring ---------- */
  function buildInlinePreviewHtml(dl) {
    if (!dl?.artifact_url) return "";
    const mime = (dl.mime_type || "").toLowerCase();
    const inlineUrl = `${dl.artifact_url}${dl.artifact_url.includes("?") ? "&" : "?"}inline=true`;
    if (mime.startsWith("video/")) return `<div class="inline-player"><video controls preload="metadata" src="${esc(inlineUrl)}"></video></div>`;
    if (mime.startsWith("audio/")) return `<div class="inline-player"><audio controls preload="metadata" src="${esc(inlineUrl)}"></audio></div>`;
    if (mime.startsWith("image/") && !mime.includes("svg")) return `<div class="inline-player"><img src="${esc(inlineUrl)}" alt="${esc(dl.filename)}" /></div>`;
    if (mime.startsWith("text/markdown") || mime.startsWith("text/plain")) {
      return `<div class="inline-text-preview mono" data-fetch-text-preview="${esc(inlineUrl)}">Loading text preview…</div>`;
    }
    if (mime.startsWith("application/pdf")) {
      return `<div class="node-actions" style="margin:6px 0;"><a class="ghost-btn btn-xs mono" href="${esc(inlineUrl)}" target="_blank" rel="noopener">Preview PDF ↗</a></div>`;
    }
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

    const dirCls = (node.type === "choice" || node.type === "result" || node.type === "player")
      ? `dir-${getEffectiveDir(node)}`
      : "";
    const hasVisualPreview = node.type === "choice" && !!node.panelOpen && ["trim", "frame", "sub_preview", "article_preview", "assets_preview", "image_studio", "playlist_range"].includes(node.inputKind);
    const extraCls = node.type === "choice"
      ? `theme-${node.theme} ${dirCls} ${node.isLeaf ? "is-leaf" : ""} ${node.inputKind && node.panelOpen ? "choice-interactive" : ""} ${hasVisualPreview ? "choice-has-preview" : ""} ${node.expanded || node.panelOpen ? "path-lit" : ""}`
      : node.type === "player" ? `${dirCls} ${node.theater ? "theater" : ""}`
      : node.type === "result" ? `${dirCls} ${node.job?.status === "completed" ? "is-complete" : ""}`
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
        ? (node.panelOpen ? `<span>Close ▴</span>` : `<span>Open ▾</span>`)
        : node.isLeaf
          ? `${iconSvg("play", "ico-xs")}<span>Run</span>`
          : (node.expanded ? `<span class="detected-dot" style="background:currentColor;"></span>` : iconSvg("chevron-right", "ico-xs"));
      let inlineExtra = "";

      if (node.panelOpen && node.inputKind === "trim") {
        const srcNode = state.nodes.get(node.rootSourceId);
        const srcDur = srcNode?.probe?.duration_seconds;
        const poster = srcNode?.probe?.thumbnail?.url || "";
        const chapters = srcNode?.probe?.metadata?.chapters || [];
        const isAudioOnly = node.accumulated?.mode === "audio";
        const act = node.accumulated?.video_output_action;
        const isAnim = act === "gif" || act === "webp_anim";
        const presets = buildTrimPresets(srcDur);
        const chapDots = srcDur
          ? chapters.map((c) => `<span class="timeline-chapter-dot" style="left:${Math.min(100, (c.start_seconds / srcDur) * 100)}%" title="${esc(c.title)}"></span>`).join("")
          : "";
        const ctaText = act === "gif" ? "Create Crisp GIF" : act === "webp_anim" ? "Create Animated WebP" : "Extract Clip";

        inlineExtra = `
          <div class="choice-inline-body">
            <div class="scrubber-stage ${isAudioOnly ? "audio-only" : ""}">
              ${isAudioOnly
                ? `<audio controls preload="metadata" data-scrubber-media></audio>`
                : `<video playsinline ${node.previewMuted ? "muted" : ""} poster="${esc(poster)}" data-scrubber-media></video>`
              }
              <div class="scrubber-hud-top">
                <span class="scrubber-badge mono num" data-scrubber-time>${esc(node.clipStart)} / ${srcDur ? esc(fmtDuration(srcDur)) : "--:--"}</span>
                ${!isAudioOnly ? `<button type="button" class="scrubber-mute-btn mono" data-scrubber-mute>${node.previewMuted ? "Muted" : "Sound"}</button>` : ""}
              </div>
              ${!isAudioOnly ? `
                <div class="scrubber-poster-overlay" data-scrubber-overlay>
                  <button type="button" class="scrubber-load-btn" data-load-scrubber>${iconSvg("play", "ico-xs")}<span>Load Live Scrubber</span></button>
                </div>
              ` : ""}
            </div>

            <div class="timeline-wrap">
              <div class="timeline-rail" data-timeline-rail title="Click to seek · Drag handles to set start & end">
                ${chapDots}
                <div class="timeline-window" data-timeline-window></div>
                <div class="timeline-playhead" data-timeline-playhead></div>
                <div class="timeline-handle" data-handle-start title="Drag clip start"></div>
                <div class="timeline-handle" data-handle-end title="Drag clip end"></div>
              </div>
            </div>

            <div class="scrubber-controls">
              <button type="button" class="scrubber-btn" data-scrubber-play>${isAnim ? "▶ Loop GIF" : "▶ Play"}</button>
              <button type="button" class="scrubber-btn ${node.loopPreview ? "active" : ""}" data-scrubber-loop title="Loop selected clip range">↻ Loop</button>
              <button type="button" class="scrubber-btn" data-set-in title="Set clip start to current playhead">[ Set In</button>
              <button type="button" class="scrubber-btn" data-set-out title="Set clip end to current playhead">Set Out ]</button>
            </div>

            <div class="scrubber-est mono" data-scrubber-est></div>

            ${isAnim ? `
              <div class="gif-tune-row">
                <span class="mono muted" style="font-size:9px;">FPS</span>
                ${[8, 12, 15].map((f) => `<button type="button" class="scrubber-btn ${node.gifFps === f ? "active" : ""}" data-gif-fps="${f}">${f}</button>`).join("")}
                <span class="mono muted" style="font-size:9px;margin-left:4px;">WIDTH</span>
                ${[480, 640].map((w) => `<button type="button" class="scrubber-btn ${node.gifWidth === w ? "active" : ""}" data-gif-width="${w}">${w}px</button>`).join("")}
              </div>
            ` : ""}

            <div class="trim-presets">
              ${presets.map((pr) => `<button type="button" class="trim-chip" data-trim-preset="${esc(pr.start)}|${esc(pr.end)}">${esc(pr.label)}</button>`).join("")}
            </div>
            <div class="cfg-row">
              <input type="text" class="cfg-input mono" data-trim-start value="${esc(node.clipStart)}" placeholder="0:00" />
              <span class="muted">→</span>
              <input type="text" class="cfg-input mono" data-trim-end value="${esc(node.clipEnd)}" placeholder="0:06" />
            </div>
            <button class="reactor-fire btn-sm" data-run-inline-leaf="${esc(node.id)}">${iconSvg("play", "ico-xs")}<span data-run-inline-label>${esc(ctaText)}</span></button>
          </div>
        `;
      } else if (node.panelOpen && node.inputKind === "frame") {
        const srcNode = state.nodes.get(node.rootSourceId);
        const srcDur = srcNode?.probe?.duration_seconds;
        const poster = srcNode?.probe?.thumbnail?.url || "";
        inlineExtra = `
          <div class="choice-inline-body">
            <div class="scrubber-stage">
              <video playsinline muted poster="${esc(poster)}" data-scrubber-media></video>
              <div class="scrubber-hud-top">
                <span class="scrubber-badge mono num" data-scrubber-time>${esc(fmtPreciseTime(node.frameSec || 0))} / ${srcDur ? esc(fmtDuration(srcDur)) : "--:--"}</span>
              </div>
              <div class="scrubber-poster-overlay" data-scrubber-overlay>
                <button type="button" class="scrubber-load-btn" data-load-scrubber>${iconSvg("camera", "ico-xs")}<span>Load Frame Preview</span></button>
              </div>
            </div>
            <div class="timeline-wrap">
              <div class="timeline-rail" data-timeline-rail title="Scrub to exact frame">
                <div class="timeline-playhead" data-timeline-playhead></div>
                <div class="timeline-handle" data-handle-start title="Drag to pick exact frame"></div>
              </div>
            </div>
            <div class="scrubber-controls">
              <button type="button" class="scrubber-btn" data-scrubber-play>▶ Play</button>
              <button type="button" class="scrubber-btn" data-frame-nudge="-5">−5s</button>
              <button type="button" class="scrubber-btn" data-frame-nudge="-1">−1s</button>
              <button type="button" class="scrubber-btn" data-frame-nudge="-0.2">−.2s</button>
              <button type="button" class="scrubber-btn" data-frame-nudge="0.2">+.2s</button>
              <button type="button" class="scrubber-btn" data-frame-nudge="1">+1s</button>
              <button type="button" class="scrubber-btn" data-frame-nudge="5">+5s</button>
            </div>
            <button class="reactor-fire btn-sm" data-run-inline-leaf="${esc(node.id)}">${iconSvg("camera", "ico-xs")}<span data-run-inline-label>Capture Frame (${esc(fmtPreciseTime(node.frameSec || 0))})</span></button>
          </div>
        `;
      } else if (node.panelOpen && node.inputKind === "sub_preview") {
        inlineExtra = `
          <div class="choice-inline-body">
            <div class="preview-search-row">
              <input type="text" class="cfg-input mono" data-sub-search placeholder="Search captions / find quote…" />
              <button type="button" class="scrubber-btn" data-sub-copy title="Copy full plain text transcript">Copy Text</button>
            </div>
            <div class="preview-scroll-box" data-sub-cues>
              <span class="mono muted" style="font-size:10px;padding:8px;">Loading live caption preview…</span>
            </div>
            <div class="scrubber-est mono" data-sub-meta><span>Inspecting caption track…</span><span>Click any line to copy</span></div>
            <div class="gif-tune-row">
              <span class="mono muted" style="font-size:9px;">EXPORT</span>
              ${[["vtt", "VTT + TXT"], ["srt", "SRT (Editors)"]].map(([f, lbl]) => `<button type="button" class="scrubber-btn ${node.subExportFmt === f ? "active" : ""}" data-sub-fmt="${f}">${lbl}</button>`).join("")}
            </div>
            <button class="reactor-fire btn-sm" data-run-inline-leaf="${esc(node.id)}">${iconSvg("download", "ico-xs")}<span>Download Captions</span></button>
          </div>
        `;
      } else if (node.panelOpen && node.inputKind === "article_preview") {
        const ws = state.nodes.get(node.rootSourceId)?.probe?.metadata?.extra?.webpage_stats || {};
        const excerpt = ws.excerpt || "Article text ready for clean extraction.";
        inlineExtra = `
          <div class="choice-inline-body">
            <div class="scrubber-est mono good">
              <span>${ws.reading_minutes ? `~${ws.reading_minutes} min read` : "Article Ready"}</span>
              <span>${(ws.word_count || 0).toLocaleString()} words</span>
            </div>
            <div class="article-excerpt-box">${esc(excerpt)}${excerpt.length >= 740 ? "\n…" : ""}</div>
            <div class="scrubber-controls">
              <button type="button" class="scrubber-btn ${node.articleFmt === "markdown" ? "active" : ""}" data-art-fmt="markdown">Markdown (.md)</button>
              <button type="button" class="scrubber-btn ${node.articleFmt === "text" ? "active" : ""}" data-art-fmt="text">Plain Text (.txt)</button>
              <button type="button" class="scrubber-btn" data-art-copy style="margin-left:auto;">Copy Excerpt</button>
            </div>
            <button class="reactor-fire btn-sm" data-run-inline-leaf="${esc(node.id)}">${iconSvg("file-text", "ico-xs")}<span data-art-cta>Download ${node.articleFmt === "text" ? "Plain Text (.txt)" : "Clean Markdown (.md)"}</span></button>
          </div>
        `;
      } else if (node.panelOpen && node.inputKind === "assets_preview") {
        const srcNode = state.nodes.get(node.rootSourceId);
        const assets = srcNode?.probe?.metadata?.extra?.webpage_assets_preview || { svgs: [], images: [] };
        const svgs = assets.svgs || [];
        const imgs = assets.images || [];
        inlineExtra = `
          <div class="choice-inline-body">
            <div class="scrubber-est mono">
              <span>${svgs.length} SVG${svgs.length === 1 ? "" : "s"} · ${imgs.length} image${imgs.length === 1 ? "" : "s"} previewed</span>
              <span>Click tile to copy / spawn</span>
            </div>
            <div class="asset-grid">
              ${svgs.map((s, idx) => `<button type="button" class="asset-tile is-svg" data-copy-svg-idx="${idx}" title="Click to copy raw SVG code"><img src="data:image/svg+xml;charset=utf-8,${encodeURIComponent(s)}" alt="SVG ${idx + 1}" /><span class="asset-tile-tag mono">SVG</span></button>`).join("")}
              ${imgs.map((u, idx) => `<button type="button" class="asset-tile" data-spawn-asset-img="${esc(u)}" title="Click to open this single image on canvas"><img src="${esc(u)}" alt="Asset ${idx + 1}" loading="lazy" /><span class="asset-tile-tag mono">IMG</span></button>`).join("")}
            </div>
            <button class="reactor-fire btn-sm" data-run-inline-leaf="${esc(node.id)}">${iconSvg("archive", "ico-xs")}<span>Download All as ZIP</span></button>
          </div>
        `;
      } else if (node.panelOpen && node.inputKind === "image_studio") {
        const srcNode = state.nodes.get(node.rootSourceId);
        const cap = analyzeProbe(srcNode?.probe);
        const imgUrl = (node.accumulated?._useThumbnailUrl && cap.thumbnail?.url) || cap.thumbnail?.url || srcNode?.url || "";
        const sw = cap.sourceWidth;
        const sh = cap.sourceHeight;
        const aspect = sw && sh ? (sw / sh) : null;
        const dimLabel = sw && sh ? `${sw}×${sh}` : "Source HD";
        const widthChoices = [null, 1920, 1280, 800].filter((w) => w === null || !sw || w < sw);
        inlineExtra = `
          <div class="choice-inline-body">
            ${imgUrl ? `
              <div class="img-studio-stage">
                <img src="${esc(imgUrl)}" alt="" />
                <span class="img-dim-badge mono" data-img-dim-badge>${esc(dimLabel)}</span>
              </div>
            ` : ""}
            <div class="gif-tune-row">
              <span class="mono muted" style="font-size:9px;">FMT</span>
              ${[["webp", "WebP"], ["png", "PNG"], ["jpg", "JPEG"], ["original", "Orig"]].map(([f, lbl]) => `<button type="button" class="scrubber-btn ${node.imgFmt === f ? "active" : ""}" data-img-fmt="${f}">${lbl}</button>`).join("")}
            </div>
            <div class="gif-tune-row">
              <span class="mono muted" style="font-size:9px;">WIDTH</span>
              ${widthChoices.map((w) => {
                const lbl = w === null ? "Full" : (aspect ? `${w}×${Math.round(w / aspect)}` : `${w}px`);
                return `<button type="button" class="scrubber-btn ${node.imgMaxWidth === w ? "active" : ""}" data-img-w="${w === null ? "" : w}">${lbl}</button>`;
              }).join("")}
            </div>
            <div class="scrubber-controls">
              <span class="mono muted" style="font-size:9px;">QUALITY</span>
              <input type="range" class="range-track" style="flex:1;" min="50" max="100" step="5" value="${node.imgQuality}" data-img-q />
              <span class="mono num" style="font-size:10px;" data-img-q-val>${node.imgQuality}%</span>
              <button type="button" class="scrubber-btn ${node.imgStripExif ? "active" : ""}" data-img-exif title="Remove GPS & camera EXIF metadata">No EXIF</button>
            </div>
            <button class="reactor-fire btn-sm" data-run-inline-leaf="${esc(node.id)}">${iconSvg("download", "ico-xs")}<span>Export Image</span></button>
          </div>
        `;
      } else if (node.panelOpen && node.inputKind === "playlist_range") {
        const srcItems = state.nodes.get(node.rootSourceId)?.probe?.items || [];
        const hasItems = srcItems.length > 0;
        inlineExtra = `
          <div class="choice-inline-body">
            ${hasItems ? `
              <div class="preview-search-row">
                <input type="text" class="cfg-input mono" data-pl-filter placeholder="Filter ${srcItems.length} items…" />
                <button type="button" class="scrubber-btn" data-pl-quick="1-5">1–5</button>
                <button type="button" class="scrubber-btn" data-pl-quick="all">All</button>
              </div>
              <div class="picker-list" data-pl-list>
                ${srcItems.slice(0, 40).map((it, idx) => {
                  const num = it.index || (idx + 1);
                  return `
                    <label class="picker-row" data-pl-title="${esc((it.title || "").toLowerCase())}">
                      <input type="checkbox" data-pl-chk="${num}" ${num <= 5 ? "checked" : ""} />
                      <span class="mono muted" style="font-size:9.5px;min-width:18px;">#${num}</span>
                      ${it.thumbnail_url ? `<img class="picker-thumb" src="${esc(it.thumbnail_url)}" alt="" loading="lazy" />` : ""}
                      <span class="picker-title">${esc(it.title || `Item #${num}`)}</span>
                      ${it.duration_seconds ? `<span class="mono muted" style="font-size:9px;">${esc(fmtDuration(it.duration_seconds))}</span>` : ""}
                      <button type="button" class="picker-spawn-btn mono" data-pl-spawn="${esc(it.direct_media_url || it.url)}" title="Open single item on canvas">↗</button>
                    </label>
                  `;
                }).join("")}
              </div>
            ` : ""}
            <input type="text" class="cfg-input mono" data-pl-range value="${esc(node.playlistRangeVal || "1-5")}" placeholder="e.g. 1-5 or 1,3,7" />
            <button class="reactor-fire btn-sm" data-run-inline-leaf="${esc(node.id)}">${iconSvg("play", "ico-xs")}<span>Extract Selected</span></button>
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
      const cap = analyzeProbe(p);
      const canPlayLive = cap.canStreamLive;
      const thumbHtml = node.probing && !p?.thumbnail?.url
        ? `<div class="node-thumb-skeleton mono">PROBING REAL CONTENT…</div>`
        : p?.thumbnail?.url
          ? `<div class="node-thumb">
               <img src="${esc(p.thumbnail.url)}" alt="" />
               ${canPlayLive ? `<button type="button" class="thumb-play-overlay" data-watch-source="${esc(node.id)}" title="Watch Live (Zero Download)"><span class="thumb-play-circle">${iconSvg("play", "ico-sm")}</span></button>` : ""}
               ${p.is_live || p.duration_seconds ? `<span class="dur-badge mono num ${p.is_live ? "is-live-badge" : ""}">${p.is_live ? "● LIVE" : fmtDuration(p.duration_seconds)}</span>` : ""}
             </div>` : "";
      const metaBits = node.probeError
        ? [shortHost(node.url), node.probeError]
        : p
          ? [p.source?.domain, p.author?.name, p.content_type?.toUpperCase(), p.size_bytes ? fmtBytes(p.size_bytes) : null, "Neon Sync"].filter(Boolean)
          : [shortHost(node.url), node.probing ? "Inspecting real streams & tracks…" : "Ready"];
      const caps = [];
      if (p) {
        if (cap.videoTiers.length) {
          const top = cap.videoTiers[0];
          if (top.height) caps.push(top.height >= 2160 ? "4K UHD" : `${top.height}p`);
          if (top.maxFps && top.maxFps >= 48) caps.push(`${Math.round(top.maxFps)}fps`);
          caps.push(`${cap.videoTiers.length} quality tier${cap.videoTiers.length === 1 ? "" : "s"}`);
        }
        if (cap.chapters.length) caps.push(`${cap.chapters.length} chapters`);
        if (cap.subtitles.length) caps.push(`${cap.subtitles.length} captions`);
        if (cap.itemCount > 1) caps.push(`${cap.itemCount} ${cap.feedType === "podcast" ? "episodes" : "items"}`);
        if (cap.webpageStats?.reading_minutes) caps.push(`~${cap.webpageStats.reading_minutes} min read`);
        if (cap.webpageStats?.svg_count) caps.push(`${cap.webpageStats.svg_count} SVGs`);
        if (cap.webpageStats?.image_count) caps.push(`${cap.webpageStats.image_count} imgs`);
        if (cap.thumbnail) caps.push("Cover");
      }
      const statusCls = node.probeError ? "err" : node.probing ? "warn" : "ok";
      const statusTxt = node.probeError ? "error" : node.probing ? "probing" : (p?.content_type || "ready");

      const sourcePorts = state.layoutMode === "compass"
        ? `
          <div class="port port-north" data-port="north" data-node="${esc(node.id)}"><span class="port-tip mono">Live &amp; Snapshots</span></div>
          <div class="port port-east port-out" data-port="east" data-node="${esc(node.id)}"><span class="port-tip mono">Drag to link or spawn</span></div>
          <div class="port port-south" data-port="south" data-node="${esc(node.id)}"><span class="port-tip mono">Images &amp; Studio</span></div>
          <div class="port port-west" data-port="west" data-node="${esc(node.id)}"><span class="port-tip mono">Audio &amp; Captions</span></div>
        `
        : `
          <div class="port port-in" data-port="in" data-node="${esc(node.id)}"></div>
          <div class="port port-out" data-port="out" data-node="${esc(node.id)}"><span class="port-tip mono">Drag to link or spawn path</span></div>
        `;

      inner = `
        ${sourcePorts}
        <div class="node-head" data-drag-handle="1">
          <span class="node-type-pill mono"><span class="node-type-dot"></span>SOURCE · ${esc(shortHost(node.url).toUpperCase())}</span>
          <div class="node-head-right">
            <span class="status-pill mono ${statusCls}">${esc(statusTxt)}</span>
            <button class="node-icon-btn" data-copy-url="${esc(node.url)}" title="Copy source URL">⧉</button>
            <button class="node-icon-btn" data-reprobe="${esc(node.id)}" title="Re-probe source">↻</button>
            <button class="node-icon-btn" data-node-collapse="${esc(node.id)}" title="Collapse / expand">${node.collapsed ? "▸" : "▾"}</button>
            <button class="node-icon-btn" data-node-del="${esc(node.id)}" title="Remove node">×</button>
          </div>
        </div>
        <div class="node-body">
          <h3 class="node-title">${esc(p?.title || node.title || node.url)}</h3>
          <div class="node-body-collapsible">
            ${thumbHtml}
            <div class="node-meta mono" ${node.probeError ? 'style="color:var(--err);"' : ""}>${esc(metaBits.join(" · "))}</div>
            ${caps.length ? `<div class="node-caps">${caps.map((c) => `<span class="cap-tag mono">${esc(c)}</span>`).join("")}</div>` : ""}
            ${canPlayLive ? `
              <div class="source-quick-bar">
                <button class="watch-pill-btn" data-watch-source="${esc(node.id)}" title="Stream & watch live immediately without downloading">
                  ${iconSvg("play", "ico-xs")}
                  <span>${cap.liveMode === "audio" ? "Listen Live" : "Watch Live"}</span>
                </button>
                <span class="mono muted" style="font-size:9.5px;">ZERO DOWNLOAD · VLC MODE</span>
              </div>
            ` : ""}
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
      const mediaInfo = j.result?.metadata?.extra?.media_analysis;
      const title = dl?.filename || j.result?.title || j.url || "Extraction";

      const metaBits = [];
      if (isErr) metaBits.push(j.error || j.status);
      else if (isDone) metaBits.push(fmtBytes(dl?.size_bytes));
      else metaBits.push(j.progress?.message || j.status || "queued");
      if (isActive && j.progress?.speed_bps) metaBits.push(`${fmtBytes(j.progress.speed_bps)}/s`);
      if (mediaInfo) {
        const c = [mediaInfo.video_codec, mediaInfo.audio_codec, mediaInfo.width && mediaInfo.height ? `${mediaInfo.width}×${mediaInfo.height}` : null].filter(Boolean).join(" · ");
        if (c) metaBits.push(c);
      }
      if (j.created_at) metaBits.push(fmtRelativeTime(j.created_at));

      const extBadge = dl?.filename && dl.filename.includes(".") ? dl.filename.split(".").pop().toUpperCase() : "NEON SYNC";
      inner = `
        <div class="port port-in" data-port="in" data-node="${esc(node.id)}"></div>
        <div class="port port-out" data-port="out" data-node="${esc(node.id)}"><span class="port-tip mono">Drag to link or annotate</span></div>
        <div class="node-head" data-drag-handle="1">
          <span class="node-type-pill mono"><span class="node-type-dot"></span>ARTIFACT · ${esc(extBadge)}</span>
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
    } else if (node.type === "player") {
      const st = node.stream;
      const tracks = st?.tracks || [];
      const activeTrack = tracks.find((t) => t.stream_id === node.activeStreamId) || tracks[0] || null;
      const isAudioOnly = activeTrack?.kind === "audio" || node.playMode === "audio";
      const statusCls = node.error ? "err" : node.loading ? "warn" : "ok";
      const statusTxt = node.error ? "error" : node.loading ? "buffering" : (st?.is_live ? "LIVE" : "streaming");
      const speedVal = node.playbackRate || 1;
      const subTracksHtml = (st?.subtitles || [])
        .map((s, idx) => `<track kind="subtitles" srclang="${esc(s.language)}" label="${esc(s.name || s.language.toUpperCase())}" src="${esc(s.url)}" ${idx === 0 ? "" : ""} />`)
        .join("");

      let stageHtml = "";
      if (node.error) {
        stageHtml = `<div class="player-stage"><div class="player-loading"><span class="mono" style="color:var(--err);padding:0 16px;text-align:center;">${esc(node.error)}</span></div></div>`;
      } else if (node.loading || !activeTrack) {
        stageHtml = `<div class="player-stage"><div class="player-loading"><div class="player-spinner"></div><span class="mono">Resolving Neon Sync network stream…</span></div></div>`;
      } else if (isAudioOnly) {
        stageHtml = `
          <div class="player-stage audio-stage">
            <div class="audio-stage-top">
              ${st.thumbnail_url ? `<img class="audio-cover" src="${esc(st.thumbnail_url)}" alt="" />` : ""}
              <div style="min-width:0;">
                <div style="font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(st.title)}</div>
                <div class="mono muted" style="font-size:10.5px;">${esc(activeTrack.label)} · Zero Download Stream</div>
              </div>
            </div>
            <audio controls autoplay playsinline data-media-el="${esc(node.id)}"></audio>
          </div>
        `;
      } else {
        stageHtml = `
          <div class="player-stage">
            <video controls autoplay playsinline crossorigin="anonymous" poster="${esc(st.thumbnail_url || "")}" data-media-el="${esc(node.id)}">
              ${subTracksHtml}
            </video>
          </div>
        `;
      }

      const chaptersHtml = (st?.chapters || []).length
        ? `<div class="player-chapters">${st.chapters.map((ch) => `<button class="chapter-chip" data-seek-to="${ch.start_seconds}">${esc(fmtDuration(ch.start_seconds))} ${esc(ch.title.slice(0, 24))}</button>`).join("")}</div>`
        : "";

      inner = `
        <div class="port port-in" data-port="in" data-node="${esc(node.id)}"></div>
        <div class="port port-out" data-port="out" data-node="${esc(node.id)}"><span class="port-tip mono">Drag to link</span></div>
        <div class="node-head" data-drag-handle="1">
          <span class="node-type-pill mono"><span class="node-type-dot"></span>LIVE STREAM ${activeTrack ? `· ${esc(activeTrack.protocol.toUpperCase())}` : ""}</span>
          <div class="node-head-right">
            <span class="status-pill mono ${statusCls}">${esc(statusTxt)}</span>
            <button class="node-icon-btn" data-player-theater="${esc(node.id)}" title="Toggle wide theater mode">${node.theater ? "⤢" : "⤡"}</button>
            <button class="node-icon-btn" data-node-collapse="${esc(node.id)}">${node.collapsed ? "▸" : "▾"}</button>
            <button class="node-icon-btn" data-node-del="${esc(node.id)}">×</button>
          </div>
        </div>
        <div class="node-body">
          <h3 class="node-title">${esc(st?.title || node.title || node.url)}</h3>
          <div class="node-body-collapsible">
            ${stageHtml}
            ${activeTrack ? `
              <div class="player-deck">
                <div class="row-gap">
                  <select class="player-select" data-player-track="${esc(node.id)}" title="Switch stream quality or audio track">
                    ${tracks.map((t) => `<option value="${esc(t.stream_id)}" ${t.stream_id === activeTrack.stream_id ? "selected" : ""}>${esc(t.label)}</option>`).join("")}
                  </select>
                  <select class="player-select" data-player-speed="${esc(node.id)}" title="Playback speed">
                    ${[0.5, 0.75, 1, 1.25, 1.5, 2].map((sp) => `<option value="${sp}" ${sp === speedVal ? "selected" : ""}>${sp}×</option>`).join("")}
                  </select>
                </div>
                <div class="row-gap">
                  ${!isAudioOnly ? `<button class="ghost-btn btn-xs mono" data-player-pip="${esc(node.id)}" title="Float video in Picture-in-Picture">PiP</button>` : ""}
                  <button class="ghost-btn btn-xs mono" data-copy-stream-url="${esc(activeTrack.stream_url)}" title="Copy Network Stream URL for VLC (⌘N)">Copy VLC URL</button>
                  <a class="ghost-btn btn-xs mono" href="${esc(activeTrack.m3u_url)}" download title="Open directly in VLC / IINA / mpv">.m3u ↗</a>
                </div>
              </div>
              ${chaptersHtml}
            ` : ""}
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

    // Drag handle (also allow dragging choice nodes if pointer moves >5px, else click blooms!)
    const dragTarget = el.querySelector("[data-drag-handle]") || (node.type === "choice" ? el.querySelector(".choice-card") : null);
    if (dragTarget) {
      dragTarget.onpointerdown = (e) => {
        if (state.spaceHeld) return;
        if (e.target.closest("button") || e.target.closest("input") || e.target.closest(".port")) return;
        e.stopPropagation();
        const sx = e.clientX, sy = e.clientY, ox = node.x, oy = node.y;
        let moved = false;
        const onMove = (mv) => {
          if (!moved && Math.hypot(mv.clientX - sx, mv.clientY - sy) > 5) {
            moved = true;
            el.classList.add("dragging");
          }
          if (!moved) return;
          node.x = ox + (mv.clientX - sx) / camera.zoom;
          node.y = oy + (mv.clientY - sy) / camera.zoom;
          el.style.left = `${node.x}px`;
          el.style.top = `${node.y}px`;
          scheduleRenderThreads();
        };
        const onUp = () => {
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
          el.classList.remove("dragging");
          if (!moved && node.type === "choice") {
            onChoiceClicked(node);
          } else if (moved) {
            renderThreads();
            if (!node.demo) persistWorkspace();
          }
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
      };

      // Double-click node header -> smoothly glide & focus camera on this node
      if (node.type !== "choice") {
        dragTarget.ondblclick = (e) => {
          if (e.target.closest("button")) return;
          e.stopPropagation();
          sfx.click();
          glideToWorld(node.x + el.offsetWidth / 2, node.y + el.offsetHeight / 2, Math.max(camera.zoom, 1));
        };
      }
    }

    // Inline inputs & quick presets on Trim nodes
    const tStart = el.querySelector("[data-trim-start]");
    if (tStart) { tStart.onpointerdown = (e) => e.stopPropagation(); tStart.oninput = () => { node.clipStart = tStart.value; }; }
    const tEnd = el.querySelector("[data-trim-end]");
    if (tEnd) { tEnd.onpointerdown = (e) => e.stopPropagation(); tEnd.oninput = () => { node.clipEnd = tEnd.value; }; }
    el.querySelectorAll("[data-trim-preset]").forEach((btn) => {
      btn.onpointerdown = (e) => e.stopPropagation();
      btn.onclick = (e) => {
        e.stopPropagation();
        const [s, en] = btn.dataset.trimPreset.split("|");
        node.clipStart = s;
        node.clipEnd = en;
        if (tStart) { tStart.value = s; tStart.dispatchEvent(new Event("input")); }
        if (tEnd) { tEnd.value = en; tEnd.dispatchEvent(new Event("input")); }
        sfx.click();
      };
    });
    if (node.type === "choice" && node.panelOpen && (node.inputKind === "trim" || node.inputKind === "frame")) {
      attachScrubberPreview(el, node);
    }

    // Re-run tree layout whenever async images load inside Source / Image Studio / Result nodes
    el.querySelectorAll(".node-thumb img, .img-studio-stage img").forEach((imgEl) => {
      imgEl.onload = () => {
        const rootId = node.type === "source" ? node.id : node.rootSourceId;
        if (rootId) layoutTreeFromRoot(rootId);
      };
    });

    // Interactive Live Caption Reader & Search wiring
    if (node.type === "choice" && node.panelOpen && node.inputKind === "sub_preview") {
      const src = state.nodes.get(node.rootSourceId);
      const cuesHost = el.querySelector("[data-sub-cues]");
      const searchInput = el.querySelector("[data-sub-search]");
      const metaEl = el.querySelector("[data-sub-meta]");
      const copyBtn = el.querySelector("[data-sub-copy]");
      let loadedCues = [];
      let loadedPlain = "";

      const renderCues = (query = "") => {
        if (!cuesHost) return;
        const q = query.trim().toLowerCase();
        const filtered = q ? loadedCues.filter((c) => c.text.toLowerCase().includes(q)) : loadedCues;
        if (!filtered.length) {
          cuesHost.innerHTML = `<span class="mono muted" style="font-size:10px;padding:8px;">${q ? "No matching lines." : "No caption lines found."}</span>`;
          return;
        }
        cuesHost.innerHTML = filtered.slice(0, 120).map((c) => {
          const safe = esc(c.text);
          const highlighted = q ? safe.replace(new RegExp(`(${q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "ig"), '<mark class="cue-mark">$1</mark>') : safe;
          return `<div class="cue-row" data-copy-cue="${esc(`[${c.start}] ${c.text}`)}" title="Click to copy timestamped line"><span class="cue-time mono">${esc(c.start)}</span><span>${highlighted}</span></div>`;
        }).join("");
        cuesHost.querySelectorAll("[data-copy-cue]").forEach((row) => {
          row.onpointerdown = (e) => e.stopPropagation();
          row.onclick = async (e) => {
            e.stopPropagation();
            try { await navigator.clipboard.writeText(row.dataset.copyCue); toast("Copied caption line!", "ok"); sfx.click(); } catch (_) {}
          };
        });
        if (metaEl) metaEl.innerHTML = `<span>${filtered.length} / ${loadedCues.length} lines</span><span>Click any line to copy</span>`;
      };

      if (searchInput) {
        searchInput.onpointerdown = (e) => e.stopPropagation();
        searchInput.oninput = () => renderCues(searchInput.value);
      }
      if (copyBtn) {
        copyBtn.onpointerdown = (e) => e.stopPropagation();
        copyBtn.onclick = async (e) => {
          e.stopPropagation();
          if (!loadedPlain) return toast("Transcript still loading…", "err");
          try { await navigator.clipboard.writeText(loadedPlain); toast("Full transcript copied to clipboard!", "ok"); sfx.complete(); } catch (_) {}
        };
      }
      el.querySelectorAll("[data-sub-fmt]").forEach((btn) => {
        btn.onpointerdown = (e) => e.stopPropagation();
        btn.onclick = (e) => {
          e.stopPropagation();
          node.subExportFmt = btn.dataset.subFmt;
          el.querySelectorAll("[data-sub-fmt]").forEach((b) => b.classList.toggle("active", b === btn));
          sfx.click();
        };
      });

      if (src?.url && !node.demo) {
        const lang = (node.accumulated?.subtitle_languages || ["en"])[0];
        const subMode = node.accumulated?.subtitle_mode || "prefer_manual";
        fetch("/v1/preview/subtitles", {
          method: "POST",
          headers: apiHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ url: src.url, language: lang, subtitle_mode: subMode }),
        })
          .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
          .then((d) => {
            loadedCues = d.cues || [];
            loadedPlain = d.plain_text || "";
            renderCues(searchInput?.value || "");
            if (node.rootSourceId) layoutTreeFromRoot(node.rootSourceId);
          })
          .catch(() => {
            if (cuesHost) cuesHost.innerHTML = `<span class="mono muted" style="font-size:10px;padding:8px;">Live preview unavailable — click Download Captions below.</span>`;
          });
      }
    }

    // Interactive Article Preview wiring
    if (node.type === "choice" && node.panelOpen && node.inputKind === "article_preview") {
      const ws = state.nodes.get(node.rootSourceId)?.probe?.metadata?.extra?.webpage_stats || {};
      el.querySelectorAll("[data-art-fmt]").forEach((btn) => {
        btn.onpointerdown = (e) => e.stopPropagation();
        btn.onclick = (e) => {
          e.stopPropagation();
          node.articleFmt = btn.dataset.artFmt;
          el.querySelectorAll("[data-art-fmt]").forEach((b) => b.classList.toggle("active", b === btn));
          const cta = el.querySelector("[data-art-cta]");
          if (cta) cta.textContent = `Download ${node.articleFmt === "text" ? "Plain Text (.txt)" : "Clean Markdown (.md)"}`;
          sfx.click();
        };
      });
      const copyArt = el.querySelector("[data-art-copy]");
      if (copyArt) {
        copyArt.onpointerdown = (e) => e.stopPropagation();
        copyArt.onclick = async (e) => {
          e.stopPropagation();
          if (ws.excerpt) {
            try { await navigator.clipboard.writeText(ws.excerpt); toast("Article excerpt copied!", "ok"); sfx.click(); } catch (_) {}
          }
        };
      }
    }

    // Interactive Webpage SVG & Image Asset Picker wiring
    if (node.type === "choice" && node.panelOpen && node.inputKind === "assets_preview") {
      const assets = state.nodes.get(node.rootSourceId)?.probe?.metadata?.extra?.webpage_assets_preview || { svgs: [], images: [] };
      el.querySelectorAll("[data-copy-svg-idx]").forEach((btn) => {
        btn.onpointerdown = (e) => e.stopPropagation();
        btn.onclick = async (e) => {
          e.stopPropagation();
          const svg = assets.svgs?.[parseInt(btn.dataset.copySvgIdx, 10)];
          if (svg) {
            try { await navigator.clipboard.writeText(svg); toast("SVG vector code copied to clipboard!", "ok"); sfx.complete(); } catch (_) {}
          }
        };
      });
      el.querySelectorAll("[data-spawn-asset-img]").forEach((btn) => {
        btn.onpointerdown = (e) => e.stopPropagation();
        btn.onclick = (e) => {
          e.stopPropagation();
          const u = btn.dataset.spawnAssetImg;
          if (u) {
            const pos = pickChildPosition(node, 0);
            const child = spawnSourceWithPossibilities(u, pos, true);
            if (child) addEdge(node.id, child.id, "image", false, "ASSET");
            sfx.capture();
          }
        };
      });
    }

    // Interactive Visual Image Studio wiring
    if (node.type === "choice" && node.panelOpen && node.inputKind === "image_studio") {
      const qRange = el.querySelector("[data-img-q]");
      const qVal = el.querySelector("[data-img-q-val]");
      const exifBtn = el.querySelector("[data-img-exif]");
      el.querySelectorAll("[data-img-fmt]").forEach((btn) => {
        btn.onpointerdown = (e) => e.stopPropagation();
        btn.onclick = (e) => {
          e.stopPropagation();
          node.imgFmt = btn.dataset.imgFmt;
          el.querySelectorAll("[data-img-fmt]").forEach((b) => b.classList.toggle("active", b === btn));
          sfx.click();
        };
      });
      el.querySelectorAll("[data-img-w]").forEach((btn) => {
        btn.onpointerdown = (e) => e.stopPropagation();
        btn.onclick = (e) => {
          e.stopPropagation();
          node.imgMaxWidth = btn.dataset.imgW ? parseInt(btn.dataset.imgW, 10) : null;
          el.querySelectorAll("[data-img-w]").forEach((b) => b.classList.toggle("active", b === btn));
          sfx.click();
        };
      });
      if (qRange) {
        qRange.onpointerdown = (e) => e.stopPropagation();
        qRange.oninput = () => {
          node.imgQuality = parseInt(qRange.value, 10);
          if (qVal) qVal.textContent = `${node.imgQuality}%`;
        };
      }
      if (exifBtn) {
        exifBtn.onpointerdown = (e) => e.stopPropagation();
        exifBtn.onclick = (e) => {
          e.stopPropagation();
          node.imgStripExif = !node.imgStripExif;
          exifBtn.classList.toggle("active", node.imgStripExif);
          sfx.click();
        };
      }
    }

    // Interactive Playlist Checklist Picker wiring
    const plFilter = el.querySelector("[data-pl-filter]");
    const plList = el.querySelector("[data-pl-list]");
    if (plList) {
      const syncCheckedToRange = () => {
        const nums = Array.from(plList.querySelectorAll("[data-pl-chk]:checked")).map((c) => parseInt(c.dataset.plChk, 10)).sort((a, b) => a - b);
        const val = nums.join(",");
        node.playlistRangeVal = val;
        const rInp = el.querySelector("[data-pl-range]");
        if (rInp) rInp.value = val;
      };
      plList.querySelectorAll("[data-pl-chk]").forEach((chk) => {
        chk.onpointerdown = (e) => e.stopPropagation();
        chk.onchange = () => { syncCheckedToRange(); sfx.click(); };
      });
      plList.querySelectorAll("[data-pl-spawn]").forEach((btn) => {
        btn.onpointerdown = (e) => e.stopPropagation();
        btn.onclick = (e) => {
          e.stopPropagation(); e.preventDefault();
          const pos = pickChildPosition(node, 0);
          const child = spawnSourceWithPossibilities(btn.dataset.plSpawn, pos, true);
          if (child) addEdge(node.id, child.id, node.theme, false, "SINGLE");
          sfx.capture();
        };
      });
      if (plFilter) {
        plFilter.onpointerdown = (e) => e.stopPropagation();
        plFilter.oninput = () => {
          const q = plFilter.value.trim().toLowerCase();
          plList.querySelectorAll(".picker-row").forEach((row) => {
            row.style.display = !q || (row.dataset.plTitle || "").includes(q) ? "" : "none";
          });
        };
      }
      el.querySelectorAll("[data-pl-quick]").forEach((btn) => {
        btn.onpointerdown = (e) => e.stopPropagation();
        btn.onclick = (e) => {
          e.stopPropagation();
          const mode = btn.dataset.plQuick;
          plList.querySelectorAll("[data-pl-chk]").forEach((chk) => {
            const n = parseInt(chk.dataset.plChk, 10);
            chk.checked = mode === "all" ? true : n <= 5;
          });
          syncCheckedToRange();
          sfx.click();
        };
      });
    }
    const textPreviewEl = el.querySelector("[data-fetch-text-preview]");
    if (textPreviewEl) {
      fetch(textPreviewEl.dataset.fetchTextPreview)
        .then((r) => (r.ok ? r.text() : ""))
        .then((txt) => {
          textPreviewEl.textContent = txt ? txt.slice(0, 900) + (txt.length > 900 ? "\n…" : "") : "Preview ready — click Save File.";
        })
        .catch(() => { textPreviewEl.textContent = "Click Save File to view full text."; });
    }
    const plRange = el.querySelector("[data-pl-range]");
    if (plRange) {
      plRange.onpointerdown = (e) => e.stopPropagation();
      plRange.oninput = () => { node.playlistRangeVal = plRange.value; };
      plRange.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); executeChoiceLeaf(node); } };
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
    el.querySelectorAll("[data-watch-source]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        promoteDemoTreeToReal();
        const cap = analyzeProbe(node.probe);
        launchLivePlayerFromParent(node, node.url, cap.liveMode || "video");
      });
    });
    el.querySelector("[data-copy-url]")?.addEventListener("click", async (e) => {
      e.stopPropagation();
      await navigator.clipboard.writeText(e.currentTarget.dataset.copyUrl);
      sfx.click();
      toast("Source URL copied", "ok");
    });
    el.querySelector("[data-reprobe]")?.addEventListener("click", (e) => {
      e.stopPropagation();
      promoteDemoTreeToReal();
      sfx.click();
      probeAndBloomLevel1(node, { bypassCache: true });
    });
    el.querySelector("[data-player-theater]")?.addEventListener("click", (e) => {
      e.stopPropagation();
      sfx.click();
      node.theater = !node.theater;
      el.classList.toggle("theater", node.theater);
      e.currentTarget.textContent = node.theater ? "⤢" : "⤡";
      renderThreads();
    });
    const trackSel = el.querySelector("[data-player-track]");
    if (trackSel) {
      trackSel.onpointerdown = (e) => e.stopPropagation();
      trackSel.onchange = () => {
        const mediaEl = el.querySelector("[data-media-el]");
        const resumeAt = mediaEl && Number.isFinite(mediaEl.currentTime) ? mediaEl.currentTime : 0;
        node.activeStreamId = trackSel.value;
        const chosen = (node.stream?.tracks || []).find((t) => t.stream_id === trackSel.value);
        node.playMode = chosen?.kind || "video";
        node.resumeAt = resumeAt;
        renderNode(node);
      };
    }
    const speedSel = el.querySelector("[data-player-speed]");
    if (speedSel) {
      speedSel.onpointerdown = (e) => e.stopPropagation();
      speedSel.onchange = () => {
        node.playbackRate = parseFloat(speedSel.value) || 1;
        const mediaEl = el.querySelector("[data-media-el]");
        if (mediaEl) mediaEl.playbackRate = node.playbackRate;
      };
    }
    el.querySelector("[data-player-pip]")?.addEventListener("click", async (e) => {
      e.stopPropagation();
      const v = el.querySelector("video[data-media-el]");
      if (v && document.pictureInPictureEnabled) {
        try {
          if (document.pictureInPictureElement === v) await document.exitPictureInPicture();
          else await v.requestPictureInPicture();
        } catch (err) { toast(`PiP unavailable: ${err.message}`, "err"); }
      }
    });
    el.querySelector("[data-copy-stream-url]")?.addEventListener("click", async (e) => {
      e.stopPropagation();
      const absUrl = `${window.location.origin}${e.currentTarget.dataset.copyStreamUrl}`;
      await navigator.clipboard.writeText(absUrl);
      sfx.click();
      toast("Network stream URL copied — paste in VLC (Open Network Stream)", "ok");
    });
    el.querySelectorAll("[data-seek-to]").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const mediaEl = el.querySelector("[data-media-el]");
        if (mediaEl) {
          mediaEl.currentTime = parseFloat(btn.dataset.seekTo) || 0;
          mediaEl.play().catch(() => {});
          sfx.click();
        }
      };
    });
    if (node.type === "player" && !node.loading && !node.error) {
      attachLiveMediaSource(el, node);
    }

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
      noteArea.oninput = () => { node.text = noteArea.value; if (!node.demo) persistWorkspace(); };
    }
  }

  /* ---------- Dynamic Content-Aware Patch Context Menu ---------- */
  const _THEME_COLOR = {
    video: "var(--accent)",
    audio: "var(--violet)",
    subtitles: "var(--info)",
    image: "var(--warn)",
    live: "var(--live)",
  };

  function openPatchMenu(fromNodeId, sx, sy, wx, wy) {
    state.pendingPatch = { fromNodeId, wx, wy };
    const menu = $("patch-menu");
    const fromNode = state.nodes.get(fromNodeId);
    const srcNode = fromNode ? resolveSourceNodeFor(fromNode.id) : null;
    const dynamicRoutes = srcNode ? getLevel1Choices(srcNode) : [];

    let html = `<div class="patch-menu-head mono">${srcNode ? `SPAWN REAL PATH (${dynamicRoutes.length})` : "CANVAS ACTION"}</div>`;
    for (const r of dynamicRoutes) {
      const col = _THEME_COLOR[r.theme] || "var(--accent)";
      html += `<button class="patch-item" data-patch-key="${esc(r.key)}"><span class="patch-item-label"><span style="color:${col};display:inline-flex;">${iconSvg(r.icon, "ico-sm")}</span><span>${esc(r.title)}</span></span><kbd class="mono">${esc((r.edgeLabel || r.key).slice(0, 9))}</kbd></button>`;
    }
    if (dynamicRoutes.length) html += `<div class="patch-sep"></div>`;
    html += `<button class="patch-item" data-patch-action="note"><span class="patch-item-label"><span style="color:var(--warn);display:inline-flex;">${iconSvg("file-text", "ico-sm")}</span><span>Attach Studio Note</span></span><kbd class="mono">NOTE</kbd></button>`;
    menu.innerHTML = html;

    menu.querySelectorAll("[data-patch-key]").forEach((btn) => {
      btn.onclick = () => {
        const pending = state.pendingPatch;
        closePatchMenu();
        if (!pending || !srcNode) return;
        const matched = getLevel1Choices(srcNode).find((c) => c.key === btn.dataset.patchKey);
        if (!matched) return;
        if (matched.delta?._livePlay) {
          promoteDemoTreeToReal();
          launchLivePlayerFromParent(srcNode, srcNode.url, matched.delta._livePlay);
          return;
        }
        const kids = spawnChoiceChildren(srcNode, [matched], true, true);
        if (kids[0]) onChoiceClicked(kids[0]);
      };
    });
    menu.querySelector('[data-patch-action="note"]')?.addEventListener("click", () => {
      const pending = state.pendingPatch;
      closePatchMenu();
      if (pending && fromNode) spawnNoteNode(pending.wx, pending.wy - 50, "", fromNode.id);
    });

    menu.style.left = `${Math.min(window.innerWidth - 280, Math.max(12, sx))}px`;
    menu.style.top = `${Math.min(window.innerHeight - 320, Math.max(64, sy))}px`;
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

  async function probeAndBloomLevel1(node, { allowBrowserFallback = false, bypassCache = false } = {}) {
    // Prune any previous non-result choice branches belonging to this source before re-blooming!
    const existingChildren = state.edges.filter((e) => e.from === node.id).map((e) => e.to);
    for (const cid of existingChildren) pruneChoiceSubtree(cid);

    node.probing = true;
    node.probeError = null;
    if (allowBrowserFallback) node.usedDeepScan = true;
    renderNode(node);
    osc.set(true, allowBrowserFallback ? "DEEP SCANNING PAGE…" : "PROBING REAL CONTENT…");

    try {
      const res = await fetch("/v1/probe", {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          url: node.url,
          include_subtitles: true,
          allow_browser_fallback: allowBrowserFallback,
          bypass_cache: bypassCache,
        }),
      });
      const data = await res.json();
      if (res.ok) {
        node.probe = data;
        node.title = data.title || node.url;
        node.probeError = null;
      } else {
        node.probeError = data.detail?.message || `Probe failed (HTTP ${res.status})`;
      }
    } catch (err) {
      node.probeError = `Network error: ${err.message}`;
    }

    node.probing = false;
    renderNode(node);
    const routes = getLevel1Choices(node);
    spawnChoiceChildren(node, routes, true, true);
    osc.set(
      false,
      node.probeError
        ? "PROBE ISSUE · CHOOSE RECOVERY"
        : `${routes.length} REAL ROUTE${routes.length === 1 ? "" : "S"} DETECTED`,
    );
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
    const branchDir = parentNode?.branchDir || "east";
    const rootSourceId = parentNode?.rootSourceId || (parentNode?.type === "source" ? parentNode.id : null);
    const node = { id, type: "result", jobId: job.job_id, job, branchDir, rootSourceId, x: pos.x, y: pos.y, collapsed: !!saved?.collapsed };
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

  /* ---------- Live Network Stream Player (VLC Mode · Zero Download) ---------- */
  function attachLiveMediaSource(el, node) {
    const mediaEl = el.querySelector("[data-media-el]");
    const activeTrack = (node.stream?.tracks || []).find((t) => t.stream_id === node.activeStreamId) || node.stream?.tracks?.[0];
    if (!mediaEl || !activeTrack) return;

    if (node.hls) {
      try { node.hls.destroy(); } catch (_) {}
      node.hls = null;
    }

    const srcUrl = activeTrack.stream_url;
    const resumeAt = node.resumeAt || 0;
    const rate = node.playbackRate || 1;
    node.resumeAt = 0;

    const applyPlaybackPrefs = () => {
      mediaEl.playbackRate = rate;
      if (resumeAt > 1 && !node.stream?.is_live) {
        try { mediaEl.currentTime = resumeAt; } catch (_) {}
      }
      mediaEl.play().catch(() => {});
    };

    if (activeTrack.protocol === "hls" && window.Hls && window.Hls.isSupported()) {
      const hls = new window.Hls({ enableWorker: true, lowLatencyMode: !!node.stream?.is_live });
      node.hls = hls;
      hls.loadSource(srcUrl);
      hls.attachMedia(mediaEl);
      hls.on(window.Hls.Events.MANIFEST_PARSED, applyPlaybackPrefs);
      hls.on(window.Hls.Events.ERROR, (_, data) => {
        if (data.fatal) toast("Live stream interrupted — try switching track", "err");
      });
    } else {
      mediaEl.src = srcUrl;
      mediaEl.addEventListener("loadedmetadata", applyPlaybackPrefs, { once: true });
    }
  }

  async function launchLivePlayerFromParent(parentNode, url, mode = "video") {
    // Reuse existing live player node if already open for the exact same URL
    const existing = Array.from(state.nodes.values()).find((n) => n.type === "player" && n.url === url && !n.error);
    if (existing) {
      sfx.click();
      glideToWorld(existing.x + 250, existing.y + 150, Math.min(camera.zoom, 0.96));
      return existing;
    }
    const siblings = state.edges.filter((e) => e.from === parentNode.id).length;
    const pos = pickChildPosition(parentNode, siblings);
    const playerDir = parentNode.branchDir || (parentNode.type === "source" ? "north" : "east");
    const playerRootId = parentNode.rootSourceId || (parentNode.type === "source" ? parentNode.id : null);
    const playerNode = {
      id: uid("player"),
      type: "player",
      url,
      title: parentNode.title || url,
      branchDir: playerDir,
      rootSourceId: playerRootId,
      playMode: mode,
      loading: true,
      error: null,
      stream: null,
      activeStreamId: null,
      playbackRate: 1,
      theater: false,
      hls: null,
      x: pos.x,
      y: pos.y,
    };
    state.nodes.set(playerNode.id, playerNode);
    renderNode(playerNode);
    const edge = addEdge(parentNode.id, playerNode.id, "live", true, mode === "audio" ? "LIVE AUDIO" : "LIVE STREAM");
    if (playerRootId) layoutTreeFromRoot(playerRootId);
    gravity.shockAt(playerNode.x + 260, playerNode.y + 160);
    glideToWorld(playerNode.x + 250, playerNode.y + 150, Math.min(camera.zoom, 0.92));
    sfx.capture();
    osc.set(true, "CONNECTING LIVE STREAM…");

    try {
      const res = await fetch("/v1/stream/resolve", {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ url, mode, preferred_quality: "auto" }),
      });
      const data = await res.json();
      if (!res.ok) {
        playerNode.loading = false;
        playerNode.error = data.detail?.message || "Could not open live stream for this link.";
        if (edge) edge.active = false;
        renderNode(playerNode);
        osc.set(false, "STREAM UNAVAILABLE");
        return;
      }
      playerNode.loading = false;
      playerNode.stream = data;
      playerNode.title = data.title;
      playerNode.activeStreamId = data.default_stream_id;
      const chosen = (data.tracks || []).find((t) => t.stream_id === data.default_stream_id);
      if (chosen) playerNode.playMode = chosen.kind;
      renderNode(playerNode);
      sfx.complete();
      osc.set(false, "STREAMING LIVE · ZERO DOWNLOAD");
    } catch (err) {
      playerNode.loading = false;
      playerNode.error = `Network error: ${err.message}`;
      if (edge) edge.active = false;
      renderNode(playerNode);
      osc.set(false, "CANVAS READY");
    }
  }

  /* ---------- Dock Submission ---------- */
  function syncDockModeVisuals() {
    const raw = $("url-input").value.trim();
    $("clear-url-btn").classList.toggle("hidden", !raw);
    const url = extractUrl(raw);
    if (url) { $("platform-tag").textContent = shortHost(url).toUpperCase(); $("platform-tag").classList.remove("hidden"); }
    else $("platform-tag").classList.add("hidden");
  }

  async function fireDockCommand() {
    const raw = $("url-input").value.trim();
    if (!raw) return toast("Paste a media link to spawn its paths", "err");
    const url = extractUrl(raw);
    if (!url) return toast("Include a valid http(s) URL", "err");

    $("url-input").value = "";
    syncDockModeVisuals();
    sfx.capture();
    spawnSourceWithPossibilities(url, null, true);
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

  /* ---------- Pin Saved Job to Canvas ---------- */
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

  /* ---------- Minimal Core Health Indicator ---------- */
  async function loadCoreHealth() {
    try {
      const res = await fetch("/v1/health");
      if (!res.ok) return;
      const data = await res.json();
      const ok = data.status === "ok";
      if ($("status-pill-text")) $("status-pill-text").textContent = ok ? "Core OK" : "Degraded";
      if ($("status-led")) $("status-led").classList.toggle("live", ok);
    } catch (_) {}
  }

  /* ---------- Viewport, Minimap & Event Wiring ---------- */
  function bindCanvasNavigation() {
    const vp = $("viewport");
    applyCamera();

    vp.addEventListener("pointerdown", (e) => {
      closePatchMenu();
      if (!state.spaceHeld && (e.target.closest(".node") || e.target.closest("button") || e.target.closest(".empty-hud-card"))) return;
      if (!state.spaceHeld) {
        state.selectedNodeId = null;
        document.querySelectorAll(".node.selected").forEach((x) => x.classList.remove("selected"));
        drawMinimap();
      }
      vp.classList.add(state.spaceHeld ? "space-pan" : "panning");
      const sx = e.clientX, sy = e.clientY, ox = camera.x, oy = camera.y;
      const onMove = (mv) => { camera.x = ox + (mv.clientX - sx); camera.y = oy + (mv.clientY - sy); applyCamera(); };
      const onUp = () => { vp.classList.remove("panning", "space-pan"); window.removeEventListener("pointermove", onMove); window.removeEventListener("pointerup", onUp); };
      window.addEventListener("pointermove", onMove); window.addEventListener("pointerup", onUp);
    });

    vp.addEventListener("wheel", (e) => {
      if (e.target.closest(".note-textarea")) return;
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
      if (e.target.closest(".node") || e.target.closest("button") || e.target.closest(".thread-pill") || e.target.closest(".empty-hud-card")) return;
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
    $("clear-canvas-btn").onclick = () => clearEntireCanvas();
    $("shortcuts-btn").onclick = () => { sfx.click(); $("shortcuts-modal")?.classList.toggle("hidden"); };
    $("close-shortcuts-modal").onclick = () => $("shortcuts-modal")?.classList.add("hidden");
    $("empty-focus-btn").onclick = () => { sfx.click(); $("url-input")?.focus(); };
    $("empty-demo-btn").onclick = () => { sfx.click(); spawnDefaultShowcase(); };
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
      const typing = document.activeElement?.tagName === "INPUT" || document.activeElement?.tagName === "TEXTAREA";
      if (e.key === "Escape") {
        closePatchMenu();
        $("shortcuts-modal")?.classList.add("hidden");
        $("dock-popover").classList.add("hidden");
        state.batchOpen = false;
        return;
      }
      if (typing) return;

      if (e.code === "Space" && !state.spaceHeld) {
        e.preventDefault();
        state.spaceHeld = true;
        $("viewport")?.classList.add("space-ready");
        return;
      }
      if (e.key === "/" || ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k")) {
        e.preventDefault(); sfx.click(); input.focus();
      } else if (e.key === "?") {
        e.preventDefault(); sfx.click(); $("shortcuts-modal")?.classList.toggle("hidden");
      } else if (e.key.toLowerCase() === "f" && !e.metaKey && !e.ctrlKey) {
        e.preventDefault(); sfx.click(); fitAllNodes();
      } else if (e.key === "0" && !e.metaKey && !e.ctrlKey) {
        e.preventDefault(); sfx.click(); camera.zoom = 1; applyCamera();
      } else if ((e.key === "+" || e.key === "=") && !e.metaKey && !e.ctrlKey) {
        e.preventDefault(); sfx.click(); camera.zoom = Math.min(2, camera.zoom * 1.18); applyCamera();
      } else if (e.key === "-" && !e.metaKey && !e.ctrlKey) {
        e.preventDefault(); sfx.click(); camera.zoom = Math.max(0.3, camera.zoom / 1.18); applyCamera();
      } else if (e.key.toLowerCase() === "t" && !e.metaKey && !e.ctrlKey) {
        e.preventDefault(); tidyGraphLayout();
      } else if (e.key.toLowerCase() === "n" && !e.metaKey && !e.ctrlKey) {
        e.preventDefault(); const c = getViewportCenterWorld(); spawnNoteNode(c.x - 135, c.y - 60, "");
      } else if ((e.key === "Backspace" || e.key === "Delete") && state.selectedNodeId) {
        e.preventDefault();
        const delId = state.selectedNodeId;
        state.selectedNodeId = null;
        removeNode(delId);
      }
    });

    document.addEventListener("keyup", (e) => {
      if (e.code === "Space" && state.spaceHeld) {
        state.spaceHeld = false;
        $("viewport")?.classList.remove("space-ready", "space-pan");
      }
    });

    if ($("clear-sandbox-btn")) $("clear-sandbox-btn").onclick = () => { sfx.click(); clearDemoGraph(); };

    $("dock-batch-btn").onclick = () => {
      sfx.click(); state.batchOpen = !state.batchOpen;
      $("dock-popover").classList.toggle("hidden", !state.batchOpen);
      $("dock-batch-btn").classList.toggle("active", state.batchOpen);
    };
    $("exit-batch-btn").onclick = () => $("dock-batch-btn").click();
    $("batch-input").oninput = () => { const n = $("batch-input").value.split(/\r?\n/).filter((x) => x.trim()).length; $("batch-count").textContent = `${n} link${n === 1 ? "" : "s"}`; };
    $("spawn-batch-btn").onclick = spawnBatchTrees;

    $("paste-btn").onclick = async () => {
      try { const text = await navigator.clipboard.readText(); if (text) { input.value = text.trim(); syncDockModeVisuals(); input.focus(); } }
      catch (_) { input.focus(); toast("Press ⌘V / Ctrl+V to paste"); }
    };
    $("clear-url-btn").onclick = () => { input.value = ""; syncDockModeVisuals(); input.focus(); };
    $("watch-live-btn").onclick = async () => {
      const raw = $("url-input").value.trim();
      const url = extractUrl(raw);
      if (!url) return toast("Paste a video, audio, or live stream URL to watch live", "err");
      $("url-input").value = "";
      syncDockModeVisuals();
      const src = spawnSourceWithPossibilities(url, null, true);
      await launchLivePlayerFromParent(src, url, "video");
    };
    $("download-btn").onclick = fireDockCommand;

    $("tidy-graph-btn").onclick = tidyGraphLayout;
    const syncLayoutBtn = () => {
      if ($("layout-mode-btn")) $("layout-mode-btn").classList.toggle("active", state.layoutMode === "compass");
      if ($("layout-mode-text")) $("layout-mode-text").textContent = state.layoutMode === "compass" ? "Compass" : "Tree";
    };
    syncLayoutBtn();
    if ($("layout-mode-btn")) {
      $("layout-mode-btn").onclick = () => {
        applyLayoutMode(state.layoutMode === "compass" ? "tree" : "compass");
        toast(state.layoutMode === "compass" ? "4-Way Compass layout active" : "Classic Tree layout active", "ok");
      };
    }

    const syncAutosave = () => $("autosave-pill").classList.toggle("active", state.autoSave);
    syncAutosave();
    $("autosave-pill").onclick = () => { state.autoSave = !state.autoSave; localStorage.setItem("neon_autosave", String(state.autoSave)); syncAutosave(); sfx.click(); };

    const syncSfx = () => { $("sfx-btn").classList.toggle("active", state.sfxEnabled); $("sfx-text").textContent = state.sfxEnabled ? "SFX" : "Muted"; };
    syncSfx();
    $("sfx-btn").onclick = () => { state.sfxEnabled = !state.sfxEnabled; localStorage.setItem("neon_sfx", String(state.sfxEnabled)); syncSfx(); if (state.sfxEnabled) sfx.click(); };

    const applyTheme = (t) => { document.documentElement.setAttribute("data-theme", t); localStorage.setItem("neon_sync_theme", t); $("theme-text").textContent = t === "dark" ? "Light" : "Dark"; gravity.wake(); };
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
    syncWorkspaceMeta();
  }

  /* ---------- Boot ---------- */
  (async () => {
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      window.lucide.createIcons();
    }
    bindAllEvents();
    await loadCoreHealth();
    await hydrateFromBackend();
  })();
})();
