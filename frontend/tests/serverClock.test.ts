import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  serverNow,
  isServerClockSynced,
  serverClockOffsetMs,
  syncServerClock,
  __setServerClockOffsetForTest,
} from '@/lib/serverClock';

/**
 * The server is the authority on time. These tests cover the client's half of that: it
 * measures its own drift against the server once, re-bases onto it, and falls back to the
 * device clock rather than breaking if the measurement fails.
 */

const DEVICE_NOW = 1_700_000_000_000;

/**
 * Stand in for `GET /api/time`.
 *
 * `serverEpoch` is the server's clock reading at the *exchange* -- that is, at the midpoint
 * of the round trip, which is the standard convention for a one-way time exchange and what
 * makes a client's `server - (sent + received) / 2` estimate the true skew. A real server
 * stamps while building the response and the client cannot know exactly when that was, so
 * the midpoint is the estimate; without a delay in the mock the two coincide and the
 * arithmetic is checked exactly.
 */
function mockFetchReturningServerEpoch(serverEpoch: number, roundTripMs = 0) {
  return vi.fn(async () => {
    if (roundTripMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, roundTripMs));
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({
        now: '2026-01-01T00:00:00+00:00',
        epoch_ms: serverEpoch + roundTripMs / 2,
      }),
    } as unknown as Response;
  });
}

describe('serverNow', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(DEVICE_NOW);
  });

  afterEach(() => {
    __setServerClockOffsetForTest(0, false);
    vi.useRealTimers();
  });

  it('is the device clock when nothing has been measured', () => {
    expect(serverNow()).toBe(DEVICE_NOW);
    expect(isServerClockSynced()).toBe(false);
  });

  it('adds the measured offset', () => {
    __setServerClockOffsetForTest(3_600_000); // device is an hour behind the server

    expect(serverNow()).toBe(DEVICE_NOW + 3_600_000);
    expect(isServerClockSynced()).toBe(true);
  });

  it('corrects a device clock that is ahead too', () => {
    __setServerClockOffsetForTest(-7_200_000);

    expect(serverNow()).toBe(DEVICE_NOW - 7_200_000);
  });

  it('advances with the device clock, so countdowns still tick', () => {
    __setServerClockOffsetForTest(60_000);

    vi.advanceTimersByTime(5_000);

    expect(serverNow()).toBe(DEVICE_NOW + 65_000);
  });
});

describe('syncServerClock', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(DEVICE_NOW);
    __setServerClockOffsetForTest(0, false);
  });

  afterEach(() => {
    __setServerClockOffsetForTest(0, false);
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('measures a positive offset when the device is behind', async () => {
    const serverMs = DEVICE_NOW + 3_600_000;
    vi.stubGlobal('fetch', mockFetchReturningServerEpoch(serverMs));

    await syncServerClock();

    expect(serverClockOffsetMs()).toBe(3_600_000);
    expect(isServerClockSynced()).toBe(true);
  });

  it('measures a negative offset when the device is ahead', async () => {
    vi.stubGlobal('fetch', mockFetchReturningServerEpoch(DEVICE_NOW - 900_000));

    await syncServerClock();

    expect(serverClockOffsetMs()).toBe(-900_000);
  });

  it('reports zero offset when the clocks already agree', async () => {
    vi.stubGlobal('fetch', mockFetchReturningServerEpoch(DEVICE_NOW));

    await syncServerClock();

    expect(serverClockOffsetMs()).toBe(0);
  });

  it('uses the midpoint of the round trip, not either end', async () => {
    // The clock has to advance during the request for a round trip to exist at all, so
    // these fake timers advance on their own. The round trip is 400ms: a start- or
    // end-based comparison would be off by the 200ms half-trip, well outside the
    // tolerance, while the midpoint cancels the delay symmetrically.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(DEVICE_NOW);
    __setServerClockOffsetForTest(0, false);
    vi.stubGlobal('fetch', mockFetchReturningServerEpoch(DEVICE_NOW + 100_000, 400));

    await syncServerClock();

    expect(Math.abs(serverClockOffsetMs() - 100_000)).toBeLessThan(60);
  });

  it('requests the server clock with caching disabled', async () => {
    const fetchMock = mockFetchReturningServerEpoch(DEVICE_NOW);
    vi.stubGlobal('fetch', fetchMock);

    await syncServerClock();

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/time'),
      expect.objectContaining({ cache: 'no-store' })
    );
  });

  it('does not measure again once synced', async () => {
    const fetchMock = mockFetchReturningServerEpoch(DEVICE_NOW + 1_000);
    vi.stubGlobal('fetch', fetchMock);

    await syncServerClock();
    await syncServerClock();

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('re-measures when explicitly forced', async () => {
    const fetchMock = mockFetchReturningServerEpoch(DEVICE_NOW + 1_000);
    vi.stubGlobal('fetch', fetchMock);

    await syncServerClock();
    await syncServerClock(true);

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('shares one request between concurrent callers', async () => {
    const fetchMock = mockFetchReturningServerEpoch(DEVICE_NOW + 500);
    vi.stubGlobal('fetch', fetchMock);

    await Promise.all([syncServerClock(), syncServerClock(), syncServerClock()]);

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('when the server clock is unavailable', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(DEVICE_NOW);
    __setServerClockOffsetForTest(0, false);
  });

  afterEach(() => {
    __setServerClockOffsetForTest(0, false);
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('falls back to the device clock on a network error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('offline');
      })
    );

    await expect(syncServerClock()).resolves.toBe(0);
    expect(isServerClockSynced()).toBe(false);
    expect(serverNow()).toBe(DEVICE_NOW);
  });

  it('falls back on a non-OK response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 503, json: async () => ({}) }) as unknown as Response)
    );

    await syncServerClock();

    expect(serverClockOffsetMs()).toBe(0);
    expect(isServerClockSynced()).toBe(false);
  });

  it('falls back when the payload has no usable epoch_ms', async () => {
    // A null/NaN offset would make every comparison false, so a match would look
    // unexpired forever. Silently keeping the device clock is the safer failure.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ now: 'x' }) }) as unknown as Response)
    );

    await syncServerClock();

    expect(Number.isNaN(serverClockOffsetMs())).toBe(false);
    expect(serverNow()).toBe(DEVICE_NOW);
  });

  it('does not throw', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('boom');
      })
    );

    await expect(syncServerClock()).resolves.toBeDefined();
  });
});
