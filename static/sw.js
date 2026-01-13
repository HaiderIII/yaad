// Yaad Service Worker - Enhanced PWA Support
const CACHE_VERSION = 'v2';
const STATIC_CACHE = `yaad-static-${CACHE_VERSION}`;
const DYNAMIC_CACHE = `yaad-dynamic-${CACHE_VERSION}`;
const IMAGE_CACHE = `yaad-images-${CACHE_VERSION}`;

// Static assets to pre-cache during install
const STATIC_ASSETS = [
  '/',
  '/login',
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
const IMAGE_CACHE_LIMIT = 100;

// Trim cache to limit
async function trimCache(cacheName, maxItems) {
  const cache = await caches.open(cacheName);
  const keys = await cache.keys();
  if (keys.length > maxItems) {
    await cache.delete(keys[0]);
    return trimCache(cacheName, maxItems);
  }
}

// Install event - pre-cache static assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
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
                   name !== DYNAMIC_CACHE &&
                   name !== IMAGE_CACHE;
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

  // Skip cross-origin requests
  if (url.origin !== location.origin) {
    // For external images (covers, avatars), try network first, cache fallback
    if (request.destination === 'image') {
      event.respondWith(
        fetch(request)
          .then((response) => {
            if (response.ok) {
              const clone = response.clone();
              caches.open(IMAGE_CACHE).then((cache) => {
                cache.put(request, clone);
                trimCache(IMAGE_CACHE, IMAGE_CACHE_LIMIT);
              });
            }
            return response;
          })
          .catch(() => caches.match(request))
      );
    }
    return;
  }

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

  // HTML pages - network first, cache fallback (stale-while-revalidate)
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
          // Return offline page if available
          return caches.match('/offline');
        });
      })
  );
});

// Background sync for failed requests (future enhancement)
self.addEventListener('sync', (event) => {
  if (event.tag === 'sync-media') {
    // Handle background sync for media updates
    console.log('Background sync triggered');
  }
});

// Push notifications (future enhancement)
self.addEventListener('push', (event) => {
  if (event.data) {
    const data = event.data.json();
    const options = {
      body: data.body,
      icon: '/static/img/icon-192.png',
      badge: '/static/img/icon-192.png',
      vibrate: [100, 50, 100],
      data: { url: data.url }
    };
    event.waitUntil(
      self.registration.showNotification(data.title, options)
    );
  }
});

// Handle notification click
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  if (event.notification.data && event.notification.data.url) {
    event.waitUntil(
      clients.openWindow(event.notification.data.url)
    );
  }
});
