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
 * End the session if a rejected request failed *because of* the access token.
 *
 * The stored token is what decides, not the status alone: a 401 from a request that never
 * carried one -- a failed login, a stale magic link -- is not a logout, and treating it as
 * one would clear a perfectly good session.
 *
 * The token is cleared before the event goes out. That both leaves nothing for the next
 * request to re-send, and collapses the burst of simultaneous 401s a page with several
 * requests in flight tends to produce into a single logout.
 */
export const endSessionIfTokenWasRejected = (status?: number) => {
  if (status !== 401) return;
  if (typeof window === 'undefined') return;
  if (!localStorage.getItem('accessToken')) return;

  localStorage.removeItem('accessToken');
  window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
};
