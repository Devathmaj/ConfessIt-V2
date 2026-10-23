/**
 * ConfessIt service worker: immutable static assets and public profile pictures only.
 *
 * It deliberately never handles API, chat, auth, admin or report traffic. Those
 * responses are per-viewer, often sensitive, and are served with `no-store`; a
 * service worker cache would survive logout and could paint one account's data
 * for the next person at the same browser. The route guards below are the
 * enforcement point, not a convention.
 *
 * Cache names carry a version. A deploy that changes a stable filename (the
 * floral artwork, a template) bumps the version and the activate handler deletes
 * every older `confessit-*` cache. Hashed Vite assets are already versioned by
 * their filename, which is what makes the indefinite TTL safe. Mutable JSON/TXT
 * (the case templates) are network-first with a cache fallback, because their
 * stable filenames can change without changing the URL.
 */

const CACHE_VERSION = 'v3';
const STATIC_CACHE = `confessit-static-${CACHE_VERSION}`;
const PROFILE_CACHE = `confessit-profile-${CACHE_VERSION}`;
const PROFILE_META_CACHE = `confessit-profile-meta-${CACHE_VERSION}`;
const PROFILE_TTL_MS = 20 * 60 * 1000;
const STATIC_FILE = /\.(?:avif|css|eot|gif|ico|jpeg|jpg|js|json|mp4|png|svg|ttf|webm|webp|woff2?|txt)$/i;
// JSON and TXT are the mutable static formats in this app: the case templates
// are stable filenames whose contents may change without a new URL. They are
// network-first with a cache fallback; everything else keeps the cache-first
// policy, and hashed /assets/ files are immutable by filename.
const MUTABLE_STATIC_FILE = /\.(?:json|txt)$/i;

/**
 * Same-origin paths whose answers are never cacheable by this worker.
 *
 * `/api/` is the reverse-proxied API layout; the bare prefixes cover the dev
 * layout and any deployment that keeps the API on its own path. Chat and
 * WebSocket paths are listed explicitly because a transcript read must always
 * come from the authoritative backend after a reconnect.
 */
const SENSITIVE_PREFIX =
  /^\/(?:api|auth|admin|staff|notifications|messages|conversations|reports|profile)(?:\/|$)/i;
const CHAT_PATH = /(?:\/messages(?:\/|$)|\/conversations(?:\/|$)|\/case-closed\/ws(?:\/|$))/i;

const isSensitive = (url) =>
  url.origin === self.location.origin && (SENSITIVE_PREFIX.test(url.pathname) || CHAT_PATH.test(url.pathname));

self.addEventListener('install', (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then(async (keys) => {
      // Delete every cache this app owns that is not one of the current names.
      // The floral cache from the earlier implementation is included.
      await Promise.all(
        keys
          .filter(
            (key) =>
              (key.startsWith('confessit-') || key.startsWith('confessit-floral-')) &&
              key !== STATIC_CACHE &&
              key !== PROFILE_CACHE &&
              key !== PROFILE_META_CACHE,
          )
          .map((key) => caches.delete(key)),
      );
      await self.clients.claim();
    }),
  );
});

const profilePicture = (url) =>
  url.hostname === 'res.cloudinary.com' && url.pathname.includes('/image/upload/');

const staticAsset = (url) =>
  url.origin === self.location.origin &&
  url.pathname !== '/sw.js' &&
  !isSensitive(url) &&
  STATIC_FILE.test(url.pathname);

const cacheableResponse = (response) => {
  if (!response.ok) return false;
  const cacheControl = response.headers.get('cache-control') || '';
  return !/no-store|private/i.test(cacheControl);
};

const serveStatic = async (request) => {
  const cache = await caches.open(STATIC_CACHE);
  const hit = await cache.match(request);
  if (hit) return hit;
  const response = await fetch(request);
  if (cacheableResponse(response)) await cache.put(request, response.clone());
  return response;
};

const serveRevalidated = async (request) => {
  const cache = await caches.open(STATIC_CACHE);
  try {
    // `no-cache` makes the browser revalidate instead of handing back a fresh
    // copy from its own HTTP cache. The cached response is only an offline
    // fallback for these mutable files.
    const response = await fetch(request, { cache: 'no-cache' });
    if (cacheableResponse(response)) await cache.put(request, response.clone());
    return response;
  } catch (error) {
    const hit = await cache.match(request);
    if (hit) return hit;
    throw error;
  }
};

