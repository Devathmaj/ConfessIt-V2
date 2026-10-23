/** Register the static asset worker after the first paint. The ordered warmup itself
 * starts from the login screen in assetWarmup.ts, so it can prioritize critical media. */
export const initFloralCaching = () => {
  if (!import.meta.env.PROD || !('serviceWorker' in navigator)) return;
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/sw.js').catch(() => undefined);
  });
};

export default initFloralCaching;
