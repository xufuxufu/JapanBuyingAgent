const CACHE_NAME = "jba-field-shell-v5";
const IMAGE_CACHE_NAME = "jba-product-images-v1";
const SHELL = [
  "/field-purchase",
  "/static/app.css",
  "/static/camera_adapter.js",
  "/static/vendor/zxing/zxing-browser-0.2.0.min.js",
  "/static/field_purchase.js",
  "/static/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL))
      .catch(() => undefined),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => ![CACHE_NAME, IMAGE_CACHE_NAME].includes(key)).map((key) => caches.delete(key)),
      )),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && (url.pathname === "/field-purchase" || url.pathname.startsWith("/static/"))) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then((cached) => cached || caches.match("/field-purchase"))),
  );
});
