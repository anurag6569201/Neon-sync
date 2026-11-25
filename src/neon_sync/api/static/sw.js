/* Neon Sync PWA Service Worker — lightweight app-shell cache, never intercepts API or media streams. */
const CACHE_NAME = "neon-sync-shell-v1";
const SHELL_ASSETS = [
  "/",
  "/manifest.webmanifest",
  "/static/app.css",
  "/static/client.js",
  "/static/sizes.js",
  "/static/app.js",
  "/static/icon.svg",
  "/static/icon-192.png",
  "/static/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .catch(() => {})
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // Never intercept API calls, downloads, SSE streams, live HLS media, or capture redirects.
  if (
    url.pathname.startsWith("/v1/") ||
    url.pathname === "/share" ||
    url.pathname.startsWith("/http:") ||
    url.pathname.startsWith("/https:")
  ) {
    return;
  }

  // Network-first for the app shell so updates apply immediately while keeping offline resilience.
  if (request.mode === "navigate" || url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok && response.type === "basic" && (url.pathname === "/" || url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest")) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)).catch(() => {});
          }
          return response;
        })
        .catch(() => caches.match(request).then((hit) => hit || (request.mode === "navigate" ? caches.match("/") : Response.error()))),
    );
  }
});
