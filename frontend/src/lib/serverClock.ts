/**
 * Client clock that measures itself against the server.
 *
 * The API is authoritative about time: it processes and stores UTC, and serves every
 * timestamp with an explicit offset. A browser clock is not a reliable second opinion. A
 * device whose clock is minutes or hours out would decide on its own that a conversation
 * has expired or that a cooldown has lifted, and no amount of correct timestamps on the
 * wire would correct that.
 *
 * So the browser keeps using its own high-resolution `Date.now()` -- which is monotonic
 * and unaffected by the user changing their system clock mid-session -- but re-based onto
 * the server with a single offset measured at startup. `serverNow()` is the single source
 * of "what time is it" for anything the user sees; `parseTimestamp` stays responsible for
 * turning a wire timestamp into a Date for display.
 *
 * Before the first sync, or if it fails, the offset is zero and `serverNow()` is simply the
 * device clock. That is the previous behaviour, so a failure degrades to the old assumption
 * rather than breaking the page.
 */

import { API_URL } from './apiBase';

let offsetMs = 0;
let synced = false;

/** Milliseconds to add to the device clock to get server time. Zero until the first sync. */
let inFlight: Promise<number> | null = null;

export function serverNow(): number {
  return Date.now() + offsetMs;
}

/** True once a measurement has landed, so a caller can tell "in sync" from "assuming". */
export function isServerClockSynced(): boolean {
  return synced;
}

export function serverClockOffsetMs(): number {
  return offsetMs;
}

/** Test hook: pin the offset without a network round trip. */
export function __setServerClockOffsetForTest(ms: number, markSynced = true): void {
  offsetMs = ms;
  synced = markSynced;
  inFlight = null;
}

/**
 * Measure the device-to-server offset and remember it.
 *
 * The request is timed at both ends and the server's stamp is compared against the
 * *midpoint* of the round trip. Using either end instead would fold the request latency
 * into the offset, so a slow connection would make the client's "now" permanently wrong by
 * half a round trip.
 *
 * Concurrent callers share one request. Safe to call repeatedly: once synced, a refresh
 * is only worth doing if a long-lived tab has been left open long enough for drift to
 * matter, so `force` opts into that.
 */
export async function syncServerClock(force = false): Promise<number> {
  if (synced && !force) return offsetMs;
  if (inFlight) return inFlight;

  inFlight = (async () => {
    const sentAt = Date.now();
    try {
      const response = await fetch(`${API_URL}/api/time`, { cache: 'no-store', signal: AbortSignal.timeout(10000) });
      if (!response.ok) throw new Error(`server time responded ${response.status}`);
      const payload = await response.json();
      const receivedAt = Date.now();

      const serverMs = Number(payload?.epoch_ms);
      if (!Number.isFinite(serverMs)) throw new Error('server time had no epoch_ms');

      offsetMs = serverMs - (sentAt + receivedAt) / 2;
      synced = true;
    } catch {
      // Leave the offset at zero: the device clock, which is what every caller used
      // before this existed. A time sync is not worth failing a page render over.
      offsetMs = 0;
      synced = false;
    } finally {
      inFlight = null;
    }
    return offsetMs;
  })();

  return inFlight;
}
