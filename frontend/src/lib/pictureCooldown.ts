import { describeApiError } from './apiError';

/**
 * The profile-picture change rule, as the Profile screen needs it.
 *
 * A participant may replace their picture once every `PICTURE_COOLDOWN_DAYS`, enforced on
 * the server by a conditional UPDATE on `users.last_profile_picture_at` (see
 * `backend/app/services/profile_service.py`). Nothing here decides whether a change is
 * allowed -- `POST /profile/upload-picture` claims the window and answers for itself. This
 * module only turns what the server has already said into something a person can read:
 *
 * - `secondsUntilPictureChange` reads the timestamp `/auth/me` carries, so the screen can
 *   state the rule and show the wait *before* anybody picks a file.
 * - `readPictureCooldownRejection` reads the 429 for the case that survives that: a page
 *   left open past the moment it loaded, where the refusal is the only fresh answer.
 *
 * The client recomputing a wait from a timestamp is deliberately advisory in both
 * directions. A clock skew that says "eligible" costs one refused request, which the server
 * then explains; a clock skew that says "wait" only ever delays a change the server would
 * have accepted, and only until the next sign-in refreshes the timestamp.
 */

/** Mirrors `PROFILE_PICTURE_COOLDOWN_DAYS` in `backend/app/services/profile_service.py`. */
export const PICTURE_COOLDOWN_DAYS = 3;

const COOLDOWN_MILLIS = PICTURE_COOLDOWN_DAYS * 24 * 60 * 60 * 1000;

/** Shown when a refusal arrives without a sentence of its own. */
const DEFAULT_REJECTION_MESSAGE =
  'You can change your profile picture once every 3 days. Please try again later.';

/** What a 429 from `POST /profile/upload-picture` means. */
export type PictureCooldownRejection = {
  /** The server's own sentence, or the default above when it sent none. */
  message: string;
  /** Seconds left, from `Retry-After`. Zero when the server did not count one out. */
  seconds: number;
};

/**
 * Seconds until `lastChangeISO` stops being a block on a new picture. Zero when there is
 * nothing to wait out.
 *
 * A timestamp that cannot be parsed reads as "no wait" rather than as an error: the field
 * is only ever a hint to the screen, and refusing a participant's picture because a value
 * was malformed would be the server's job failing in the client's hands.
 */
export const secondsUntilPictureChange = (
  lastChangeISO: string | null | undefined,
  now: number = Date.now(),
): number => {
  if (!lastChangeISO) return 0;
  const changedAt = Date.parse(lastChangeISO);
  if (!Number.isFinite(changedAt)) return 0;
  // A timestamp ahead of this clock is this machine's clock being behind the server's, not
  // a change made in the future. Treating it as "just happened" keeps the wait inside one
  // window, so a skewed clock can never park the control behind a longer wait than the rule
  // itself allows.
  const startedAt = Math.min(changedAt, now);
  return Math.max(0, Math.ceil((startedAt + COOLDOWN_MILLIS - now) / 1000));
};

/**
 * The wait in the form a person would say it: "2 days, 4 hours".
 *
 * Not `2d 04h`. The confession countdown is abbreviated because it ticks above a feed;
 * this one sits under an avatar, once, and spells the units out so "3 days" cannot be
 * mistaken for hours.
 */
export const formatPictureCooldownRemaining = (seconds: number): string => {
  const total = Math.max(0, Math.floor(seconds));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);

  const unit = (value: number, word: string) => `${value} ${word}${value === 1 ? '' : 's'}`;

  if (days > 0) {
    return hours > 0 ? `${unit(days, 'day')}, ${unit(hours, 'hour')}` : unit(days, 'day');
  }
  if (hours > 0) {
    return minutes > 0 ? `${unit(hours, 'hour')}, ${unit(minutes, 'minute')}` : unit(hours, 'hour');
  }
  if (minutes > 0) return unit(minutes, 'minute');
  return 'under a minute';
};

/**
 * What a 429 from the upload endpoint means, or `null` for any other failure.
 *
 * Only a 429 is a cooldown. The same endpoint answers 400 for a file that is not an image
 * and 413 for one that is too large, and telling somebody to wait three days over a
 * mislabelled PNG would be both wrong and unactionable.
 *
 * `Retry-After` is preferred over the message because it is the server's own count and the
 * message may have been composed against a clock that has moved on since.
 */
export const readPictureCooldownRejection = (
  error: unknown,
): PictureCooldownRejection | null => {
  const response = (
    error as {
      response?: {
        status?: number;
        data?: { detail?: unknown };
        headers?: Record<string, string>;
      };
    }
  )?.response;

  if (!response || response.status !== 429) return null;

  const retryAfter = Number(
    response.headers?.['retry-after'] ?? response.headers?.['Retry-After'],
  );
  const seconds = Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : 0;

  return {
    // Read through `describeApiError` rather than as `detail` directly: `detail` is typed
    // string-or-array by FastAPI in general, and an array handed to a toast would be
    // rendered as React children.
    message:
      describeApiError({ response }, DEFAULT_REJECTION_MESSAGE) || DEFAULT_REJECTION_MESSAGE,
    seconds,
  };
};
