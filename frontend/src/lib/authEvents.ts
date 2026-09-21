// src/lib/authEvents.ts

/**
 * Shared handling for the moment the API stops accepting the access token.
 *
 * The access token is a short-lived JWT, so a session can lapse while the tab is still
 * open. Every request then fails with 401, but nothing was watching for that status, so
 * the UI carried on looking signed in until a page load finally threw the token away.
 * These helpers give one place that notices the rejection and ends the session, and the
 * auth context turns the event into a real logout.
 */

/** Dispatched on `window` once a token-carrying request comes back 401. */
export const AUTH_EXPIRED_EVENT = 'confessit:auth-expired';

/**
 * Dispatched on `window` when the 401 says the session was replaced by a newer login.
 *
 * `AUTH_EXPIRED_EVENT` covers the ordinary end of a session. This one covers the
 * single-session rule: the token is still valid and unexpired, but the session it belongs
 * to was superseded by a sign-in on another device. The token is cleared either way; the
 * difference is that the auth context can tell the person it happened because they signed
 * in somewhere else, rather than leaving them to guess between "try again" and "gone".
 */
export const SESSION_SUPERSEDED_EVENT = 'confessit:session-superseded';

/**
 * The fallback wording when the 401 body is unavailable. The server sends this same sentence
 * as the detail, so the two only differ when the body cannot be read at all.
 */
export const SESSION_SUPERSEDED_MESSAGE =
  'You were signed out because a new session was opened on another device.';

/** The `WWW-Authenticate` error tag the backend uses to mark a replaced session. */
const SESSION_SUPERSEDED_ERROR = 'session_superseded';

/**
 * Whether a rejected response carries the marker for a session replaced by a newer login.
 *
 * The interceptor needs this before it decides what to do: a superseded 401 has to end the
 * session rather than be silently refreshed, and the story has to reach the person as
 * "signed in somewhere else". It is the same header `endSessionIfTokenWasRejected` reads,
 * factored out so the decision can be made without side effects.
 */
export const isSessionSupersededError = (error?: {
  response?: { headers?: Record<string, string | undefined> };
}) =>
  error?.response?.headers?.['www-authenticate']?.includes(SESSION_SUPERSEDED_ERROR) ?? false;

/**
 * End the session if a rejected request failed *because of* the access token.
 *
 * The stored token is what decides, not the status alone: a 401 from a request that never
 * carried one -- a failed login, a stale magic link -- is not a logout, and treating it as
 * one would clear a perfectly good session.
 *
 * The token is cleared before the event goes out. That both leaves nothing for the next
 * request to re-send, and collapses the burst of simultaneous 401s a page with several
 * requests in flight tends to produce into a single logout.
 *
 * A 401 whose `WWW-Authenticate` header carries the `session_superseded` error is a
 * different kind of end: the token is valid but its session was replaced by a newer login,
 * so the caller is told exactly that instead of a bare "expired".
 */
export const endSessionIfTokenWasRejected = (error?: {
  response?: {
    status?: number;
    headers?: Record<string, string | undefined>;
    data?: { detail?: unknown };
  };
}) => {
  if (error?.response?.status !== 401) return;
  if (typeof window === 'undefined') return;
  if (!localStorage.getItem('accessToken')) return;

  const superseded = isSessionSupersededError(error);
  localStorage.removeItem('accessToken');

  if (superseded) {
    const detail = error?.response?.data?.detail;
    window.dispatchEvent(
      new CustomEvent(SESSION_SUPERSEDED_EVENT, {
        detail: typeof detail === 'string' ? detail : SESSION_SUPERSEDED_MESSAGE,
      })
    );
    return;
  }
  window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
};
