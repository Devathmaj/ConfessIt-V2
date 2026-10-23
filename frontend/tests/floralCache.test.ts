import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { FLORAL_PAIRS } from '@/components/FloralDecor';
import { initFloralCaching } from '@/lib/floralCache';
import { warmStaticAssets } from '@/lib/assetWarmup';

/**
 * Static delivery moved from an idle fetch burst to an ordered warmup that starts
 * on the login screen. The old test asserted the burst; these assert the two
 * halves that still exist: the worker registration is production-only, and the
 * warmup fills the static cache in priority order exactly once.
 */
describe('static asset delivery', () => {
  const put = vi.fn();
  const open = vi.fn(async () => ({ put }));

  beforeEach(() => {
    // jsdom's Request rejects the root-relative asset URLs Vite hands the
    // module, which would make every warmup a silent no-op in this test.
    vi.stubGlobal(
      'Request',
      class {
        url: string;
        constructor(input: string | { url: string }) {
          this.url = typeof input === 'string' ? input : input.url;
        }
      },
    );
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true } as Response));
    Object.defineProperty(window, 'caches', {
      configurable: true,
      value: { open },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete (window as { caches?: unknown }).caches;
  });

  it('does not register a worker outside production', () => {
    const register = vi.fn();
    Object.defineProperty(navigator, 'serviceWorker', {
      configurable: true,
      value: { register },
    });

    initFloralCaching();
    window.dispatchEvent(new Event('load'));

    expect(register).not.toHaveBeenCalled();
    delete (navigator as { serviceWorker?: unknown }).serviceWorker;
  });

  it('warms critical media before floral art, exactly once per session', async () => {
    const first = warmStaticAssets();
    const second = warmStaticAssets();
    expect(second).toBe(first);

    await first;

    const urls = vi.mocked(fetch).mock.calls.map(([request]) =>
      request instanceof Request ? request.url : String(request),
    );
    expect(urls[0]).toContain('ConfessIt');
    expect(urls.some((url) => url.includes('Dashboard'))).toBe(true);
    for (const { src } of FLORAL_PAIRS) {
      expect(urls.some((url) => url.includes(src))).toBe(true);
    }
    expect(urls.at(-1)).toContain('/templates/template_6.json');
    expect(put).toHaveBeenCalledTimes(urls.length);
  });
});
