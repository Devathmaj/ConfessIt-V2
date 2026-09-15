import { describe, it, expect, vi, beforeEach } from 'vitest';
import { toast } from 'sonner';
import {
  EXPIRED_MATCH_NOTICE,
  readCooldown,
  describeExpiredMatch,
  formatRemaining,
  notifyExpiredMatch,
  cooldownPermitsMatch,
  UNANSWERED_COOLDOWN,
  type CooldownRemaining,
} from '@/pages/MatchmakingPage';

vi.mock('sonner', () => ({
  toast: { info: vi.fn() },
}));

const infoCalls = () => vi.mocked(toast.info).mock.calls;

/** What GET /matchmaking/check answers while a cooldown is still running. */
const COOLDOWN_RESPONSE = {
  status: 'cooldown',
  message: 'You are on a cooldown. Please try again in 3 hours and 20 minutes.',
  remaining_hours: 3,
  remaining_minutes: 20,
};

const cooldown = readCooldown(COOLDOWN_RESPONSE) as CooldownRemaining;

describe('readCooldown', () => {
  it('reads the server answer while a cooldown is running', () => {
    expect(cooldown).toEqual({
      hours: 3,
      minutes: 20,
      message: 'You are on a cooldown. Please try again in 3 hours and 20 minutes.',
    });
  });

  it('reports no cooldown when the server says the user may draw', () => {
    // The regression: the page only ever looked for a "matched" branch here, which the check
    // endpoint does not answer with, so a running cooldown was never read off the response at
    // all -- not on mount and not on a heart click.
    expect(readCooldown({ status: 'eligible', message: 'Proceed?' })).toBeNull();
  });

  it('carries a message even if the server omits one', () => {
    const read = readCooldown({ status: 'cooldown', remaining_hours: 1, remaining_minutes: 5 });

    expect(read?.message).toMatch(/cooldown/i);
    expect(read?.hours).toBe(1);
    expect(read?.minutes).toBe(5);
  });
});

describe('cooldownPermitsMatch', () => {
  it('permits a draw on an eligible answer', () => {
    expect(cooldownPermitsMatch({ known: true, remaining: null })).toBe(true);
  });

  it('refuses a draw while the wait is running', () => {
    expect(cooldownPermitsMatch({ known: true, remaining: cooldown })).toBe(false);
  });

  it('refuses a draw when the check never came back', () => {
    // The one that matters. `known: false` used to be folded into `remaining: null`, so a
    // failed request read as permission and the user was sent through the whole matching
    // animation to be turned away by the draw endpoint at the end of it.
    expect(cooldownPermitsMatch(UNANSWERED_COOLDOWN)).toBe(false);
  });
});

describe('describeExpiredMatch', () => {
  it('quotes the cooldown while one is still running', () => {
    // The bug: a moderator-terminated pair was told "You can find a new match now" while the
    // server was inside the cooldown, because an expiry was assumed to mean the wait was over.
    expect(describeExpiredMatch(cooldown)).toBe(COOLDOWN_RESPONSE.message);
    expect(describeExpiredMatch(cooldown)).not.toBe(EXPIRED_MATCH_NOTICE);
    expect(describeExpiredMatch(cooldown)).not.toMatch(/find a new match/i);
  });

  it('announces a new match when the cooldown has genuinely lifted', () => {
    expect(describeExpiredMatch(null)).toBe(EXPIRED_MATCH_NOTICE);
  });
});

describe('formatRemaining', () => {
  it('counts hours and minutes', () => {
    expect(formatRemaining(cooldown)).toBe('3h 20m');
  });

  it('drops the hours under an hour', () => {
    expect(formatRemaining({ hours: 0, minutes: 45, message: '' })).toBe('45m');
  });
});

describe('notifyExpiredMatch with a cooldown running', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(toast.info).mockClear();
  });

  it('says the cooldown rather than promising a match', () => {
    notifyExpiredMatch('match-1', cooldown);

    expect(infoCalls()).toHaveLength(1);
    expect(infoCalls()[0][0]).toBe(COOLDOWN_RESPONSE.message);
  });

  it('still announces only once per match', () => {
    notifyExpiredMatch('match-1', cooldown);
    notifyExpiredMatch('match-1', cooldown);
    notifyExpiredMatch('match-1', cooldown);

    expect(infoCalls()).toHaveLength(1);
  });

  it('falls back to the expiry notice with no cooldown', () => {
    notifyExpiredMatch('match-1');

    expect(infoCalls()[0][0]).toBe(EXPIRED_MATCH_NOTICE);
  });
});
