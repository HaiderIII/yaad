// Yaad Service Worker - Enhanced PWA Support
const CACHE_VERSION = 'v3';
const STATIC_CACHE = `yaad-static-${CACHE_VERSION}`;
const DYNAMIC_CACHE = `yaad-dynamic-${CACHE_VERSION}`;

// Static assets to pre-cache during install
const STATIC_ASSETS = [
  '/offline',
  '/static/css/output.css',
  '/static/js/app.js',
  '/static/manifest.json',
  '/static/favicon.svg',
  '/static/img/icon-192.svg',
  '/static/img/icon-192.png',
  '/static/img/icon-512.png'
];

// Cache size limits
const DYNAMIC_CACHE_LIMIT = 50;

// Trim cache to limit
async function trimCache(cacheName, maxItems) {
  const cache = await caches.open(cacheName);
  const keys = await cache.keys();
  if (keys.length > maxItems) {
    const excess = keys.slice(0, keys.length - maxItems);
    await Promise.all(excess.map(key => cache.delete(key)));
  }
}

// Install event - pre-cache static assets
self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((cache) => cache.addAll(STATIC_ASSETS))
  );
});

// Activate event - clean up old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => {
            return name.startsWith('yaad-') &&
                   name !== STATIC_CACHE &&
                   name !== DYNAMIC_CACHE;
          })
          .map((name) => caches.delete(name))
      );
    }).then(() => self.clients.claim())
  );
});

// Fetch event - implement caching strategies
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Skip non-GET requests
  if (request.method !== 'GET') return;

  // Skip ALL cross-origin requests - let the browser handle them directly
  // This avoids CSP connect-src issues with external images (TMDB, YouTube, etc.)
  if (url.origin !== location.origin) return;

  // API requests - network only, no caching
  if (url.pathname.startsWith('/api/')) {
    return;
  }

  // Static assets - cache first, network fallback
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) return cached;
        return fetch(request).then((response) => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(STATIC_CACHE).then((cache) => cache.put(request, clone));
          }
          return response;
        });
      })
    );
    return;
  }

  // HTML pages - network first, cache fallback
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(DYNAMIC_CACHE).then((cache) => {
            cache.put(request, clone);
            trimCache(DYNAMIC_CACHE, DYNAMIC_CACHE_LIMIT);
          });
        }
        return response;
      })
      .catch(() => {
        return caches.match(request).then((cached) => {
          if (cached) return cached;
          return caches.match('/offline');
        });
      })
  );
});
