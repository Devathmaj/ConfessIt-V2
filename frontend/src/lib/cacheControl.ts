/**
 * Small bridge to the service worker's explicit cleanup messages.
 *
 * The worker's cache names are versioned, so a deploy invalidates stable
 * filenames on its own. These messages exist for the two events that cannot wait
 * for a deploy or a TTL: a profile picture replaced by its owner, and an
 * operator/test asking for a clean worker state.
 *
 * Every call is best-effort. A browser without service-worker support, a
 * registration that is not active yet, or a page served in development (where
 * the worker is not registered) simply has nothing to clear.
 */

type WorkerMessage = 'CLEAR_PROFILE_CACHE' | 'CLEAR_CACHES';

const postToWorker = async (type: WorkerMessage): Promise<void> => {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;
  try {
    const registration = await navigator.serviceWorker.ready;
    registration.active?.postMessage({ type });
  } catch {
    // No controller/registration: nothing was cached by a worker on this page.
  }
};

/** The profile picture changed, so the 20-minute entry must not survive it. */
export const clearProfilePictureCache = (): Promise<void> =>
  postToWorker('CLEAR_PROFILE_CACHE');

/** Drop every `confessit-*` cache this app owns. */
export const clearServiceWorkerCaches = (): Promise<void> =>
  postToWorker('CLEAR_CACHES');
