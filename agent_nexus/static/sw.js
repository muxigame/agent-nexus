/* agent-nexus inbox service worker (design §8.12).
 *
 * Caches only the static shell so the PWA opens offline; mail never: every
 * /api/ request (and anything not in SHELL) goes straight to the network and
 * its response is never stored. No Web Push in 0.2.
 */
"use strict";
const CACHE = "anx-inbox-shell-v1";
const SHELL = ["/inbox/", "/inbox/inbox.js", "/inbox/manifest.webmanifest", "/inbox/icon.png"];

function cacheable(url) {
  return url.origin === self.location.origin && !url.pathname.startsWith("/api/") && SHELL.includes(url.pathname);
}

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL.filter((path) => path !== "/inbox/"))).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))));
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || !cacheable(url)) return; // network only, never cached
  // Network first: a fresh shell when online, the cached one offline.
  event.respondWith(fetch(request).then((response) => {
    if (response.ok && response.type === "basic" && !response.redirected) {
      const copy = response.clone();
      caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
    }
    return response;
  }).catch(() => caches.match(request).then((cached) => cached || Response.error())));
});
