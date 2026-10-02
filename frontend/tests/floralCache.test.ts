import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { FLORAL_PAIRS } from '@/components/FloralDecor';
import { initFloralCaching } from '@/lib/floralCache';

// The module dedupes per session, so one test owns the single first run;
// requestIdleCallback is absent in jsdom, which takes the setTimeout path.
describe('floral precaching', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true } as Response),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('fetches every floral asset once idle, exactly once per session', async () => {
    expect(window.requestIdleCallback).toBeUndefined();

    initFloralCaching();
    expect(fetch).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(3000);
    expect(fetch).toHaveBeenCalledTimes(FLORAL_PAIRS.length);
    for (const { src } of FLORAL_PAIRS) {
      expect(fetch).toHaveBeenCalledWith(src, { credentials: 'same-origin' });
    }

    // A second startup call must not refetch.
    initFloralCaching();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(fetch).toHaveBeenCalledTimes(FLORAL_PAIRS.length);
  });
});
