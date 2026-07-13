const CACHE_PREFIX = "furniture-ar-shell-";
const CACHE_NAME = `${CACHE_PREFIX}__ASSET_VERSION__`;
const OFFLINE_URL = "/static/offline.html";
const APP_SHELL = [
  "/",
  "/static/styles.css",
  "/static/app.js",
  "/manifest.webmanifest",
  OFFLINE_URL,
  "/static/icons/icon.svg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-maskable-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(
      APP_SHELL.map((url) => new Request(url, { cache: "reload" })),
    )),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys
        .filter((key) => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
        .map((key) => caches.delete(key)),
    )),
  );
  self.clients.claim();
});

self.addEventListener("message", (event) => {
  if (event.data === "SKIP_WAITING") self.skipWaiting();
});

async function cacheFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request, { ignoreSearch: true });
  if (cached) return cached;

  const response = await fetch(request);
  if (response.ok && response.type === "basic") {
    await cache.put(request, response.clone());
  }
  return response;
}

async function networkFirstNavigation(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request, { cache: "no-store" });
    if (response.ok && response.type === "basic") {
      await cache.put(request, response.clone());
    }
    return response;
  } catch (_error) {
    return (
      await cache.match(request, { ignoreSearch: true })
      || await cache.match(OFFLINE_URL)
      || Response.error()
    );
  }
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Model and diagnostics routes must always reflect the server. Never let a
  // cached GLB, OBJ, or USDZ replace a conversion-specific response or AR file.
  if (
    url.pathname.startsWith("/generated/")
    || url.pathname.startsWith("/models/")
    || url.pathname.startsWith("/debug/")
    || url.pathname === "/service-worker.js"
  ) {
    event.respondWith(fetch(request, { cache: "no-store" }));
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(networkFirstNavigation(request));
    return;
  }

  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(cacheFirst(request));
  }
});
