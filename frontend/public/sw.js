// ConfessIt floral service worker: aggressive, versioned precache for the
// `public/floral_svgs` artwork so branches survive even a hard refresh.
// Cache Storage is site data, NOT the HTTP cache a hard refresh bypasses,
// so a cache-first fetch handler keeps serving these bytes after Ctrl+F5
// until the user clears site data. The filenames are stable and their
// content never changes without a deploy, so there is deliberately no quota
// or eviction: bump FLORAL_CACHE below and the old cache is dropped on
// activate. Non-floral requests are never intercepted.
const FLORAL_CACHE = 'confessit-floral-v1';
const FLORAL_ASSETS = [
  '/floral_svgs/1706715_no_text_no_attribution.svg',
  '/floral_svgs/1706715_top_no_text_no_attribution.svg',
  '/floral_svgs/1706715_bottom_no_text_no_attribution.svg',
  '/floral_svgs/1748704_cherry_blossom_no_attribution.svg',
  '/floral_svgs/4016406_cherry_blossom_no_text_no_attribution.svg',
  '/floral_svgs/4126474_cherry_blossom_no_attribution.svg',
  '/floral_svgs/4536919_no_text_no_attribution.svg',
  '/floral_svgs/cherry_blossom_cropped.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(FLORAL_CACHE)
      .then((cache) =>
        Promise.all(
          // One asset failing (offline install, partial deploy) must not fail
          // the whole precache: each URL settles on its own.
          FLORAL_ASSETS.map((url) =>
            fetch(new Request(url, { cache: 'reload' }))
              .then((response) => {
                if (response.ok) return cache.put(url, response);
                return undefined;
              })
              .catch(() => undefined),
          ),
        ),
      )
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((key) => key !== FLORAL_CACHE).map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith('/floral_svgs/')) return;
  // Cache-first, deliberately ignoring the request's own cache mode: this is
  // what keeps serving branches through a hard refresh. The network fills
  // the cache on the first miss (first visit, new version).
  event.respondWith(
    caches.open(FLORAL_CACHE).then((cache) =>
      cache.match(request, { ignoreSearch: true }).then(
        (hit) =>
          hit ??
          fetch(request).then((response) => {
            if (response.ok) cache.put(request, response.clone()).catch(() => undefined);
            return response;
          }),
      ),
    ),
  );
});