const serveProfilePicture = async (request) => {
  const cache = await caches.open(PROFILE_CACHE);
  const metadata = await caches.open(PROFILE_META_CACHE);
  const cached = await cache.match(request);
  const metadataKey = new Request(
    `${self.location.origin}/__profile-cache-meta__?url=${encodeURIComponent(request.url)}`,
  );
  const timestampResponse = await metadata.match(metadataKey);
  const storedAt = Number(timestampResponse ? await timestampResponse.text() : NaN);
  const fresh = cached && Number.isFinite(storedAt) && Date.now() - storedAt < PROFILE_TTL_MS;
  if (fresh) return cached;

  try {
    const response = await fetch(request);
    if (response.status === 404 || response.status === 410) {
      await cache.delete(request);
      await metadata.delete(metadataKey);
      return response;
    }
    // Cross-origin <img> requests can be opaque; their bytes remain cacheable,
    // while the separate same-origin metadata entry records the freshness time.
    if (response.ok || response.type === 'opaque') {
      await cache.put(request, response.clone());
      await metadata.put(metadataKey, new Response(String(Date.now())));
    }
    return response;
  } catch (error) {
    if (cached) return cached;
    throw error;
  }
};

const serveRange = async (request) => {
  const full = await serveStatic(new Request(request.url, { method: 'GET' }));
  const range = request.headers.get('range');
  if (!range || !full.ok) return full;
  const match = /^bytes=(\d*)-(\d*)$/.exec(range);
  if (!match) return full;
  const bytes = await full.arrayBuffer();
  const start = match[1] ? Number(match[1]) : 0;
  const end = match[2] ? Math.min(Number(match[2]), bytes.byteLength - 1) : bytes.byteLength - 1;
  if (start > end || start >= bytes.byteLength) {
    return new Response(null, { status: 416, headers: { 'Content-Range': `bytes */${bytes.byteLength}` } });
  }
  return new Response(bytes.slice(start, end + 1), {
    status: 206,
    headers: {
      'Content-Type': full.headers.get('content-type') || 'application/octet-stream',
      'Content-Length': String(end - start + 1),
      'Content-Range': `bytes ${start}-${end}/${bytes.byteLength}`,
      'Accept-Ranges': 'bytes',
    },
  });
};

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);

  // Never touch per-viewer, chat, auth, admin or report responses. Returning
  // without respondWith leaves them to the network and the browser's own
  // no-store handling.
  if (isSensitive(url)) return;

  // Authenticated love-note delivery URLs are durable bearer links. The inbox is
  // re-read from the database and filters removed rows before displaying them;
  // never persist their image responses in either service-worker cache.
  if (url.hostname === 'res.cloudinary.com' && url.pathname.includes('/image/authenticated/')) return;

  if (profilePicture(url)) {
    event.respondWith(serveProfilePicture(request));
    return;
  }
  if (staticAsset(url)) {
    if (MUTABLE_STATIC_FILE.test(url.pathname)) {
      event.respondWith(serveRevalidated(request));
      return;
    }
    event.respondWith(request.headers.has('range') ? serveRange(request) : serveStatic(request));
  }
});

/**
 * Explicit cleanup, for events that do not wait for the TTL: a new profile
 * picture makes the old bytes wrong immediately, and a sign-out can ask for a
 * clean worker state without trusting the activate hook to have run.
 */
self.addEventListener('message', (event) => {
  const type = event.data && event.data.type;
  if (type === 'CLEAR_PROFILE_CACHE') {
    event.waitUntil(
      Promise.all([caches.delete(PROFILE_CACHE), caches.delete(PROFILE_META_CACHE)]),
    );
  }
  if (type === 'CLEAR_CACHES') {
    event.waitUntil(
      caches.keys().then((keys) =>
        Promise.all(
          keys
            .filter((key) => key.startsWith('confessit-') || key.startsWith('confessit-floral-'))
            .map((key) => caches.delete(key)),
        ),
      ),
    );
  }
});
