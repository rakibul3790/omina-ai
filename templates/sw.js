// ═══════════════════════════════════════════════════
//  OMINA AI — Service Worker v1.0
//  Handles: offline caching, background sync, push notifications
// ═══════════════════════════════════════════════════

const CACHE_NAME     = 'omina-v1';
const STATIC_CACHE   = 'omina-static-v1';
const DYNAMIC_CACHE  = 'omina-dynamic-v1';

// Static assets to cache on install
const STATIC_ASSETS = [
  '/',
  '/login',
  'https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700;800;900&display=swap',
  'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css',
  'https://cdn.tailwindcss.com',
];

// ── Install ──────────────────────────────────────────────────
self.addEventListener('install', event => {
  console.log('[SW] Installing Omina AI Service Worker...');
  event.waitUntil(
    caches.open(STATIC_CACHE).then(cache => {
      return cache.addAll(STATIC_ASSETS).catch(err => {
        console.warn('[SW] Some static assets failed to cache:', err);
      });
    }).then(() => self.skipWaiting())
  );
});

// ── Activate ─────────────────────────────────────────────────
self.addEventListener('activate', event => {
  console.log('[SW] Activating...');
  event.waitUntil(
    caches.keys().then(keys => {
      return Promise.all(
        keys
          .filter(k => k !== STATIC_CACHE && k !== DYNAMIC_CACHE)
          .map(k => {
            console.log('[SW] Deleting old cache:', k);
            return caches.delete(k);
          })
      );
    }).then(() => self.clients.claim())
  );
});

// ── Fetch Strategy ───────────────────────────────────────────
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // Skip: non-GET, chrome-extension, API calls, Firebase
  if (request.method !== 'GET') return;
  if (url.protocol === 'chrome-extension:') return;
  if (url.pathname.startsWith('/api/') || url.pathname === '/chat') return;
  if (url.hostname.includes('firebase') || url.hostname.includes('googleapis')) return;
  if (url.hostname.includes('groq')) return;

  // Strategy: Network first → Cache fallback → Offline page
  event.respondWith(
    fetch(request)
      .then(response => {
        // Cache successful responses
        if (response && response.status === 200 && response.type !== 'opaque') {
          const clone = response.clone();
          caches.open(DYNAMIC_CACHE).then(cache => {
            cache.put(request, clone);
          });
        }
        return response;
      })
      .catch(() => {
        // Network failed → try cache
        return caches.match(request).then(cached => {
          if (cached) return cached;
          // For navigation requests, return cached main page
          if (request.destination === 'document') {
            return caches.match('/');
          }
          return new Response('Offline', { status: 503, statusText: 'Service Unavailable' });
        });
      })
  );
});

// ── Push Notifications ───────────────────────────────────────
self.addEventListener('push', event => {
  if (!event.data) return;
  let data;
  try { data = event.data.json(); }
  catch { data = { title: 'Omina AI', body: event.data.text() }; }

  const options = {
    body:    data.body || 'You have a new notification',
    icon:    'https://ui-avatars.com/api/?name=O&size=192&background=7c3aed&color=fff&rounded=true&bold=true',
    badge:   'https://ui-avatars.com/api/?name=O&size=72&background=7c3aed&color=fff&rounded=true&bold=true',
    tag:     data.tag || 'omina-notif',
    renotify: true,
    data:    { url: data.url || '/' },
    actions: [
      { action: 'open',    title: 'Open Omina' },
      { action: 'dismiss', title: 'Dismiss' },
    ],
    vibrate: [100, 50, 100],
  };

  event.waitUntil(
    self.registration.showNotification(data.title || 'Omina AI', options)
  );
});

// ── Notification Click ───────────────────────────────────────
self.addEventListener('notificationclick', event => {
  event.notification.close();
  if (event.action === 'dismiss') return;

  const url = event.notification.data?.url || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      for (const client of clientList) {
        if (client.url === url && 'focus' in client) return client.focus();
      }
      return clients.openWindow(url);
    })
  );
});

// ── Background Sync ──────────────────────────────────────────
self.addEventListener('sync', event => {
  if (event.tag === 'omina-sync') {
    event.waitUntil(
      // Retry any queued messages when back online
      self.clients.matchAll().then(clients => {
        clients.forEach(client => client.postMessage({ type: 'SYNC_READY' }));
      })
    );
  }
});

// ── Message from app ─────────────────────────────────────────
self.addEventListener('message', event => {
  if (event.data?.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
  if (event.data?.type === 'CACHE_URLS') {
    const urls = event.data.urls || [];
    caches.open(DYNAMIC_CACHE).then(cache => cache.addAll(urls));
  }
});
