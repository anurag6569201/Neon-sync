const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const code = fs.readFileSync(path.join(__dirname, "../src/neon_sync/api/static/client.js"), "utf8");
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });

function harness(fetch) {
  const timers = new Map();
  const sources = [];
  let nextId = 0;
  class EventSource {
    constructor() { this.listeners = {}; sources.push(this); }
    addEventListener(event, listener) { this.listeners[event] = listener; }
    close() { this.closed = true; }
  }
  const window = { localStorage: { getItem() { throw Error("blocked"); }, setItem() { throw Error("blocked"); } } };
  vm.runInNewContext(code, {
    window, fetch, EventSource, AbortController, TypeError, Error, TextDecoder, URL, URLSearchParams,
    setTimeout(callback, delay) { const id = ++nextId; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  return {
    client: window.NeonClient, timers, sources,
    async tick() {
      const [id, timer] = [...timers.entries()].sort((a, b) => a[1].delay - b[1].delay)[0];
      timers.delete(id);
      await timer.callback();
    },
  };
}

test("blocked browser storage preserves preferences for the session", () => {
  const { client } = harness();
  client.preferences.setItem("theme", "light");
  assert.equal(client.preferences.getItem("theme"), "light");
  assert.equal(client.preferences.getItem("missing"), null);
});

test("playlist checkbox selections stay compact without dropping gaps", () => {
  const { client } = harness();
  assert.equal(client.compressIndices([1, 2, 3, 6, 8, 9, 9]), "1-3,6,8-9");
  assert.equal(client.compressIndices(Array.from({ length: 100 }, (_, i) => i + 1)), "1-100");
});

test("playlist selections retain late positions and bounded inclusive ranges", () => {
  const { client } = harness();
  assert.equal(client.compressIndices(client.parseIndices("2-5,151:165:2,166")), "2-5,151,153,155,157,159,161,163,165-166");
  assert.equal(client.parseIndices("151-166").length, 16);
  assert.throws(() => client.parseIndices("9-2"), /end must follow/);
  assert.throws(() => client.parseIndices("1-100000000"), /too large/);
});

test("validation errors name the field and explain how to correct it", async () => {
  const { client } = harness(async () => response({ detail: [{ loc: ["body", "clip_end_seconds"], msg: "Must be greater than start" }] }, 422));
  await assert.rejects(client.request("/v1/jobs"), /clip_end_seconds: Must be greater than start/);
});

test("gateway HTML produces a readable error", async () => {
  const { client } = harness(async () => ({ ok: false, status: 502, json: async () => { throw Error("HTML"); } }));
  await assert.rejects(client.request("/v1/jobs"), /unreadable response/);
});

test("polling recovers a lost SSE connection and completes exactly once with the artifact", async () => {
  let calls = 0;
  const h = harness(async () => response(++calls === 1
    ? { job_id: "a", status: "downloading" }
    : { job_id: "a", status: "completed", result: { download: { artifact_url: "/file.mp4" } } }));
  const completed = [];
  h.client.watchJob("a", { onUpdate() {}, onComplete(job) { completed.push(job); } });
  await h.tick();
  h.sources[0].onerror();
  await h.tick();
  assert.equal(completed.length, 1);
  assert.equal(completed[0].result.download.artifact_url, "/file.mp4");
  h.sources[0].listeners.progress({ data: '{"status":"completed"}' });
  assert.equal(completed.length, 1);
  assert.equal(h.timers.size, 0);
});

test("a failed final-record request retries without losing completion", async () => {
  let calls = 0;
  const h = harness(async () => {
    if (++calls === 1) throw new TypeError("offline");
    return response({ status: "completed", result: { download: { filename: "done.mp4" } } });
  });
  let done = false;
  h.client.watchJob("a", { onUpdate() {}, onComplete() { done = true; } });
  h.sources[0].listeners.progress({ data: '{"status":"completed"}' });
  await h.tick();
  assert.equal(done, false);
  await h.tick();
  assert.equal(done, true);
  assert.equal(h.timers.size, 0);
});

test("closing a watcher prevents updates after a late in-flight response", async () => {
  let resolve;
  const h = harness(() => new Promise((r) => { resolve = r; }));
  let updates = 0;
  const watcher = h.client.watchJob("a", { onUpdate() { updates++; }, onComplete() { updates++; } });
  const pending = h.tick();
  watcher.close();
  resolve(response({ status: "completed" }));
  await pending;
  assert.equal(updates, 0);
  assert.equal(h.timers.size, 0);
});


test("size metadata streams across split lines and UTF-8 chunks before completion", async () => {
  const encoder = new TextEncoder();
  const bytes = encoder.encode(JSON.stringify({ url: "one", probe: { title: "Café", size_bytes: 1024 } }) + "\n");
  const split = bytes.indexOf(195) + 1;
  let streamController;
  const stream = new ReadableStream({ start(controller) { streamController = controller; } });
  const { client } = harness(async () => ({ ok: true, body: stream }));
  const received = [];
  const pending = client.probeSizes(["one", "two"], (result) => received.push(result));
  streamController.enqueue(bytes.slice(0, split));
  streamController.enqueue(bytes.slice(split));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(received[0].probe.title, "Café");
  streamController.enqueue(encoder.encode(JSON.stringify({ url: "two", error: { message: "Unavailable" } })));
  streamController.close();
  await pending;
  assert.equal(received.length, 2);
  assert.equal(received[1].error.message, "Unavailable");
});

test("size metadata respects rate-limit retry timing", async () => {
  const { client } = harness(async () => ({ ...response({ detail: { message: "Slow down" } }, 429), headers: { get: () => "12" } }));
  await assert.rejects(client.probeSizes(["one"], () => {}), (error) => error.status === 429 && error.retryAfter === 12);
});

test("aborting size metadata suppresses late streamed results", async () => {
  const controller = new AbortController();
  const { client } = harness(async () => ({ ok: true, body: new ReadableStream({ start(stream) {
    controller.abort();
    stream.enqueue(new TextEncoder().encode('{"url":"one"}\n'));
    stream.close();
  } }) }));
  const received = [];
  await client.probeSizes(["one"], (result) => received.push(result), { signal: controller.signal });
  assert.equal(received.length, 0);
});

test("incoming URL capture extracts http(s) links from url/text params and rejects unsafe schemes", () => {
  const { client } = harness();
  assert.equal(client.extractIncomingUrl("?url=https%3A%2F%2Fyoutu.be%2Fabc123%3Ft%3D12"), "https://youtu.be/abc123?t=12");
  assert.equal(client.extractIncomingUrl("?text=Check%20this%20out%20https%3A%2F%2Fexample.com%2Fclip.mp4)."), "https://example.com/clip.mp4");
  assert.equal(client.extractIncomingUrl("?url=javascript%3Aalert(1)"), null);
  assert.equal(client.extractIncomingUrl("?source=pwa"), null);
  assert.equal(client.extractIncomingUrl(""), null);
});

test("bookmarklet generator targets the active origin cleanly", () => {
  const { client } = harness();
  const href = client.buildBookmarkletHref("https://neonsync.app/");
  assert.ok(href.startsWith("javascript:"));
  assert.ok(href.includes("https://neonsync.app/?url="));
  assert.ok(href.includes("encodeURIComponent(location.href)"));
});
