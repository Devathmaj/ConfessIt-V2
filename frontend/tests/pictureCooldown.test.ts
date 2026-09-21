import { describe, it, expect } from 'vitest';
import {
  PICTURE_COOLDOWN_DAYS,
  formatPictureCooldownRemaining,
  readPictureCooldownRejection,
  secondsUntilPictureChange,
} from '@/lib/pictureCooldown';

/** 2026-01-01T00:00:00Z, fixed so the arithmetic below cannot drift with the clock. */
const CHANGED_AT = Date.UTC(2026, 0, 1);
const THREE_DAYS = PICTURE_COOLDOWN_DAYS * 24 * 60 * 60 * 1000;

describe('secondsUntilPictureChange', () => {
  it('reports the whole window when the change has just happened', () => {
    expect(secondsUntilPictureChange(new Date(CHANGED_AT).toISOString(), CHANGED_AT)).toBe(
      PICTURE_COOLDOWN_DAYS * 24 * 60 * 60,
    );
  });

  it('counts down as the clock moves', () => {
    const anHourLater = CHANGED_AT + 60 * 60 * 1000;

    expect(secondsUntilPictureChange(new Date(CHANGED_AT).toISOString(), anHourLater)).toBe(
      PICTURE_COOLDOWN_DAYS * 24 * 60 * 60 - 60 * 60,
    );
  });

  it('reports no wait once the window has passed', () => {
    const after = CHANGED_AT + THREE_DAYS + 1000;

    expect(secondsUntilPictureChange(new Date(CHANGED_AT).toISOString(), after)).toBe(0);
  });

  it('reports no wait for an account that has never changed its picture', () => {
    // Absent, null and an empty string are all "never", and all three must leave the
    // control enabled -- a participant who has never uploaded would otherwise be met with
    // a countdown for a change they never made.
    expect(secondsUntilPictureChange(undefined, CHANGED_AT)).toBe(0);
    expect(secondsUntilPictureChange(null, CHANGED_AT)).toBe(0);
    expect(secondsUntilPictureChange('', CHANGED_AT)).toBe(0);
  });

  it('reports no wait rather than crashing on a timestamp it cannot read', () => {
    // This value only ever decides what the screen shows before a request is made. Refusing
    // a picture because a value was malformed would be the server's job failing in the
    // client's hands.
    expect(secondsUntilPictureChange('not a date', CHANGED_AT)).toBe(0);
  });

  it('never reports a wait longer than the window itself', () => {
    // Clock skew the other way: this machine's clock is behind the server's, so the
    // timestamp arrives from the future. Read as "just happened", so the control is never
    // parked behind a wait the rule does not actually impose.
    const skewed = CHANGED_AT + THREE_DAYS * 2;

    expect(secondsUntilPictureChange(new Date(skewed).toISOString(), CHANGED_AT)).toBe(
      PICTURE_COOLDOWN_DAYS * 24 * 60 * 60,
    );
  });
});

describe('formatPictureCooldownRemaining', () => {
  it('spells the units out over a day', () => {
    // Abbreviations are for the countdown above a feed; this sits under an avatar, once.
    expect(formatPictureCooldownRemaining(2 * 86400 + 23 * 3600 + 59 * 60)).toBe(
      '2 days, 23 hours',
    );
  });

  it('drops the hours when there are none left', () => {
    expect(formatPictureCooldownRemaining(2 * 86400)).toBe('2 days');
  });

  it('counts hours and minutes below a day', () => {
    expect(formatPictureCooldownRemaining(3660)).toBe('1 hour, 1 minute');
    expect(formatPictureCooldownRemaining(3600)).toBe('1 hour');
  });

  it('counts minutes below an hour', () => {
    expect(formatPictureCooldownRemaining(120)).toBe('2 minutes');
    expect(formatPictureCooldownRemaining(60)).toBe('1 minute');
  });

  it('does not claim a whole minute when there is less than one', () => {
    // "0 minutes" reads as a broken notice; the wait is real, it is just short.
    expect(formatPictureCooldownRemaining(30)).toBe('under a minute');
    expect(formatPictureCooldownRemaining(0)).toBe('under a minute');
    expect(formatPictureCooldownRemaining(-30)).toBe('under a minute');
  });
});

describe('readPictureCooldownRejection', () => {
  /** A 429 as axios surfaces it: the server's own sentence plus Retry-After. */
  const tooManyRequests = (seconds: number, detail?: string) => ({
    response: {
      status: 429,
      data: detail === undefined ? {} : { detail },
      headers: { 'retry-after': String(seconds) },
    },
  });

  it('reads the wait from the server Retry-After', () => {
    // The write is the authority: the page may have loaded hours ago, so the refusal is
    // what the countdown has to start from rather than the timestamp it arrived with.
    const rejection = readPictureCooldownRejection(tooManyRequests(90000));

    expect(rejection?.seconds).toBe(90000);
  });

  it('quotes the server sentence when there is one', () => {
    const detail =
      'You can change your profile picture once every 3 days. Please try again in 2 days and 1 hour.';

    expect(readPictureCooldownRejection(tooManyRequests(180000, detail))?.message).toBe(detail);
  });

  it('states the rule when the server sent no sentence', () => {
    const rejection = readPictureCooldownRejection(tooManyRequests(600));

    expect(rejection?.message).toContain('once every 3 days');
    expect(rejection?.seconds).toBe(600);
  });

  it('still refuses with a notice when Retry-After is absent', () => {
    const rejection = readPictureCooldownRejection({
      response: { status: 429, data: { detail: 'Slow down.' }, headers: {} },
    });

    expect(rejection?.message).toBe('Slow down.');
    expect(rejection?.seconds).toBe(0);
  });

  it('does not read a 400 as a cooldown', () => {
    // The same endpoint answers 400 for a file that is not an image and 413 for one that
    // is too large. Telling somebody to wait three days over a mislabelled PNG would be
    // both wrong and unactionable.
    expect(
      readPictureCooldownRejection({
        response: {
          status: 400,
          data: { detail: 'Invalid file type. Only images are allowed' },
          headers: {},
        },
      }),
    ).toBeNull();
  });

  it('ignores an error with no response at all', () => {
    expect(readPictureCooldownRejection(new Error('Network Error'))).toBeNull();
    expect(readPictureCooldownRejection(undefined)).toBeNull();
  });
});
