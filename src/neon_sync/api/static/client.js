/* Shared browser transport: actionable errors and progress that survives SSE loss. */
(() => {
  "use strict";

  const terminal = (status) => ["completed", "failed", "cancelled"].includes(status);
  const memory = new Map();
  const preferences = {
    getItem(key) {
      try { return window.localStorage.getItem(key) ?? memory.get(key) ?? null; }
      catch (_) { return memory.get(key) ?? null; }
    },
    setItem(key, value) {
      memory.set(key, String(value));
      try { window.localStorage.setItem(key, String(value)); } catch (_) {}
    },
  };

  async function request(url, init = {}, timeoutMs = 45000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, { ...init, signal: controller.signal });
      let data;
      try { data = await response.json(); }
      catch (_) {
        throw new Error("The service returned an unreadable response. Try again shortly.");
      }
      if (!response.ok) {
        const detail = data?.detail;
        const message = Array.isArray(detail)
          ? detail.map((item) => `${(item.loc || []).filter((part) => part !== "body").join(" · ")}: ${item.msg}`).join("; ")
          : detail?.message || (typeof detail === "string" ? detail : null);
        const error = new Error(message || `Request failed (HTTP ${response.status}). Try again shortly.`);
        error.status = response.status;
        error.code = detail?.code;
        error.retryable = detail?.retryable;
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") {
        throw new Error("The request timed out. Check your connection and try again.");
      }
      if (error instanceof TypeError) {
        throw new Error("Cannot reach Neon Sync. Check your connection; queued jobs will keep running.");
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  async function probeSizes(urls, onResult, { signal, headers = {} } = {}) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
    const timer = setTimeout(abort, 120000);
    let reader;
    try {
      const response = await fetch("/v1/probe/sizes", {
        method: "POST", headers: { ...headers, "Content-Type": "application/json" },
        body: JSON.stringify({ urls }), signal: controller.signal,
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        const error = new Error(data.detail?.message || "Size metadata could not be loaded. Try again.");
        error.status = response.status;
        error.retryAfter = Math.max(1, Number(response.headers.get("Retry-After")) || 60);
        throw error;
      }
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let pending = "";
      while (true) {
        const { value, done } = await reader.read();
        pending += decoder.decode(value, { stream: !done });
        const lines = pending.split("\n");
        pending = lines.pop();
        for (const line of lines) if (line.trim() && !controller.signal.aborted) onResult(JSON.parse(line));
        if (done) break;
      }
      if (pending.trim() && !controller.signal.aborted) onResult(JSON.parse(pending));
    } finally {
      await reader?.cancel().catch(() => {});
      reader?.releaseLock();
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }

  function watchJob(jobId, { onUpdate, onComplete, onError = () => {} }) {
    let closed = false;
    let source = null;
    let timer = null;
    let polling = false;
    let delay = 1500;
    const url = `/v1/jobs/${encodeURIComponent(jobId)}`;

    function close() {
      closed = true;
      clearTimeout(timer);
      source?.close();
    }

    function schedule(ms) {
      clearTimeout(timer);
      if (!closed) timer = setTimeout(poll, ms);
    }

    async function poll() {
      if (closed || polling) return;
      polling = true;
      try {
        const job = await request(url, {}, 12000);
        if (closed) return;
        delay = 1500;
        onUpdate(job);
        if (terminal(job.status)) {
          close();
          onComplete(job);
          return;
        }
      } catch (error) {
        if (closed) return;
        if (error.status === 404) {
          close();
          onError(error);
          return;
        }
        delay = Math.min(delay * 2, 30000);
      } finally {
        polling = false;
        // Also poll while SSE is healthy: reverse proxies can silently stall it.
        schedule(source ? 10000 : delay);
      }
    }

    try {
      source = new EventSource(`${url}/events`);
      source.addEventListener("progress", (event) => {
        if (closed) return;
        try {
          const progress = JSON.parse(event.data);
          if (terminal(progress.status)) {
            // Fetch the full record (including the artifact) before stopping.
            schedule(0);
          } else {
            onUpdate({ job_id: jobId, status: progress.status, progress });
          }
        } catch (_) { schedule(0); }
      });
      source.onerror = () => {
        source?.close();
        source = null;
        schedule(0);
      };
    } catch (_) { source = null; }
    schedule(0);
    return { close };
  }

  function compressIndices(indices) {
    const sorted = [...new Set(indices)].sort((a, b) => a - b);
    const ranges = [];
    for (let i = 0; i < sorted.length; i++) {
      const start = sorted[i];
      while (i + 1 < sorted.length && sorted[i + 1] === sorted[i] + 1) i++;
      ranges.push(start === sorted[i] ? String(start) : `${start}-${sorted[i]}`);
    }
    return ranges.join(",");
  }

  function parseIndices(selection, limit = 10000) {
    if (!selection.trim()) return [];
    const indices = new Set();
    for (const part of selection.split(",")) {
      const match = /^([1-9]\d*)(?:[-:]([1-9]\d*)(?::([1-9]\d*))?)?$/.exec(part.trim());
      if (!match) throw new Error("Use positions such as 1-5, 51-100, or 1,3,7.");
      const start = Number(match[1]), end = Number(match[2] || match[1]), step = Number(match[3] || 1);
      if (![start, end, step].every(Number.isSafeInteger) || end < start) throw new Error("Check the selected range: the end must follow the start.");
      if (Math.floor((end - start) / step) + 1 > limit) throw new Error("This selection is too large. Choose a smaller range.");
      for (let index = start; index <= end; index += step) {
        indices.add(index);
        if (indices.size > limit) throw new Error("This selection is too large. Choose a smaller range.");
      }
    }
    return [...indices];
  }

  const INCOMING_URL_RE = /https?:\/\/[^\s<>"']+/i;

  function extractIncomingUrl(search) {
    if (!search) return null;
    let params;
    try { params = new URLSearchParams(search); }
    catch (_) { return null; }
    for (const key of ["url", "text", "link", "q", "title"]) {
      const value = params.get(key);
      if (!value) continue;
      const match = INCOMING_URL_RE.exec(value.trim());
      if (!match) continue;
      const cleaned = match[0].replace(/[).,;!?'"}\]]+$/, "");
      try {
        const parsed = new URL(cleaned);
        if (parsed.protocol === "http:" || parsed.protocol === "https:") return cleaned;
      } catch (_) {}
    }
    return null;
  }

  function buildBookmarkletHref(origin) {
    const base = String(origin || "").replace(/\/+$/, "");
    return `javascript:(()=>{window.open(${JSON.stringify(`${base}/?url=`)}+encodeURIComponent(location.href),"_blank","noopener");})();`;
  }

  window.NeonClient = {
    request,
    probeSizes,
    watchJob,
    preferences,
    terminal,
    compressIndices,
    parseIndices,
    extractIncomingUrl,
    buildBookmarkletHref,
  };
})();
