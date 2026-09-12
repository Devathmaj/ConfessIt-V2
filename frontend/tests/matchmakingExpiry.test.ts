import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EXPIRED_MATCH_NOTICE, timeUntilNextMatch } from '@/pages/MatchmakingPage';

/** An ISO timestamp the given number of minutes from now, as the API would send it. */
const isoInMinutes = (minutes: number) =>
  new Date(Date.now() + minutes * 60_000).toISOString();

describe('timeUntilNextMatch', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-03-01T12:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('counts down while the match is still live', () => {
    expect(timeUntilNextMatch(isoInMinutes(30))).toBe('30m');
    expect(timeUntilNextMatch(isoInMinutes(90))).toBe('1h 30m');
  });

  it('reports "now" the moment the match expires', () => {
    // The regression: this used to add an hour past expiry, so a match that had been over
    // for 54 minutes still told the user to wait another 6. The cooldown and the match
    // expire at the same instant, so there is nothing left to count.
    expect(timeUntilNextMatch(isoInMinutes(-54))).toBe('now');
    expect(timeUntilNextMatch(isoInMinutes(-1))).toBe('now');
    expect(timeUntilNextMatch(isoInMinutes(0))).toBe('now');
  });

  it('never reports a wait for an already-expired match, however long ago', () => {
    expect(timeUntilNextMatch(isoInMinutes(-60))).toBe('now');
    expect(timeUntilNextMatch(isoInMinutes(-60 * 24))).toBe('now');
  });

  it('degrades to a dash rather than NaN on an unparseable timestamp', () => {
    expect(timeUntilNextMatch('not-a-date')).toBe('—');
    expect(timeUntilNextMatch('')).toBe('—');
  });
});

describe('EXPIRED_MATCH_NOTICE', () => {
  it('quotes no wait time', () => {
    expect(EXPIRED_MATCH_NOTICE).toBe(
      'Your previous match has expired. You can find a new match now.'
    );
    expect(EXPIRED_MATCH_NOTICE).not.toMatch(/\d+\s*(m|h|min|hour)/i);
  });
});
