import { FLORAL_PAIRS } from '@/components/FloralDecor';

/**
 * Aggressive floral delivery: the branches reach the browser before anyone
 * logs in and stay cached heavily afterwards.
 *
 * Two layers, both deferred so the login video, poster and first paint win
 * every race:
 * - Idle prefetch: once the browser is idle, every FLORAL_PAIRS URL is
 *   fetched, warming the HTTP cache (and the worker's Cache Storage once it
 *   is active). Runs once per session; a failure is silent because the <img>
 *   tags load the same URLs on demand anyway.
 * - Service worker (`public/sw.js`): registered after `load`, precaches the
 *   same set into versioned Cache Storage and serves it cache-first, which is
 *   what survives even a hard refresh. Production only, so dev HMR and the
 *   jsdom test run never see a worker.
 */
let prefetchStarted = false;

const prefetchFloralAssets = () => {
  if (prefetchStarted) return;
  prefetchStarted = true;
  const run = () => {
    for (const { src } of FLORAL_PAIRS) {
      void fetch(src, { credentials: 'same-origin' }).catch(() => undefined);
    }
  };
  if (typeof window.requestIdleCallback === 'function') {
    window.requestIdleCallback(run, { timeout: 5000 });
  } else {
    window.setTimeout(run, 3000);
  }
};

const registerFloralWorker = () => {
  if (!import.meta.env.PROD || !('serviceWorker' in navigator)) return;
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/sw.js').catch(() => undefined);
  });
};

/** Call once at startup (main.tsx). Safe to call again: both halves are idempotent. */
export const initFloralCaching = () => {
  prefetchFloralAssets();
  registerFloralWorker();
};

export default initFloralCaching;
