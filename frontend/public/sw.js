const CACHE_NAME = 'talentgram-pwa-v4';
const OFFLINE_URL = '/offline';

// Static assets to cache immediately during installation
const STATIC_ASSETS = [
  OFFLINE_URL,
  '/site.webmanifest',
  '/favicon.ico',
  '/apple-touch-icon.png',
  '/icon-72.png',
  '/icon-96.png',
  '/icon-128.png',
  '/icon-144.png',
  '/icon-152.png',
  '/icon-180.png',
  '/icon-192.png',
  '/icon-192-maskable.png',
  '/icon-256.png',
  '/icon-384.png',
  '/icon-512.png',
  '/icon-512-maskable.png',
];

// Install Event: Cache app shell / static assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      console.log('[Service Worker] Pre-caching offline fallback and static assets');
      return cache.addAll(STATIC_ASSETS);
    }).then(() => self.skipWaiting())
  );
});

// Activate Event: Clean up outdated caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cacheName) => {
          if (cacheName !== CACHE_NAME) {
            console.log('[Service Worker] Deleting old cache:', cacheName);
            return caches.delete(cacheName);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

// Helper to determine if a request should bypass cache entirely
function shouldBypassCache(request) {
  // 1. Only cache GET requests
  if (request.method !== 'GET') {
    return true;
  }

  const url = new URL(request.url);

  // 2. Never cache backend APIs (/api/) or auth endpoints
  if (url.pathname.includes('/api/') || url.pathname.includes('/auth/')) {
    return true;
  }

  // 3. Never cache uploads, downloads, or media hosting providers (Cloudinary, R2, CF Stream)
  const bypassHosts = [
    'res.cloudinary.com',
    'r2.cloudflarestorage.com',
    'cloudflarestream.com',
    'api.cloudinary.com',
    'fonts.googleapis.com',
    'fonts.gstatic.com'
  ];
  if (bypassHosts.some(host => url.hostname.includes(host))) {
    return true;
  }

  // 4. Never cache authenticated responses or JWT endpoints
  // Note: headers are inspected; if Authorization header is set, we bypass.
  if (request.headers.has('Authorization') || request.headers.has('authorization')) {
    return true;
  }

  // 5. Bypass console, admin control panel pages and sensitive subdomains/routes
  if (
    url.pathname.startsWith('/admin') ||
    url.pathname.startsWith('/portal') ||
    url.pathname.startsWith('/review') ||
    url.pathname.startsWith('/submit') ||
    url.pathname.startsWith('/links')
  ) {
    return true;
  }

  return false;
}

// Fetch Event: Cache strategy & offline fallback
self.addEventListener('fetch', (event) => {
  const request = event.request;

  if (shouldBypassCache(request)) {
    // Network only (bypass cache)
    return;
  }

  // Navigation requests: Network first with offline fallback
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .catch(() => {
          console.log('[Service Worker] Navigation failed, serving offline page');
          return caches.match(OFFLINE_URL);
        })
    );
    return;
  }

  // Static assets (JS, CSS, images, webmanifest, fonts): Stale-While-Revalidate
  event.respondWith(
    caches.match(request).then((cachedResponse) => {
      const fetchPromise = fetch(request).then((networkResponse) => {
        // Cache valid successful responses of self-origin static assets
        if (
          networkResponse &&
          networkResponse.status === 200 &&
          networkResponse.type === 'basic' &&
          (request.url.includes('_next/static') || STATIC_ASSETS.some(asset => request.url.endsWith(asset)))
        ) {
          const responseToCache = networkResponse.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(request, responseToCache);
          });
        }
        return networkResponse;
      }).catch((err) => {
        console.warn('[Service Worker] Fetch failed for static asset:', request.url, err);
        throw err;
      });

      return cachedResponse || fetchPromise;
    })
  );
});

// ==========================================
// PUSH NOTIFICATION & SYNC BOILERPLATE (Readiness)
// ==========================================

// Push Notification Event Listener
self.addEventListener('push', (event) => {
  console.log('[Service Worker] Push Received.');
  let data = { title: 'Talentgram', body: 'New casting update available!' };
  
  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      data = { title: 'Talentgram', body: event.data.text() };
    }
  }

  const options = {
    body: data.body,
    icon: '/icon-192.png',
    badge: '/icon-72.png',
    vibrate: [100, 50, 100],
    data: data.data || {},
    actions: data.actions || []
  };

  event.waitUntil(
    self.registration.showNotification(data.title, options)
  );
});

// Notification Click Event Listener
self.addEventListener('notificationclick', (event) => {
  console.log('[Service Worker] Notification click Received.');
  event.notification.close();

  // Handle click action (e.g., open specific page)
  event.waitUntil(
    clients.matchAll({ type: 'window' }).then((clientList) => {
      for (const client of clientList) {
        if (client.url === '/' && 'focus' in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow('/');
      }
    })
  );
});

// Background Sync (Readiness)
self.addEventListener('sync', (event) => {
  console.log('[Service Worker] Background Sync Triggered:', event.tag);
  // Future sync handlers go here
});

// Periodic Sync (Readiness)
self.addEventListener('periodicsync', (event) => {
  console.log('[Service Worker] Periodic Sync Triggered:', event.tag);
  // Future periodic sync handlers go here
});
