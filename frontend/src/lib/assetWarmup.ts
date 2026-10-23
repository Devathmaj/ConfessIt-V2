import ConfessItImage from '@/assets/ConfessIt.webp';
import ConfessItWebm from '@/assets/ConfessIt.webm';
import ConfessItMp4 from '@/assets/ConfessIt.mp4';
import DashboardImage from '@/assets/Dashboard.webp';
import DashboardWebm from '@/assets/Dashboard.webm';
import DashboardMp4 from '@/assets/Dashboard.mp4';
import { FLORAL_PAIRS } from '@/components/FloralDecor';

const STATIC_CACHE = 'confessit-static-v3';
let warmup: Promise<void> | null = null;

const cacheInOrder = async (cache: Cache, urls: string[]) => {
  for (const url of urls) {
    try {
      const request = new Request(url, { cache: 'reload', credentials: 'same-origin' });
      const response = await fetch(request);
      if (response.ok) await cache.put(request, response);
    } catch {
      // A missing file or temporary offline state must not stop later assets warming.
    }
  }
};

/** Warm the static asset groups in the order requested, beginning on the login screen. */
export const warmStaticAssets = (): Promise<void> => {
  if (warmup) return warmup;
  if (!('caches' in window)) return Promise.resolve();

  warmup = (async () => {
    try {
      // Ask the browser to protect the static Cache Storage entries from
      // routine quota eviction where persistent storage is available.
      if ('storage' in navigator && typeof navigator.storage.persist === 'function') {
        await navigator.storage.persist().catch(() => false);
      }
      const cache = await window.caches.open(STATIC_CACHE);
      await cacheInOrder(cache, [ConfessItImage]);
      await cacheInOrder(cache, [ConfessItWebm, ConfessItMp4]);
      await cacheInOrder(cache, [DashboardImage]);
      await cacheInOrder(cache, [DashboardWebm, DashboardMp4]);
      await cacheInOrder(cache, FLORAL_PAIRS.map(({ src }) => src));
      const templates = Array.from({ length: 6 }, (_, index) => index + 1);
      for (const number of templates) {
        await cacheInOrder(cache, [
          `/templates/template_${number}.png`,
          `/templates/template_${number}.json`,
        ]);
      }
    } catch {
      // Browser storage may be disabled or full; normal network loading still works.
    }
  })();
  return warmup;
};
