import { describe, it, expect } from 'vitest';
import {
  formatCooldownRemaining,
  readConfessionCooldown,
  readCooldownRejection,
} from '@/lib/confessionCooldown';

/** What GET /confessions/cooldown answers while the two-hour window is still running. */
const COOLDOWN_RESPONSE = {
  status: 'cooldown',
  message: 'You can post one confession every 2 hours. Please try again in 1 hour and 58 minutes.',
  cooldown_hours: 2,
  remaining_minutes: 118,
  remaining_seconds: 7080,
};

describe('readConfessionCooldown', () => {
  it('reads the wait off the advisory answer', () => {
    expect(readConfessionCooldown(COOLDOWN_RESPONSE)).toEqual({
      message: COOLDOWN_RESPONSE.message,
      hours: 1,
      minutes: 58,
      seconds: 7080,
    });
  });

  it('reports no wait when the server says the user may confess', () => {
    // "eligible" is an answer, not a missing field. Reading only the cooldown branch and
    // treating everything else as a failure would put a notice up for a user who may post.
    expect(readConfessionCooldown({ status: 'eligible', message: 'Proceed?' })).toBeNull();
  });

  it('keeps the server sentence rather than recomposing it', () => {
    // The message is what the user reads and it quotes the rule. Rebuilding it from the
    // numbers would be the second place the two-hour rule is written down.
    expect(readConfessionCooldown(COOLDOWN_RESPONSE)?.message).toContain('every 2 hours');
  });

  it('still reports a wait when the server sent no message', () => {
    const read = readConfessionCooldown({ status: 'cooldown', remaining_seconds: 600 });

    expect(read?.message).toMatch(/every 2 hours/);
    expect(read?.seconds).toBe(600);
  });

  it('falls back to the rounded minutes when there is no count to count down', () => {
    // A wait with no number is still a wait, so the notice must not silently vanish.
    const read = readConfessionCooldown({ status: 'cooldown', remaining_minutes: 42 });

    expect(read).toEqual({
      message: expect.stringMatching(/every 2 hours/),
      hours: 0,
      minutes: 42,
      seconds: 0,
    });
  });

  it('reports no seconds left when the server contradicts itself', () => {
    // `status: cooldown` with nothing left is a contradiction between two fields. The
    // sentence is still worth having, but a zero is what stops the page starting a
    // countdown that has already ended -- otherwise the notice reads "0s remaining" above
    // an enabled button.
    const read = readConfessionCooldown({
      status: 'cooldown',
      remaining_seconds: 0,
      remaining_minutes: 0,
    });

    expect(read?.seconds).toBe(0);
  });
});

describe('formatCooldownRemaining', () => {
  it('counts hours and minutes', () => {
    expect(formatCooldownRemaining(7080)).toBe('1h 58m');
  });

  it('counts minutes and seconds under an hour', () => {
    expect(formatCooldownRemaining(125)).toBe('2m 05s');
  });

  it('counts plain seconds under a minute', () => {
    expect(formatCooldownRemaining(9)).toBe('9s');
  });

  it('never renders a negative wait', () => {
    expect(formatCooldownRemaining(-30)).toBe('0s');
  });
});

describe('readCooldownRejection', () => {
  /** A 429 as axios surfaces it: the server's own sentence plus Retry-After. */
  const tooManyRequests = (seconds: number, detail?: string) => ({
    response: {
      status: 429,
      data: detail === undefined ? {} : { detail },
      headers: { 'retry-after': String(seconds) },
    },
  });

  it('starts the countdown from the server Retry-After', () => {
    // The write is the authority: a dialog can sit open for an hour, so the notice has to
    // come from the refusal rather than from the advisory check the page loaded with.
    const rejection = readCooldownRejection(tooManyRequests(5400));

    expect(rejection).toEqual({
      message: expect.stringMatching(/every 2 hours/),
      hours: 1,
      minutes: 30,
      seconds: 5400,
    });
  });

  it('quotes the server sentence when there is one', () => {
    const detail = 'You can post one confession every 2 hours. Please try again in 30 minutes.';

    expect(readCooldownRejection(tooManyRequests(1800, detail))?.message).toBe(detail);
  });

  it('still refuses with a notice when Retry-After is absent', () => {
    const rejection = readCooldownRejection({
      response: { status: 429, data: { detail: 'Slow down.' }, headers: {} },
    });

    expect(rejection?.message).toBe('Slow down.');
    expect(rejection?.seconds).toBe(0);
  });

  it('does not read a 400 as a cooldown', () => {
    // The same endpoint answers 400 for the unstorable-character and comment-limit rules.
    // Telling somebody to wait two hours over a control character would be unactionable.
    expect(readCooldownRejection({ response: { status: 400, data: {}, headers: {} } })).toBeNull();
  });

  it('ignores an error with no response at all', () => {
    expect(readCooldownRejection(new Error('Network Error'))).toBeNull();
    expect(readCooldownRejection(undefined)).toBeNull();
  });
});