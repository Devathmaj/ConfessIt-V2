import { describeApiError } from './apiError';

/** Shown when the server refuses a post without saying why in a form that can be rendered. */
const DEFAULT_COOLDOWN_MESSAGE =
  'You can post one confession every 2 hours. Please try again shortly.';

/**
 * The confession cooldown, as the compose screen needs it.
 *
 * One confession per participant every two hours, enforced on the server by a conditional
 * UPDATE on `users.last_confession_at` (see `backend/app/services/confession_service.py`).
 * Nothing here decides whether a post is allowed -- `POST /confessions` claims the window
 * and answers for itself. This module only turns the server's answer into something to show,
 * and the countdown that follows it.
 *
 * The countdown is why the raw seconds are on the response at all: the server's `message` is
 * a sentence for a person, and a sentence that re-renders once a second while it counts down
 * would say "1 hour and 0 minutes" for a whole minute and then jump. So the message is shown
 * once, verbatim, and the ticking number is derived from `remaining_seconds` locally.
 *
 * Kept out of `ConfessionsPage` so it can be unit tested on its own, and so the page can
 * hot-reload: a module exporting both a component and plain functions cannot Fast Refresh.
 */

/** What the server says about the wait, if one is running. */
export type ConfessionCooldown = {
  /** The server's own sentence. Shown verbatim, never recomposed from the numbers. */
  message: string;
  /** Hours left, at the moment the answer came back. Drives the hours/minutes breakdown. */
  hours: number;
  /** Minutes left within the current hour. */
  minutes: number;
  /** Seconds left in total. The countdown counts down from this. */
  seconds: number;
};

/**
 * Read `GET /confessions/cooldown`.
 *
 * A response that does not say `cooldown` is an answer of "eligible", not a mystery, so
 * anything else reads as no wait. `remaining_seconds` is preferred over the minutes
 * breakdown for the same reason the server sends it: a countdown off a rounded minute can be
 * a full minute out of date the moment it starts.
 */
export const readConfessionCooldown = (response: unknown): ConfessionCooldown | null => {
  const body = response as {
    status?: string;
    message?: string;
    remaining_minutes?: number;
    remaining_seconds?: number;
  } | null;

  if (!body || body.status !== 'cooldown') return null;

  const seconds = Number(body.remaining_seconds);
  if (Number.isFinite(seconds) && seconds > 0) {
    return {
      message:
        body.message ??
        'You can post one confession every 2 hours. Please try again shortly.',
      hours: Math.floor(seconds / 3600),
      minutes: Math.floor((seconds % 3600) / 60),
      seconds,
    };
  }

  // A wait with no usable number to count down is still a wait, so the message is honoured
  // and the breakdown falls back to the minutes the server rounded to.
  const minutes = Number(body.remaining_minutes);
  return {
    message:
      body.message ?? 'You can post one confession every 2 hours. Please try again shortly.',
    hours: 0,
    minutes: Number.isFinite(minutes) ? minutes : 0,
    seconds: 0,
  };
};

/**
 * One line of the notice, in the form a person would say it.
 *
 * "1h 04m" rather than "1 hour 4 minutes": the notice sits above a feed, and it ticks.
 */
export const formatCooldownRemaining = (seconds: number): string => {
  const remaining = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(remaining / 3600);
  const minutes = Math.floor((remaining % 3600) / 60);
  const secs = remaining % 60;

  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`;
  if (minutes > 0) return `${minutes}m ${String(secs).padStart(2, '0')}s`;
  return `${secs}s`;
};

/**
 * What a 429 from `POST /confessions` means.
 *
 * The 429 is the authority, not the advisory check: a user can leave the compose dialog open
 * for an hour, and the write is what refuses. The `Retry-After` header is the server's own
 * count, so it is preferred over parsing the message, and the message is the fallback so a
 * refusal is still explained in the server's words rather than a generic "try again".
 */
export const readCooldownRejection = (error: unknown): ConfessionCooldown | null => {
  const response = (error as { response?: { status?: number; data?: { detail?: unknown }; headers?: Record<string, string> } })
    ?.response;

  // Only a 429 is a cooldown. A 400 from the same endpoint is the body-length or unstorable
  // character rule, and telling somebody to wait two hours over a control character would be
  // both wrong and unactionable.
  if (!response || response.status !== 429) return null;

  const retryAfter = Number(response.headers?.['retry-after'] ?? response.headers?.['Retry-After']);
  const seconds = Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : 0;
  // Read through `describeApiError` rather than as `detail` directly. This branch only fires on
  // a 429, which the server currently raises with a plain string, but `detail` is typed as
  // string-or-array by FastAPI in general -- and an array assigned to this `message: string`
  // would render as React children downstream and take the page with it.
  const message =
    describeApiError({ response }, DEFAULT_COOLDOWN_MESSAGE) || DEFAULT_COOLDOWN_MESSAGE;

  if (seconds > 0) {
    return {
      message,
      hours: Math.floor(seconds / 3600),
      minutes: Math.floor((seconds % 3600) / 60),
      seconds,
    };
  }

  return {
    message,
    hours: 0,
    minutes: 0,
    seconds: 0,
  };
};
