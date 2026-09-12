import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { toast } from 'sonner';
import {
  EXPIRED_MATCH_NOTICE,
  timeUntilNextMatch,
  notifyExpiredMatch,
} from '@/pages/MatchmakingPage';

vi.mock('sonner', () => ({
  toast: { info: vi.fn() },
}));

const infoCalls = () => vi.mocked(toast.info).mock.calls;

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

describe('notifyExpiredMatch', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(toast.info).mockClear();
  });

  it('announces the expiry once', () => {
    notifyExpiredMatch('match-1');
    expect(infoCalls()).toHaveLength(1);
    expect(infoCalls()[0][0]).toBe(EXPIRED_MATCH_NOTICE);
  });

  it('stays quiet on every later visit for the same match', () => {
    // The regression: the page re-reads the current match on every mount, so an expired
    // match kept re-announcing itself on each visit.
    notifyExpiredMatch('match-1');
    notifyExpiredMatch('match-1');
    notifyExpiredMatch('match-1');
    expect(infoCalls()).toHaveLength(1);
  });

  it('still announces the next match that expires', () => {
    notifyExpiredMatch('match-1');
    notifyExpiredMatch('match-2');
    expect(infoCalls()).toHaveLength(2);
  });

  it('re-announces a match it has forgotten', () => {
    // A stale key from an earlier session must not suppress a genuine first notice.
    localStorage.setItem('expired_match_notice_shown_for', 'some-older-match');
    notifyExpiredMatch('match-1');
    expect(infoCalls()).toHaveLength(1);
  });

  it('keeps announcing when there is no match id to key on', () => {
    // Without an id there is nothing safe to deduplicate against, and suppressing on an
    // unknown key would silence every future notice.
    notifyExpiredMatch(undefined);
    notifyExpiredMatch(undefined);
    expect(infoCalls()).toHaveLength(2);
  });

  it('does not accumulate a key per match', () => {
    notifyExpiredMatch('match-1');
    notifyExpiredMatch('match-2');
    notifyExpiredMatch('match-3');
    const keys = Object.keys(localStorage).filter(k => k.startsWith('expired_match_notice'));
    expect(keys).toEqual(['expired_match_notice_shown_for']);
  });
});
